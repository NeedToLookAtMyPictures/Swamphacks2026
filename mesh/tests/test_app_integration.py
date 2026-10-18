"""End to end: the web app's API and a mesh node share one database.

Two app instances (alice, bob) each get their own Flask API and app database,
and each runs a mesh node on that same database over the simulated radio.
Data goes in and comes out only through the HTTP API the web interface uses,
so this checks the whole path: UI -> API -> app database -> mesh -> peer's
app database -> peer's API -> peer's UI.

Needs Flask (the app's requirements); skipped without it.
"""
import collections
import os
import random
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "..", "application", "user_app")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, APP_DIR)

from trailmesh import AppStore, Config, Node  # noqa: E402
from trailmesh.lora import LoRaParams  # noqa: E402
from trailmesh.sim import SimClock, SimMedium, run  # noqa: E402

try:
    from flask import Flask

    from api import api
    from database import Database
    from global_database import GlobalDatabase
except ImportError:  # Flask not installed, or a checkout without the app
    Flask = None


def make_app(directory, name, device_id):
    """One user's app: its database plus a Flask test client for its API."""
    db = Database(os.path.join(directory, f"{name}.db"))
    db.init_device(name, device_id)
    app = Flask(name)
    app.config["DATABASE"] = db
    app.config["GLOBAL_DATABASE"] = GlobalDatabase(os.path.join(directory, f"{name}-global.db"))
    app.register_blueprint(api)
    return db, app.test_client()


@unittest.skipIf(Flask is None, "needs Flask and the user app (pip install -r application/user_app/requirements.txt)")
class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trailmesh-app-")
        self.clock = SimClock()
        self.medium = SimMedium(self.clock)
        self.alice_db, self.alice = make_app(self.tmp, "alice", "00000001")
        self.bob_db, self.bob = make_app(self.tmp, "bob", "00000002")
        self.delivered = []
        cfg = Config(always_on=True)
        self.nodes = [
            Node(self.medium.link("a"), AppStore(self.alice_db), cfg, clock=self.clock,
                 on_delivered=lambda o, s: self.delivered.append((o, s)), rng=random.Random(1)),
            Node(self.medium.link("b"), AppStore(self.bob_db), cfg, clock=self.clock, rng=random.Random(2)),
        ]

    def test_mesh_status_names_peers_and_rows_say_how_they_arrived(self):
        """GET /api/mesh (the Mesh tab) and received_via (the "via ..." notes)."""
        self.assertEqual(self.bob.get("/api/mesh").get_json(), {"enabled": False})
        # What main.py's start_mesh puts in the app config, minus a real board
        self.bob.application.config["MESH"] = {
            "port": "sim", "board_kind": "espnow", "radio": "auto", "board": None,
            "node": self.nodes[1], "events": collections.deque([{"at": 0, "text": "hello"}]),
        }
        sent = self.alice.post("/api/messages", json={"receiver": "*", "content": "bear at the shelter"}).get_json()
        run(self.nodes, self.clock, 15, until=lambda: bool(self.nodes[1].syncs))
        self.assertTrue(self.nodes[1].syncs, "no exchange finished")

        st = self.bob.get("/api/mesh").get_json()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["events"], [{"at": 0, "text": "hello"}])
        node = st["node"]
        self.assertEqual(node["id"], "00000002")
        self.assertEqual(node["nearby"], 1)
        [peer] = node["peers"]
        self.assertEqual(peer["name"], "alice")  # from alice's message's origin_id
        self.assertEqual(peer["id"], "00000001")
        self.assertEqual(peer["heard_on"], "ESP-NOW")
        self.assertEqual(peer["rssi"], -60)
        self.assertTrue(peer["last_sync"]["ok"])
        self.assertEqual(peer["last_sync"]["radio"], "ESP-NOW")
        self.assertGreaterEqual(peer["last_sync"]["received"], 1)

        theirs = self.bob.get(f"/api/messages/{sent['origin_id']}/{sent['seq']}").get_json()
        self.assertEqual(theirs["received_via"], "ESP-NOW")
        mine = self.alice.get(f"/api/messages/{sent['origin_id']}/{sent['seq']}").get_json()
        self.assertIsNone(mine["received_via"])  # written on this device

    def test_message_posted_in_one_app_appears_in_the_other_and_is_delivered(self):
        sent = self.alice.post("/api/messages", json={"receiver": "bob", "content": "bridge out at mile 12"})
        self.assertLess(sent.status_code, 300, sent.get_data(as_text=True))
        msg = sent.get_json()

        def bob_sees_it():
            rows = self.bob.get("/api/messages").get_json()
            return any(r["origin_id"] == msg["origin_id"] and r["seq"] == msg["seq"] for r in rows)

        run(self.nodes, self.clock, 15, until=bob_sees_it)
        self.assertTrue(bob_sees_it(), "message never reached bob's API")
        row = self.bob.get(f"/api/messages/{msg['origin_id']}/{msg['seq']}").get_json()
        self.assertEqual(row["content"], "bridge out at mile 12")
        self.assertEqual(row["sender"], "alice")

        # Bob's app marked it delivered (a tombstone); the mesh carries that back to alice.
        def alice_knows_delivered():
            r = self.alice.get(f"/api/messages/{msg['origin_id']}/{msg['seq']}").get_json()
            return bool(r and r["is_dead"])

        run(self.nodes, self.clock, 200, until=alice_knows_delivered)  # next sync waits out the cooldown
        self.assertTrue(alice_knows_delivered(), "alice's copy was never marked delivered")
        self.assertEqual(self.delivered, [(msg["origin_id"], msg["seq"])])

    def test_locale_comment_and_position_reach_the_peer(self):
        r = self.alice.post("/api/locales", json={"location": "spring-1203", "name": "Spring at mile 1203",
                                                  "comment": "dry as of noon"})
        self.assertLess(r.status_code, 300, r.get_data(as_text=True))
        r = self.alice.put("/api/location", json={"latitude": 29.65, "longitude": -82.32})
        self.assertLess(r.status_code, 300, r.get_data(as_text=True))

        def bob_has_both():
            comments = self.bob.get("/api/locales").get_json()
            people = self.bob.get("/api/contacts").get_json()
            return any(c["comment"] == "dry as of noon" for c in comments) and any(
                p.get("name", "").lower() == "alice" for p in people
            )

        run(self.nodes, self.clock, 15, until=bob_has_both)
        self.assertTrue(bob_has_both(), "comment or alice's position never reached bob's API")


@unittest.skipIf(Flask is None, "needs Flask and the user app (pip install -r application/user_app/requirements.txt)")
class ConnectionStateTests(unittest.TestCase):
    """GET /api/connection, which colours the Send button: fast / lora / none."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trailmesh-conn-")
        self.clock = SimClock(start=time.time())   # positions in the database use wall-clock time
        self.alice_db, self.alice = make_app(self.tmp, "alice", "00000001")
        self.bob_db, self.bob = make_app(self.tmp, "bob", "00000002")

    def nodes(self, *, lora: bool, espnow: bool):
        """A node per user; `lora` gives each a LoRa link, `espnow` an ESP-NOW one (both = a Heltec)."""
        lora_air = SimMedium(self.clock, airtime=LoRaParams().airtime) if lora else None
        espnow_air = SimMedium(self.clock) if espnow else None
        out = []
        for name, db, client in (("a", self.alice_db, self.alice), ("b", self.bob_db, self.bob)):
            link = lora_air.link(name) if lora else espnow_air.link(name)
            fast = espnow_air.link(name + "-fast") if lora and espnow else None
            node = Node(link, AppStore(db), Config.lora(always_on=True) if lora else Config(always_on=True),
                        clock=self.clock, fast_link=fast, rng=random.Random(name))
            client.application.config["MESH"] = {"port": "sim", "board_kind": "sim", "radio": "auto",
                                                 "board": None, "node": node, "events": collections.deque()}
            out.append(node)
        return out

    def hear_each_other(self, nodes):
        run(nodes, self.clock, 10, until=lambda: all(n.heard for n in nodes))
        self.assertTrue(all(n.heard for n in nodes), "the nodes never heard each other")

    def state(self, client):
        return client.get("/api/connection").get_json()

    def test_nothing_in_reach_is_none(self):
        self.assertEqual(self.state(self.alice)["state"], "none")      # no mesh radio at all
        self.nodes(lora=True, espnow=False)
        self.assertEqual(self.state(self.alice)["state"], "none")      # radio on, nobody heard yet

    def test_far_peer_over_lora_is_lora(self):
        self.alice_db.set_location(29.6516, -82.3248)
        self.bob_db.set_location(29.6600, -82.3300)                    # ~1 km apart
        nodes = self.nodes(lora=True, espnow=True)
        self.hear_each_other(nodes)
        self.bob.post("/api/sync", json={"geolocations": [dict(self.alice_db.list("geolocations")[0])]})
        c = self.state(self.bob)
        self.assertEqual((c["state"], c["via"]), ("lora", "LoRa"))
        self.assertEqual(c["detail"], "a device in LoRa range")  # generic: the button is shared by every chat

    def test_close_peer_on_a_two_radio_board_is_fast_before_any_message(self):
        self.alice_db.set_location(29.65160, -82.32480)
        self.bob_db.set_location(29.65175, -82.32480)                  # ~17 m apart
        self.bob.post("/api/sync", json={"geolocations": [dict(self.alice_db.list("geolocations")[0])]})
        nodes = self.nodes(lora=True, espnow=True)
        self.hear_each_other(nodes)
        c = self.state(self.bob)
        self.assertEqual((c["state"], c["via"]), ("fast", "ESP-NOW"))
        self.assertIn("m away", c["detail"])
        self.assertNotIn("alice", c["detail"])

    def test_peer_heard_on_espnow_is_fast(self):
        nodes = self.nodes(lora=False, espnow=True)
        self.hear_each_other(nodes)
        self.assertEqual(self.state(self.alice)["via"], "ESP-NOW")

    def test_connected_server_is_fast(self):
        self.alice.application.config["SERVER"] = type("Finder", (), {"current": ("http://srv", "wifi")})()
        c = self.state(self.alice)
        self.assertEqual((c["state"], c["via"]), ("fast", "server"))


if __name__ == "__main__":
    unittest.main()
