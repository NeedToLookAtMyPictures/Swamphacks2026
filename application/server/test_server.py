"""Run with: python -m unittest test_server"""

import hashlib
import json
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
from global_database import GlobalDatabase  # noqa: E402


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

    def test_loop_syncs_often_while_connected_and_looks_again_when_lost(self):
        finder = mock.Mock(current=("x", "configured"))
        finder.find.return_value = "x"
        loop = server_sync.SyncLoop(self.alice, finder)
        self.assertEqual(loop.step(), server_sync.SYNC_EVERY)
        self.assertEqual(loop.step(), server_sync.SYNC_EVERY)
        self.assertEqual(finder.find.call_count, 1)  # stays with the server it found
        with mock.patch.object(server_sync, "sync_once", side_effect=OSError("gone")):
            self.assertEqual(loop.step(), server_sync.CHECK_EVERY)
        self.assertIsNone(loop.url)
        finder.find.return_value = None
        self.assertEqual(loop.step(), server_sync.CHECK_EVERY)
        self.assertEqual(finder.find.call_count, 2)

    def test_server_starts_with_the_sample_global_data(self):
        data = self.client.get("/api/global").json
        self.assertEqual(len(data["locales"]), 10)
        self.assertEqual([t["id"] for t in data["trails"]], ["trail-gainesville-hawthorne", "trail-john-muir"])
        self.assertTrue(data["trails"][0]["gpx"].lstrip().startswith("<?xml"))
        self.assertEqual(len(self.client.get("/api/global_locales").json), 10)
        trail = self.client.get("/api/global_trails").json[0]
        self.assertNotIn("gpx", trail)
        self.assertGreater(trail["points"], 2)

    def test_global_data_reaches_devices_once_and_updates_follow(self):
        globe = GlobalDatabase(Path(self.tmp.name) / "alice-global.db")
        server_sync.sync_once(self.alice, "x", globe)
        self.assertEqual(len(globe.locales()), 10)
        self.assertEqual(len(globe.trails()), 2)
        version = globe.info()["version"]
        self.assertEqual(version, self.client.get("/api/global").json["version"])
        # Up to date: the server doesn't send it again
        with mock.patch.object(GlobalDatabase, "load") as load:
            server_sync.sync_once(self.alice, "x", globe)
        load.assert_not_called()
        # The server gets new global data; the device follows on its next sync
        server_app = self.client.application.config["SERVER"]
        spring = {"id": "s1", "category": "water", "name": "Spring", "latitude": 29.6,
                  "longitude": -82.3}
        server_app.global_db.load({"locales": [spring], "trails": []})
        server_sync.sync_once(self.alice, "x", globe)
        self.assertEqual([l["id"] for l in globe.locales()], ["s1"])
        self.assertEqual(globe.trails(), [])
        self.assertNotEqual(globe.info()["version"], version)

    def test_devices_that_dont_ask_get_no_global_data(self):
        response = self.client.post("/api/sync", json={"name": "alice", "device_id": "d-alice"})
        self.assertNotIn("global", response.json)
        self.assertNotIn("global_version", response.json)
        self.assertNotIn("map", response.json)
        self.assertNotIn("map_version", response.json)

    def test_server_has_the_gainesville_hawthorne_map(self):
        offline_map = server.read_map(server.MAP_DIR)
        self.assertEqual(offline_map["info"]["name"], "Gainesville to Hawthorne, Florida")
        with offline_map["path"].open("rb") as f:
            self.assertEqual(f.read(7), b"PMTiles")
        info = self.client.get("/api/map.json").json
        self.assertEqual(info["version"], offline_map["version"])
        self.assertEqual(info["bbox"], [-82.42, 29.56, -82.05, 29.73])

    def use_map_server(self, tiles):
        """Point the test client at a server with a small map; returns that map's folder."""
        server_map = Path(self.tmp.name) / "server-map"
        server_map.mkdir(exist_ok=True)
        (server_map / "map.pmtiles").write_bytes(tiles)
        (server_map / "map.json").write_text('{"name": "Test area", "bbox": [0, 0, 1, 1]}')
        self.client = server.create_app(Path(self.tmp.name) / "map.db",
                                        map_dir=server_map).test_client()
        return server_map

    def fake_download(self, _url, path, dest, timeout=None):
        data = self.client.get(path).data
        Path(dest).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_map_reaches_devices_once_and_updates_follow(self):
        server_map = self.use_map_server(b"first map")
        device_map = Path(self.tmp.name) / "alice-data"
        with mock.patch.object(server_sync, "download", side_effect=self.fake_download) as dl:
            server_sync.sync_once(self.alice, "x", map_dir=device_map)
            self.assertEqual((device_map / "map.pmtiles").read_bytes(), b"first map")
            info = json.loads((device_map / "map.json").read_text())
            self.assertEqual(info["name"], "Test area")
            self.assertEqual(info["version"], hashlib.sha256(b"first map").hexdigest())
            # Up to date: not downloaded again
            server_sync.sync_once(self.alice, "x", map_dir=device_map)
            self.assertEqual(dl.call_count, 1)
            # The server gets a new map; the device follows on its next sync
            (server_map / "map.pmtiles").write_bytes(b"second map")
            server_app = self.client.application.config["SERVER"]
            server_app.map = server.read_map(server_map)
            server_sync.sync_once(self.alice, "x", map_dir=device_map)
            self.assertEqual((device_map / "map.pmtiles").read_bytes(), b"second map")
        self.assertFalse((device_map / "map.pmtiles.part").exists())

    def test_a_bad_map_download_keeps_the_old_map(self):
        self.use_map_server(b"new map")
        device_map = Path(self.tmp.name) / "alice-data"
        device_map.mkdir()
        (device_map / "map.pmtiles").write_bytes(b"old map")
        (device_map / "map.json").write_text('{"name": "Old area"}')  # no version: replaced

        def truncated(url, path, dest, timeout=None):
            self.fake_download(url, path, dest)
            Path(dest).write_bytes(b"new m")
            return hashlib.sha256(b"new m").hexdigest()

        with mock.patch.object(server_sync, "download", side_effect=truncated):
            merged = server_sync.sync_once(self.alice, "x", map_dir=device_map)
        self.assertIsNotNone(merged)  # the records still synced
        self.assertEqual((device_map / "map.pmtiles").read_bytes(), b"old map")
        self.assertEqual(json.loads((device_map / "map.json").read_text())["name"], "Old area")
        self.assertFalse((device_map / "map.pmtiles.part").exists())
        # Tried again on the next sync
        with mock.patch.object(server_sync, "download", side_effect=self.fake_download):
            server_sync.sync_once(self.alice, "x", map_dir=device_map)
        self.assertEqual((device_map / "map.pmtiles").read_bytes(), b"new map")

    def test_server_without_a_map(self):
        self.client = server.create_app(Path(self.tmp.name) / "nomap.db",
                                        map_dir=Path(self.tmp.name) / "missing").test_client()
        self.assertEqual(self.client.get("/api/map.pmtiles").status_code, 404)
        self.assertEqual(self.client.get("/api/map.json").status_code, 404)
        response = self.client.post("/api/sync", json={"name": "alice", "device_id": "d-alice",
                                                       "map_version": None})
        self.assertIsNone(response.json["map_version"])
        self.assertNotIn("map", response.json)

    def test_map_file_supports_range_requests(self):
        self.use_map_server(b"0123456789")
        response = self.client.get("/api/map.pmtiles", headers={"Range": "bytes=2-4"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"234")

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

    def test_works_under_the_public_url_prefix(self):
        # https://heuslerene.com/server/... arrives as /server/... ; the wifi address has no prefix
        health = self.client.get("/server/api/health").json
        self.assertEqual(health, {**health, "ok": True})
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertIn('"/server/api"', self.client.get("/server/").text)
        self.assertIn('"/api"', self.client.get("/").text)
        self.assertEqual(self.client.get("/serverless/api/health").status_code, 404)

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

    def test_finder_tries_several_servers_in_order(self):
        finder = server_sync.ServerFinder("https://public.example, http://10.0.0.5:8000", lan=False)
        self.assertEqual(finder.urls, ["https://public.example", "http://10.0.0.5:8000"])
        with mock.patch.object(server_sync.ServerFinder, "answers", return_value=True):
            self.assertEqual(finder.find(), "https://public.example")
        with mock.patch.object(server_sync.ServerFinder, "answers",
                               side_effect=lambda url: url.startswith("http://10.")):
            self.assertEqual(finder.find(), "http://10.0.0.5:8000")
        self.assertEqual(server_sync.ServerFinder("", lan=True).describe(), "a server on the wifi")


if __name__ == "__main__":
    unittest.main()
