"""Run with: python -m unittest test_database"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from flask import Flask

from api import api
from database import MESSAGE_TTL, Database, DeviceNotInitialized


def make_db(tmp, name, user_id, device_id):
    db = Database(Path(tmp) / f"{name}.db")
    db.init_device(name, user_id=user_id, device_id=device_id)
    return db


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.alice = make_db(self.tmp.name, "alice", "u-alice", "d-alice")
        self.bob = make_db(self.tmp.name, "bob", "u-bob", "d-bob")
        self.carol = make_db(self.tmp.name, "carol", "u-carol", "d-carol")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_is_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.alice.init_device("someone else")
        with self.alice._connect() as conn:
            for column in ("device_id", "user_id", "name"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"UPDATE device_info SET {column} = 'x'")
        self.assertEqual(self.alice.get_device()["name"], "alice")

    def test_seq_increments_across_tables(self):
        a = self.alice.add_message("u-bob", "hi")
        b = self.alice.add_locale("loc-1", "Spring", "clean water")
        c = self.alice.add_tombstone("d-x", 1, "test")
        self.assertEqual([a["seq"], b["seq"], c["seq"]], [1, 2, 3])
        self.assertEqual(a["origin_id"], "d-alice")
        self.assertEqual(self.alice.get_device()["next_seq"], 4)

    def test_requires_device(self):
        db = Database(Path(self.tmp.name) / "empty.db")
        with self.assertRaises(DeviceNotInitialized):
            db.add_message("u-bob", "hi")

    def test_sync_is_idempotent(self):
        self.alice.add_message("u-bob", "hi")
        self.alice.add_locale("loc-1", "Spring")
        self.assertEqual(self.bob.merge(self.alice.export())["messages"], 1)
        self.assertEqual(self.bob.merge(self.alice.export()),
                         {"locales": 0, "messages": 0, "geolocations": 0, "tombstones": 0})

    def test_delivered_tombstone_kills_message_everywhere(self):
        msg = self.alice.add_message("u-bob", "hi")
        self.carol.merge(self.alice.export())   # carol relays it
        self.bob.merge(self.carol.export())     # bob receives it
        self.bob.mark_delivered(msg["origin_id"], msg["seq"])
        self.assertTrue(self.bob.get("messages", "d-alice", 1)["is_dead"])

        self.carol.merge(self.bob.export())
        self.assertTrue(self.carol.get("messages", "d-alice", 1)["is_dead"])

    def test_dead_message_is_not_picked_up(self):
        msg = self.alice.add_message("u-bob", "hi")
        self.bob.merge(self.alice.export())
        self.bob.mark_delivered(msg["origin_id"], msg["seq"])
        # carol never had the message, so she doesn't save it, but keeps the tombstone
        self.carol.merge(self.bob.export())
        self.assertIsNone(self.carol.get("messages", "d-alice", 1))
        self.assertEqual(len(self.carol.list("tombstones")), 1)

    def test_old_messages_deleted_except_for_sender_and_receiver(self):
        old = time.time() - MESSAGE_TTL - 10
        message = dict(origin_id="d-alice", seq=1, time_created=old, sender="u-alice",
                       receiver="u-bob", content="old", is_dead=0)
        for db in (self.alice, self.bob, self.carol):
            with db._connect() as conn:
                db._insert(conn, "messages", {**message, "time_received": old})
            db.purge()
        self.assertTrue(self.alice.get("messages", "d-alice", 1)["is_dead"])
        self.assertTrue(self.bob.get("messages", "d-alice", 1)["is_dead"])
        self.assertIsNone(self.carol.get("messages", "d-alice", 1))
        self.assertEqual(self.alice.export()["messages"], [])  # kept, but not shared

    def test_geolocation_keeps_newest_with_device_tiebreak(self):
        point = dict(user="u-x", time_created=100.0, latitude=1.0, longitude=1.0)
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
        app.register_blueprint(api)
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_flow(self):
        c = self.client
        self.assertEqual(c.get("/api/device").status_code, 409)
        r = c.post("/api/device", json={"name": "Alice", "user_id": "u-alice"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json["next_seq"], 1)
        self.assertEqual(c.post("/api/device", json={"name": "Eve"}).status_code, 409)
        self.assertEqual(c.patch("/api/device", json={"name": "Eve"}).status_code, 400)
        self.assertEqual(c.patch("/api/device", json={"last_server_sync": 5}).json["last_server_sync"], 5)

        msg = c.post("/api/messages", json={"receiver": "u-bob", "content": "hi"}).json
        self.assertEqual(msg["seq"], 1)
        self.assertEqual(c.post("/api/messages", json={"receiver": "u-bob"}).status_code, 400)
        self.assertEqual(c.post("/api/locales", json={"location": "l1", "name": "Spring"}).status_code, 201)
        self.assertEqual(c.put("/api/location", json={"latitude": 29.6, "longitude": -82.3}).status_code, 200)
        # only the receiver may mark a message delivered
        self.assertEqual(c.post("/api/messages/%s/1/delivered" % msg["origin_id"]).status_code, 403)

        incoming = {"messages": [dict(origin_id="d-bob", seq=1, time_created=time.time(),
                                      sender="u-bob", receiver="u-alice", content="yo")]}
        self.assertEqual(c.post("/api/sync", json=incoming).json["merged"]["messages"], 1)
        self.assertEqual(c.post("/api/messages/d-bob/1/delivered").status_code, 201)
        self.assertEqual(c.get("/api/messages/d-bob/1").json["is_dead"], 1)

        shared = c.get("/api/sync").json
        self.assertEqual(len(shared["messages"]), 2)
        self.assertEqual(len(shared["tombstones"]), 1)
        self.assertEqual(len(shared["geolocations"]), 1)
        self.assertEqual(c.get("/api/nope").status_code, 404)
        self.assertEqual(c.post("/api/sync", json={"nope": []}).status_code, 400)
        self.assertEqual(c.post("/api/sync", json={"messages": [{"seq": 1}]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
