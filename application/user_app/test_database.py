"""Run with: python -m unittest test_database"""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from flask import Flask

from api import api
from database import MESSAGE_TTL, Database, DeviceNotInitialized


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
        app.register_blueprint(api)
        self.client = app.test_client()

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


if __name__ == "__main__":
    unittest.main()
