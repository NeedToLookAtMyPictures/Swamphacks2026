"""Run with: python -m unittest test_server"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ["SERVER_DB"] = str(Path(_tmp.name) / "default.db")  # keep import from touching data/

import server  # noqa: E402
import server_sync  # noqa: E402  (importable once server has set up the path)
from database import Database  # noqa: E402


class ServerSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.client = server.create_app(tmp / "server.db").test_client()
        self.alice = Database(tmp / "alice.db")
        self.alice.init_device("alice", "d-alice")
        self.bob = Database(tmp / "bob.db")
        self.bob.init_device("bob", "d-bob")
        # Send the device's requests to the test client instead of over the network
        patcher = mock.patch.object(server_sync, "post", self.post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, _url, path, payload):
        response = self.client.post(path, json=payload)
        if response.status_code >= 400:
            raise server_sync.ServerError(f"{response.status_code}: {response.json['error']}")
        return response.json

    def test_message_travels_through_server_and_delivery_comes_back(self):
        message = self.alice.add_message("bob", "hi")
        server_sync.sync_once(self.alice, "x")
        server_sync.sync_once(self.bob, "x")
        received = self.bob.get("messages", "d-alice", message["seq"])
        self.assertEqual(received["content"], "hi")
        self.assertEqual(received["is_dead"], 1)  # bob is the receiver: delivered on arrival
        # bob's delivered tombstone reaches alice through the server
        server_sync.sync_once(self.bob, "x")
        server_sync.sync_once(self.alice, "x")
        self.assertEqual(self.alice.get("messages", "d-alice", message["seq"])["is_dead"], 1)

    def test_records_relayed_by_another_device_are_pushed(self):
        # carol's old record reaches alice offline, long after it was written
        server_sync.sync_once(self.alice, "x")  # alice has synced before
        self.alice.merge({"locales": [dict(
            origin_id="d-carol", seq=1, time_created=time.time() - 3600, location="loc-1",
            name="Spring", sender="carol", comment="clean water")]})
        server_sync.sync_once(self.alice, "x")
        server_sync.sync_once(self.bob, "x")
        self.assertEqual([r["comment"] for r in self.bob.list("locales")], ["clean water"])

    def test_cursors_advance_and_repeat_syncs_are_harmless(self):
        self.alice.set_location(29.6, -82.3)
        first = server_sync.sync_once(self.alice, "x")
        device = self.alice.get_device()
        self.assertIsNotNone(device["server_cursor"])
        self.assertIsNotNone(device["last_push"])
        self.assertIsNotNone(device["last_server_sync"])
        self.assertEqual(first, {"locales": 0, "messages": 0, "geolocations": 0, "tombstones": 0})
        self.assertEqual(server_sync.sync_once(self.alice, "x"), first)
        server_sync.sync_once(self.bob, "x")
        self.assertEqual(self.bob.contacts()[0]["name"], "alice")

    def test_register_keeps_names_unique(self):
        self.assertEqual(server_sync.register("x", "dave", "d-dave"),
                         {"name": "dave", "device_id": "d-dave"})
        self.assertEqual(server_sync.register("x", "dave", "d-dave")["device_id"], "d-dave")
        with self.assertRaises(server_sync.ServerError):
            server_sync.register("x", "DAVE", "d-other")
        with self.assertRaises(server_sync.ServerError):
            server_sync.register("x", "two words")
        self.assertEqual(len(server_sync.register("x", "erin")["device_id"]), 8)
        # devices created offline are registered when they first sync
        server_sync.sync_once(self.alice, "x")
        with self.assertRaises(server_sync.ServerError):
            server_sync.register("x", "alice", "d-imposter")
        names = [u["name"] for u in self.client.get("/api/users").json]
        self.assertEqual(names, ["alice", "dave", "erin"])

    def test_bad_sync_requests(self):
        c = self.client
        self.assertEqual(c.get("/api/health").status_code, 200)
        self.assertEqual(c.post("/api/sync", json={"name": "alice"}).status_code, 400)
        self.assertEqual(c.post("/api/sync", json={"name": "alice", "device_id": "d",
                                                   "records": {"nope": []}}).status_code, 400)
        self.assertEqual(c.post("/api/sync", json={"name": "alice", "device_id": "d",
                                                   "records": {"messages": [{"seq": 1}]}}
                                ).status_code, 400)
        self.assertEqual(c.get("/api/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
