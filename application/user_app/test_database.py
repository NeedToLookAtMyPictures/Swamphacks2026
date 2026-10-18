"""Run with: python -m unittest test_database"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

import server_sync
from api import api
from database import MESSAGE_TTL, SCHEMA, SHARED_TABLES, Database, DeviceNotInitialized
from global_database import GlobalDatabase


class FakeServer:
    """Stands in for the sync server, so account creation can reserve names."""

    def __init__(self, reachable=True):
        self.reachable = reachable
        self.names = {}
        self.current = None

    def find(self):
        return "http://fake" if self.reachable else None

    def describe(self):
        return "a fake server"

    def register(self, _url, name, device_id=None):
        if name.lower() in self.names and self.names[name.lower()] != device_id:
            raise server_sync.ServerError("409: taken")
        self.names[name.lower()] = device_id or "d-" + name.lower()
        return {"name": name, "device_id": self.names[name.lower()]}

    def install(self, app, test):
        """Use this as app's server for the rest of the test."""
        app.config["SERVER"] = self
        for name, fake in (("register", self.register),
                           ("sync_once", lambda *_: None)):
            patcher = mock.patch.object(server_sync, name, fake)
            patcher.start()
            test.addCleanup(patcher.stop)


def make_db(tmp, name, device_id):
    db = Database(Path(tmp) / f"{name}.db")
    db.init_device(name, device_id=device_id)
    return db


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.alice = make_db(self.tmp.name, "alice", "d-alice")
        self.bob = make_db(self.tmp.name, "bob", "d-bob")
        self.carol = make_db(self.tmp.name, "carol", "d-carol")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_is_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.alice.init_device("someone_else")
        with self.alice._connect() as conn:
            for column in ("device_id", "name"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"UPDATE device_info SET {column} = 'x'")
        self.assertEqual(self.alice.get_device()["name"], "alice")

    def test_seq_increments_across_tables(self):
        a = self.alice.add_message("bob", "hi")
        b = self.alice.add_locale("loc-1", "Spring", "clean water")
        c = self.alice.add_tombstone("d-x", 1, "test")
        self.assertEqual([a["seq"], b["seq"], c["seq"]], [1, 2, 3])
        self.assertEqual(a["origin_id"], "d-alice")
        self.assertEqual(self.alice.get_device()["next_seq"], 4)

    def test_requires_device(self):
        db = Database(Path(self.tmp.name) / "empty.db")
        with self.assertRaises(DeviceNotInitialized):
            db.add_message("bob", "hi")

    def test_sync_is_idempotent(self):
        self.alice.add_message("bob", "hi")
        self.alice.add_locale("loc-1", "Spring")
        self.assertEqual(self.bob.merge(self.alice.export())["messages"], 1)
        self.assertEqual(self.bob.merge(self.alice.export()),
                         {"locales": 0, "messages": 0, "geolocations": 0, "tombstones": 0})

    def test_delivered_tombstone_kills_message_everywhere(self):
        msg = self.alice.add_message("bob", "hi")
        self.carol.merge(self.alice.export())   # carol relays it
        self.assertFalse(self.carol.get("messages", "d-alice", 1)["is_dead"])  # relaying isn't delivery
        self.bob.merge(self.carol.export())     # bob receives it: delivered automatically
        self.assertTrue(self.bob.get("messages", "d-alice", 1)["is_dead"])
        self.assertEqual([t["reason"] for t in self.bob.list("tombstones")], ["delivered"])

        self.carol.merge(self.bob.export())
        self.assertTrue(self.carol.get("messages", "d-alice", 1)["is_dead"])

    def test_sender_sees_received_only_from_tombstone_within_31_days(self):
        msg = self.alice.add_message("bob", "hi")
        self.bob.merge(self.alice.export())
        self.alice.merge(self.bob.export())     # tombstone reaches alice in time
        message = self.alice.get("messages", "d-alice", msg["seq"])
        self.assertTrue(message["is_dead"])
        self.assertTrue(message["received"])
        self.assertNotIn("received", self.alice.export()["messages"][0])  # local only

        # the tombstone reaches alice after the message is 31 days old: received or died
        late = self.alice.add_message("bob", "late")
        self.bob.merge(self.alice.export())
        with self.alice._connect() as conn:
            conn.execute("UPDATE messages SET time_created = ? WHERE seq = ?",
                         (time.time() - MESSAGE_TTL - 10, late["seq"]))
        self.alice.merge(self.bob.export())
        message = self.alice.get("messages", "d-alice", late["seq"])
        self.assertTrue(message["is_dead"])
        self.assertFalse(message["received"])

    def test_expired_message_is_not_received(self):
        msg = self.alice.add_message("bob", "hi")
        with self.alice._connect() as conn:
            conn.execute("UPDATE messages SET time_created = ?", (time.time() - MESSAGE_TTL - 10,))
        self.alice.purge()
        message = self.alice.get("messages", "d-alice", msg["seq"])
        self.assertTrue(message["is_dead"])
        self.assertFalse(message["received"])

    def test_schema_2_gains_received_column(self):
        path = Path(self.tmp.name) / "v2.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA.replace(
                "    received      INTEGER NOT NULL DEFAULT 0,  -- local only: delivered tombstone came within 31 days\n", ""))
            conn.execute("PRAGMA user_version = 2")
        conn.close()
        db = Database(path)
        db.init_device("erin")
        db.add_message("bob", "hi")
        self.assertEqual(db.list("messages")[0]["received"], 0)

    def test_schema_3_gains_sync_cursors(self):
        path = Path(self.tmp.name) / "v3.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute("ALTER TABLE device_info DROP COLUMN last_push")
            conn.execute("ALTER TABLE device_info DROP COLUMN server_cursor")
            conn.execute("PRAGMA user_version = 3")
        conn.close()
        db = Database(path)
        db.init_device("erin")
        self.assertIsNone(db.update_device(last_push=5.0)["server_cursor"])

    def test_schema_4_gains_server_id(self):
        path = Path(self.tmp.name) / "v4.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute("ALTER TABLE device_info DROP COLUMN server_id")
            conn.execute("PRAGMA user_version = 4")
        conn.close()
        db = Database(path)
        db.init_device("erin")
        self.assertEqual(db.update_device(server_id="s1")["server_id"], "s1")

    def test_schema_5_gains_received_via(self):
        path = Path(self.tmp.name) / "v5.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            for table in SHARED_TABLES:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN received_via")
            conn.execute("PRAGMA user_version = 5")
        conn.close()
        db = Database(path)
        db.init_device("erin")
        theirs = self.alice.add_locale("loc-1", "Spring")
        db.merge(self.alice.export(), via="LoRa")
        self.assertEqual(db.get("locales", "d-alice", theirs["seq"])["received_via"], "LoRa")
        mine = db.add_locale("loc-1", "Spring")
        self.assertIsNone(db.get("locales", mine["origin_id"], mine["seq"])["received_via"])

    def test_received_via_is_kept_locally_only(self):
        self.alice.add_message("bob", "hi")
        self.alice.add_locale("loc-1", "Spring")
        self.alice.set_location(29.65, -82.32)
        self.bob.merge(self.alice.export(), via="LoRa")
        self.assertEqual(self.bob.get("messages", "d-alice", 1)["received_via"], "LoRa")
        self.assertEqual(self.bob.get("locales", "d-alice", 2)["received_via"], "LoRa")
        self.assertEqual(self.bob.get_location("alice")["received_via"], "LoRa")
        delivered = self.bob.list("tombstones")[0]  # bob made it himself
        self.assertIsNone(delivered["received_via"])
        self.assertIsNone(self.alice.get("messages", "d-alice", 1)["received_via"])
        self.assertTrue(all("received_via" not in row
                            for rows in self.bob.export().values() for row in rows))

        self.alice.set_location(29.66, -82.32)  # a newer position arriving another way
        self.bob.merge(self.alice.export(), via="ESP-NOW")
        self.assertEqual(self.bob.get_location("alice")["received_via"], "ESP-NOW")
        with self.assertRaises(ValueError):
            self.bob.merge({}, via="carrier pigeon")

    def test_dead_message_is_not_picked_up(self):
        msg = self.alice.add_message("bob", "hi")
        self.bob.merge(self.alice.export())     # delivered on arrival
        # carol never had the message, so she doesn't save it, but keeps the tombstone
        self.carol.merge(self.bob.export())
        self.assertIsNone(self.carol.get("messages", "d-alice", 1))
        self.assertEqual(len(self.carol.list("tombstones")), 1)

    def test_old_messages_deleted_except_for_sender_and_receiver(self):
        old = time.time() - MESSAGE_TTL - 10
        message = dict(origin_id="d-alice", seq=1, time_created=old, sender="alice",
                       receiver="bob", content="old", is_dead=0)
        for db in (self.alice, self.bob, self.carol):
            with db._connect() as conn:
                db._insert(conn, "messages", {**message, "time_received": old})
            db.purge()
        self.assertTrue(self.alice.get("messages", "d-alice", 1)["is_dead"])
        self.assertTrue(self.bob.get("messages", "d-alice", 1)["is_dead"])
        self.assertIsNone(self.carol.get("messages", "d-alice", 1))
        self.assertEqual(self.alice.export()["messages"], [])  # kept, but not shared

    def test_message_to_a_name_reaches_that_person_on_any_device(self):
        # alice addresses "dave", whom nobody has met; it is relayed until dave sees it
        msg = self.alice.add_message("dave", "are you out there?")
        self.carol.merge(self.alice.export())
        dave = make_db(self.tmp.name, "dave", "d-dave")
        dave.merge(self.carol.export())         # delivered on arrival
        self.assertEqual(dave.get("messages", "d-alice", msg["seq"])["receiver"], "dave")
        self.alice.merge(dave.export())
        self.assertTrue(self.alice.get("messages", "d-alice", msg["seq"])["is_dead"])

    def test_broadcast_reaches_everyone_and_is_never_delivered(self):
        msg = self.alice.add_message("*", "Bridge at mile 3 is out")
        self.bob.merge(self.alice.export())
        self.carol.merge(self.bob.export())
        for db in (self.bob, self.carol):
            message = db.get("messages", "d-alice", msg["seq"])
            self.assertEqual(message["receiver"], "*")
            self.assertFalse(message["is_dead"])
            self.assertEqual(db.list("tombstones"), [])
        self.assertNotIn("*", [c["name"] for c in self.carol.contacts()])
        with self.assertRaises(ValueError):
            self.alice.add_message("everyone!", "not a valid name")

    def test_names_are_case_insensitive(self):
        msg = self.alice.add_message("BOB", "hi")
        self.bob.merge(self.alice.export())
        self.assertTrue(self.bob.get("messages", "d-alice", msg["seq"])["is_dead"])  # "BOB" is bob
        old = time.time() - MESSAGE_TTL - 10
        with self.bob._connect() as conn:  # bob keeps old messages addressed to him
            conn.execute("UPDATE messages SET time_created = ?", (old,))
        self.bob.purge()
        self.assertIsNotNone(self.bob.get("messages", "d-alice", msg["seq"]))

    def test_name_rules(self):
        with self.assertRaises(ValueError):
            self.alice.add_message("alice", "talking to myself")
        with self.assertRaises(ValueError):
            self.alice.add_message("has spaces", "hi")
        with self.assertRaises(ValueError):
            Database(Path(self.tmp.name) / "bad.db").init_device("x" * 33)
        # a fresh device that already holds alice's data can't take her name
        self.alice.set_location(1, 2)
        newcomer = Database(Path(self.tmp.name) / "newcomer.db")
        with newcomer._connect() as conn:
            newcomer._upsert_geolocation(conn, {**self.alice.list("geolocations")[0]})
        with self.assertRaises(ValueError):
            newcomer.init_device("Alice")
        newcomer.init_device("Alicia")

    def test_contacts(self):
        self.assertEqual(self.alice.contacts(), [])
        self.bob.set_location(29.6, -82.3)
        self.bob.add_message("dave", "hi")
        self.alice.merge(self.bob.export())
        contacts = {c["name"]: c for c in self.alice.contacts()}
        self.assertEqual(set(contacts), {"bob", "dave"})
        self.assertEqual(contacts["bob"]["latitude"], 29.6)
        self.assertIsNone(contacts["dave"]["latitude"])

    def test_old_schema_is_refused(self):
        path = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE device_info (user_id TEXT)")
        with self.assertRaises(RuntimeError):
            Database(path)

    def test_geolocation_keeps_newest_with_device_tiebreak(self):
        point = dict(user="xavier", time_created=100.0, latitude=1.0, longitude=1.0)
        self.alice.merge({"geolocations": [{**point, "origin_id": "d-b"}]})
        self.alice.merge({"geolocations": [{**point, "origin_id": "d-a", "latitude": 2.0}]})
        self.assertEqual(self.alice.list("geolocations")[0]["latitude"], 1.0)  # lower ID loses
        self.alice.merge({"geolocations": [{**point, "origin_id": "d-c", "latitude": 3.0}]})
        self.assertEqual(self.alice.list("geolocations")[0]["latitude"], 3.0)  # higher ID wins
        self.alice.merge({"geolocations": [{**point, "origin_id": "d-z", "time_created": 50.0}]})
        self.assertEqual(self.alice.list("geolocations")[0]["latitude"], 3.0)  # older loses


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = Flask(__name__)
        app.config["DATABASE"] = Database(Path(self.tmp.name) / "api.db")
        app.config["GLOBAL_DATABASE"] = GlobalDatabase(Path(self.tmp.name) / "global.db")
        app.register_blueprint(api)
        self.client = app.test_client()
        self.server = FakeServer()
        self.server.install(app, self)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flow(self):
        c = self.client
        self.assertEqual(c.get("/api/device").status_code, 409)
        self.assertEqual(c.post("/api/device", json={"name": "two words"}).status_code, 400)
        r = c.post("/api/device", json={"name": "Alice"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json["next_seq"], 1)
        self.assertEqual(c.post("/api/device", json={"name": "Eve"}).status_code, 409)
        self.assertEqual(c.post("/api/messages", json={"receiver": "alice", "content": "x"}).status_code, 400)
        self.assertEqual(c.patch("/api/device", json={"name": "Eve"}).status_code, 400)
        self.assertEqual(c.patch("/api/device", json={"last_server_sync": 5}).json["last_server_sync"], 5)

        msg = c.post("/api/messages", json={"receiver": "bob", "content": "hi"}).json
        self.assertEqual(msg["seq"], 1)
        self.assertEqual(c.post("/api/messages", json={"receiver": "bob"}).status_code, 400)
        self.assertEqual(c.post("/api/locales", json={"location": "l1", "name": "Spring"}).status_code, 201)
        self.assertEqual(c.put("/api/location", json={"latitude": 29.6, "longitude": -82.3}).status_code, 200)
        # only the receiver may mark a message delivered
        self.assertEqual(c.post("/api/messages/%s/1/delivered" % msg["origin_id"]).status_code, 403)

        incoming = {"messages": [dict(origin_id="d-bob", seq=1, time_created=time.time(),
                                      sender="bob", receiver="ALICE", content="yo")]}
        self.assertEqual(c.post("/api/sync", json=incoming).json["merged"]["messages"], 1)
        self.assertEqual(c.get("/api/messages/d-bob/1").json["is_dead"], 1)  # delivered on arrival
        self.assertEqual(c.get("/api/messages/d-bob/1").json["received_via"], "sync API")
        self.assertEqual(c.post("/api/messages/d-bob/1/delivered").status_code, 409)
        self.assertEqual(c.get("/api/messages/d-bob/1").json["is_dead"], 1)

        shared = c.get("/api/sync").json
        self.assertEqual(len(shared["messages"]), 2)
        self.assertEqual(len(shared["tombstones"]), 1)
        self.assertEqual(len(shared["geolocations"]), 1)
        self.assertEqual([p["name"] for p in c.get("/api/contacts").json], ["bob"])
        self.assertEqual(c.post("/api/messages", json={"receiver": "*", "content": "all"}).status_code, 201)
        broadcast = c.get("/api/messages").json[-1]
        # a broadcast has no single receiver, so nobody can mark it delivered
        self.assertEqual(c.post(f"/api/messages/{broadcast['origin_id']}/{broadcast['seq']}/delivered").status_code, 403)
        self.assertEqual(c.get("/api/nope").status_code, 404)
        self.assertEqual(c.post("/api/sync", json={"nope": []}).status_code, 400)
        self.assertEqual(c.post("/api/sync", json={"messages": [{"seq": 1}]}).status_code, 400)

    def test_account_needs_the_server(self):
        c = self.client
        self.server.reachable = False
        r = c.post("/api/device", json={"name": "alice"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("server", r.json["error"])
        self.assertEqual(c.get("/api/device").status_code, 409)  # nothing was created
        self.server.reachable = True
        self.server.register("x", "alice", "d-someone-else")
        self.assertEqual(c.post("/api/device", json={"name": "ALICE"}).status_code, 409)
        self.assertEqual(c.post("/api/device", json={"name": "two words"}).status_code, 400)
        r = c.post("/api/device", json={"name": "bob"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json["device_id"], "d-bob")  # the ID the server handed out
        self.assertEqual(c.get("/api/server").json["looking_for"], "a fake server")

    def test_global_load(self):
        c = self.client
        spring = {"id": "w1", "category": "Water", "name": "Spring", "latitude": 1, "longitude": 2}
        self.assertEqual(c.post("/api/global/load", json={}).status_code, 400)
        self.assertEqual(c.post("/api/global/load", json={"locales": [spring]}).json, {"locales": 1, "trails": 0})
        self.assertEqual(c.get("/api/global/locales").json[0]["category"], "water")
        self.assertEqual(c.get("/api/global/categories").json, [{"category": "water", "count": 1}])
        self.assertIsNotNone(c.get("/api/global/info").json["loaded_at"])
        gpx = GlobalDatabaseTest.GPX
        c.post("/api/global/load", json={"locales": [], "trails": [{"id": "t1", "name": "Loop", "gpx": gpx}]})
        self.assertEqual(c.get("/api/global/trails").json[0]["name"], "Loop")
        self.assertEqual(c.get("/api/global/trails/t1.gpx").get_data(as_text=True), gpx)
        self.assertEqual(c.get("/api/global/trails/nope.gpx").status_code, 404)


class GlobalDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = GlobalDatabase(Path(self.tmp.name) / "global.db")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def locale(id, category="water"):
        return {"id": id, "category": category, "name": id, "latitude": 1.0, "longitude": 2.0}

    def test_load_replaces_everything(self):
        self.assertIsNone(self.db.info()["loaded_at"])
        self.db.load({"locales": [self.locale("a"), self.locale("b", "campsite")]})
        self.db.load({"locales": [self.locale("c")]})
        self.assertEqual([l["id"] for l in self.db.locales()], ["c"])
        self.assertIsNotNone(self.db.info()["loaded_at"])

    GPX = ('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
           '<trkpt lat="29.0" lon="-82.0"/><trkpt lat="29.01" lon="-82.0"/>'
           '</trkseg></trk></gpx>')

    def test_trails(self):
        self.db.load({"locales": [], "trails": [{"id": "t1", "name": "Loop", "gpx": self.GPX}]})
        [trail] = self.db.trails()
        self.assertEqual(trail["segments"], [[[29.0, -82.0], [29.01, -82.0]]])
        self.assertAlmostEqual(trail["length_km"], 1.11, places=2)
        self.assertEqual(self.db.trail_gpx("t1"), self.GPX)
        # routes (rte/rtept) work too, without a namespace
        self.db.load({"locales": [], "trails": [{"id": "t2", "name": "Route", "gpx":
            '<gpx><rte><rtept lat="1" lon="2"/><rtept lat="3" lon="4"/></rte></gpx>'}]})
        self.assertEqual([t["id"] for t in self.db.trails()], ["t2"])

    def test_bad_gpx_is_refused(self):
        for gpx in ("not xml", "<gpx><trk><trkseg><trkpt lat='1' lon='2'/></trkseg></trk></gpx>"):
            with self.assertRaises(ValueError):
                self.db.load({"locales": [], "trails": [{"id": "t", "name": "x", "gpx": gpx}]})

    def test_bad_record_changes_nothing(self):
        self.db.load({"locales": [self.locale("a")]})
        with self.assertRaises(ValueError):
            self.db.load({"locales": [self.locale("b"), {"id": "c"}]})
        self.assertEqual([l["id"] for l in self.db.locales()], ["a"])


if __name__ == "__main__":
    unittest.main()
