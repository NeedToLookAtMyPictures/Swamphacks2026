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
import discovery  # noqa: E402  (importable once server has set up the path)
import server_sync  # noqa: E402
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
        patcher = mock.patch.object(server_sync, "call", self.call)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, _url, path, payload=None, timeout=None):
        response = (self.client.get(path) if payload is None
                    else self.client.post(path, json=payload))
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

    def test_new_server_gets_a_full_exchange(self):
        # alice synced with one server; her cursors mean nothing to another one
        self.alice.add_message("bob", "hi")
        server_sync.sync_once(self.alice, "x")
        first_server = self.alice.get_device()["server_id"]
        self.client = server.create_app(Path(self.tmp.name) / "wifi.db").test_client()
        server_sync.sync_once(self.alice, "x")
        self.assertNotEqual(self.alice.get_device()["server_id"], first_server)
        self.assertEqual(len(self.client.get("/api/messages").json), 1)

    def test_server_id_survives_restarts(self):
        path = Path(self.tmp.name) / "restart.db"
        first = server.Server(path).server_id
        self.assertEqual(server.Server(path).server_id, first)
        self.assertEqual(len(first), 8)

    def test_dashboard_shows_the_database(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.client.get("/api/health").json["server_id"], page.text)
        for table in ("users", "locales", "messages", "geolocations", "tombstones"):
            self.assertIn(f'"{table}"', page.text)

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


class DiscoveryTest(unittest.TestCase):
    def test_device_finds_the_server(self):
        # Ask the loopback address instead of broadcasting to the whole network
        port = 48765
        discovery.start_responder(8123, port=port)
        self.assertEqual(discovery.find_servers(timeout=0.5, port=port, address="127.0.0.1"),
                         ["http://127.0.0.1:8123"])

    def test_nobody_answers(self):
        self.assertEqual(discovery.find_servers(timeout=0.2, port=48766, address="127.0.0.1"), [])

    def test_finder_prefers_the_configured_server_then_the_wifi(self):
        finder = server_sync.ServerFinder("http://internet", lan=True)
        with mock.patch.object(server_sync.ServerFinder, "answers",
                               side_effect=lambda url: url == "http://internet"):
            self.assertEqual(finder.find(), "http://internet")
            self.assertEqual(finder.current[1], "configured")
        with mock.patch.object(server_sync.ServerFinder, "answers", return_value=False), \
                mock.patch.object(discovery, "find_servers", return_value=["http://10.0.0.5:8000"]):
            self.assertEqual(finder.find(), "http://10.0.0.5:8000")
            self.assertEqual(finder.current[1], "wifi")
        with mock.patch.object(server_sync.ServerFinder, "answers", return_value=False), \
                mock.patch.object(discovery, "find_servers", return_value=[]):
            self.assertIsNone(finder.find())
        self.assertIsNone(server_sync.ServerFinder(None, lan=False).find())


if __name__ == "__main__":
    unittest.main()
