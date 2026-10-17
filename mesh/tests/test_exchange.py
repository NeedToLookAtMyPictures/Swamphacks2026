"""Run with:  python -m unittest discover -s tests   (from the mesh/ directory)"""
import os
import random
import struct
import sys
import tempfile
import time
import types
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from app_db import database, new_db  # noqa: E402
from trailmesh import AppStore, Config, Kind, Node, Record  # noqa: E402
from trailmesh.records import record_id  # noqa: E402
from trailmesh.session import Session  # noqa: E402
from trailmesh.sim import SimClock, SimMedium, run  # noqa: E402
from trailmesh.wire import DATA_HDR, MAX_FRAME, Frame, T  # noqa: E402

FAST = dict(always_on=True, cooldown=5.0)
DAY = 86_400


@unittest.skipIf(database is None, "the app's database.py isn't available (no checkout or git main)")
class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.delivered = []

    def node(self, medium, name, dev_id, mtu=250, **cfg):
        db = new_db(self.dir, name, f"{dev_id:08x}")
        return Node(medium.link(name, mtu), AppStore(db), Config(**cfg), clock=self.clock,
                    rng=random.Random(dev_id), on_delivered=lambda o, s: self.delivered.append((name, o, s)))

    @staticmethod
    def ids(n):
        return {r.id for r in n.store._shared()}

    @staticmethod
    def message(n, origin_id, seq):
        return n.store.db.get("messages", origin_id, seq)

    def test_two_nodes_converge(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        a.store.db.add_locale("spring-1203", "Mile 1203 spring", "dry as of noon")
        b.store.db.set_location(29.65, -82.32)
        run([a, b], self.clock, 20, until=lambda: self.ids(a) == self.ids(b) and len(self.ids(a)) == 2)
        self.assertEqual(self.ids(a), self.ids(b))
        [loc] = b.store.db.list("locales")
        self.assertEqual(loc["comment"], "dry as of noon")
        self.assertEqual(a.store.db.get_location("bob")["latitude"], 29.65)

    def test_delivered_tombstone_kills_message_and_reaches_sender(self):
        """A writes to carol; B relays; C's 'delivered' tombstone spreads back through B to A."""
        m = SimMedium(self.clock)
        reach = {frozenset((b"alice", b"bob"))}
        m.in_range = lambda x, y: frozenset((x, y)) in reach
        a, b, c = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST), self.node(m, "carol", 3, **FAST)
        msg = a.store.db.add_message("carol", "meet at the shelter")
        key = (msg["origin_id"], msg["seq"])
        run([a, b, c], self.clock, 20, until=lambda: self.message(b, *key) is not None)
        self.assertIsNotNone(self.message(b, *key), "B relays the message")

        reach.clear()
        reach.add(frozenset((b"bob", b"carol")))  # B walks on and meets C
        run([a, b, c], self.clock, 60, until=lambda: self.message(b, *key)["is_dead"])
        self.assertEqual(self.message(c, *key)["content"], "meet at the shelter")
        self.assertTrue(self.message(c, *key)["is_dead"], "C tombstoned its own message")
        self.assertTrue(self.message(b, *key)["is_dead"], "C's tombstone reached B")

        reach.clear()
        reach.add(frozenset((b"alice", b"bob")))  # B meets A again
        run([a, b, c], self.clock, 60, until=lambda: bool(self.delivered))
        self.assertEqual(self.delivered, [("alice", *key)])
        self.assertTrue(self.message(a, *key)["is_dead"])
        dead = record_id(Kind.MESSAGE, *key)
        for n in (a, b, c):
            self.assertNotIn(dead, [rid for rid, _, _ in n.store.inventory(0)], "dead messages aren't relayed")

    def test_server_tombstone_kills_message_without_delivery(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        msg = a.store.db.add_message("carol", "hello")
        key = (msg["origin_id"], msg["seq"])
        b.store.db.merge({"tombstones": [dict(origin_id="server", seq=1, target_origin_id=key[0],
                                              target_seq=key[1], time_created=time.time(), reason="revoked")]})
        run([a, b], self.clock, 30, until=lambda: a.store.db.list("tombstones"))
        self.assertTrue(self.message(a, *key)["is_dead"])
        self.assertIsNone(self.message(b, *key), "B never takes the dead message")
        self.assertEqual(self.delivered, [], "a revoked message wasn't delivered")

    def test_broadcast_message_is_never_tombstoned(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        msg = a.store.db.add_message(database.BROADCAST, "bear at the shelter")
        run([a, b], self.clock, 20, until=lambda: self.message(b, msg["origin_id"], msg["seq"]) is not None)
        self.assertFalse(self.message(b, msg["origin_id"], msg["seq"])["is_dead"])
        self.assertEqual(b.store.db.list("tombstones"), [])

    def test_expired_message_is_not_offered(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        msg = a.store.db.add_message("carol", "old news")
        later = time.time() + 32 * DAY
        with mock.patch.object(database, "time", types.SimpleNamespace(time=lambda: later)):
            self.assertEqual(a.store.inventory(0), [])
            self.assertTrue(self.message(a, msg["origin_id"], msg["seq"])["is_dead"], "sender keeps it, dead")
            run([a, b], self.clock, 5)
        self.assertIsNone(self.message(b, msg["origin_id"], msg["seq"]))

    def test_large_record_is_chunked_over_lossy_link(self):
        m = SimMedium(self.clock, loss=0.3, seed=1)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        text = random.Random(0).randbytes(10_000).hex()  # 20 KB of text
        loc = a.store.db.add_locale("spring-1203", "Mile 1203 spring", text)
        run([a, b], self.clock, 60, until=lambda: b.store.db.get("locales", loc["origin_id"], loc["seq"]))
        self.assertEqual(b.store.db.get("locales", loc["origin_id"], loc["seq"])["comment"], text)

    def test_1mb_record_is_split_into_250_byte_frames(self):
        m = SimMedium(self.clock)
        # The links would accept 1500-byte frames; the session must still cap them at 250.
        a, b = self.node(m, "alice", 1, mtu=1500, **FAST), self.node(m, "bob", 2, mtu=1500, **FAST)
        sizes = []
        for n in (a, b):
            send = n.link.send
            n.link.send = lambda payload, *args, _send=send: (sizes.append(len(payload)), _send(payload, *args))[1]
        text = "x" * 1_000_000
        msg = a.store.db.add_message("carol", text)
        run([a, b], self.clock, 600, until=lambda: self.message(b, msg["origin_id"], msg["seq"]) is not None)
        self.assertEqual(self.message(b, msg["origin_id"], msg["seq"])["content"], text)
        self.assertLessEqual(max(sizes), MAX_FRAME)
        self.assertGreater(len(sizes), 1_000_000 // MAX_FRAME)

    def test_gossip_carries_data_across_hops(self):
        m = SimMedium(self.clock)
        reach = {frozenset((b"alice", b"bob"))}
        m.in_range = lambda x, y: frozenset((x, y)) in reach
        a, b, c = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST), self.node(m, "carol", 3, **FAST)
        loc = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "dry")
        has = lambda n: n.store.db.get("locales", loc["origin_id"], loc["seq"]) is not None  # noqa: E731
        run([a, b, c], self.clock, 10, until=lambda: has(b))
        self.assertFalse(has(c))
        reach.clear()
        reach.add(frozenset((b"bob", b"carol")))  # B walks on and meets C; A is long gone
        run([a, b, c], self.clock, 10, until=lambda: has(c))
        self.assertTrue(has(c))

    def test_cutoff_skips_records_created_before_last_server_sync(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        now = time.time()
        row = dict(origin_id="0000000a", location="spring-1203", name="Mile 1203 spring", sender="dave")
        a.store.db.merge({"locales": [dict(row, seq=1, time_created=now - 500, comment="old"),
                                      dict(row, seq=2, time_created=now - 50, comment="new")]})
        a.store.last_server_sync = now - 10
        b.store.last_server_sync = now - 100  # cutoff = now - 100
        run([a, b], self.clock, 10, until=lambda: b.store.db.list("locales"))
        run([a, b], self.clock, 2)
        self.assertEqual([r["comment"] for r in b.store.db.list("locales")], ["new"],
                         "both devices got the older one from the server")

    def test_newest_position_wins(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        now = time.time()
        a.store.db.merge({"geolocations": [dict(user="dave", origin_id="0000000a", time_created=now,
                                                latitude=29.7, longitude=-82.3)]})
        b.store.db.merge({"geolocations": [dict(user="dave", origin_id="0000000a", time_created=now - 60,
                                                latitude=29.6, longitude=-82.4)]})
        run([a, b], self.clock, 10, until=lambda: b.store.db.get_location("dave")["latitude"] == 29.7)
        for n in (a, b):
            self.assertEqual(n.store.db.get_location("dave")["latitude"], 29.7)

    def test_walk_out_of_range_mid_transfer_then_resume(self):
        m = SimMedium(self.clock)
        in_range = [True]
        m.in_range = lambda x, y: in_range[0]
        ends = []
        a = self.node(m, "alice", 1, **dict(FAST, retry_backoff=1.0))
        b = self.node(m, "bob", 2, **dict(FAST, retry_backoff=1.0))
        b.on_session_end = ends.append
        urgent = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "dry")
        bulk = a.store.db.add_message("carol", "x" * 200_000)
        got = lambda rec, table: b.store.db.get(table, rec["origin_id"], rec["seq"]) is not None  # noqa: E731
        run([a, b], self.clock, 10, until=lambda: got(urgent, "locales"))
        in_range[0] = False
        run([a, b], self.clock, 10, until=lambda: bool(ends))
        self.assertTrue(got(urgent, "locales"), "high-priority record should land before the bulk message")
        self.assertFalse(got(bulk, "messages"))
        self.assertFalse(ends[0].ok)
        in_range[0] = True
        run([a, b], self.clock, 120, until=lambda: got(bulk, "messages"))
        self.assertTrue(got(bulk, "messages"))

    def test_duty_cycled_nodes_meet_in_shared_window(self):
        m = SimMedium(self.clock)
        a = self.node(m, "alice", 1, period=20, window=2)
        b = self.node(m, "bob", 2, period=20, window=2)
        msg = a.store.db.add_message("bob", "hi")
        elapsed = run([a, b], self.clock, 60, until=lambda: self.message(b, msg["origin_id"], msg["seq"]))
        self.assertIsNotNone(self.message(b, msg["origin_id"], msg["seq"]))
        self.assertLessEqual(elapsed, 25)

    def test_identical_sets_skip_exchange(self):
        m = SimMedium(self.clock)
        sessions = []
        a, b = self.node(m, "alice", 1, **FAST), self.node(m, "bob", 2, **FAST)
        a.on_session_end = b.on_session_end = sessions.append
        row = dict(origin_id="0000000a", seq=1, time_created=time.time(), location="spring-1203",
                   name="Mile 1203 spring", sender="dave", comment="dry")
        for n in (a, b):
            n.store.db.merge({"locales": [row]})
        run([a, b], self.clock, 10)
        self.assertEqual(sessions, [])

    def test_frames_fit_lora_sized_mtu(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "alice", 1, mtu=100, **FAST), self.node(m, "bob", 2, mtu=100, **FAST)
        for i in range(30):
            a.store.db.add_locale(f"site-{i}", f"Site {i}", f"comment {i}" * 10)
        run([a, b], self.clock, 30, until=lambda: self.ids(a) == self.ids(b))
        self.assertEqual(self.ids(a), self.ids(b))

    def test_malformed_chunks_are_dropped(self):
        store = AppStore(new_db(self.dir, "bob", "00000002"))
        rec = Record(Kind.LOCALE, dict(origin_id="0000000a", seq=1, time_created=1.0, location="x", name="x",
                                       sender="dave", comment="ok"))
        enc = rec.encode()
        good_crc = zlib.crc32(enc).to_bytes(4, "little")
        other = record_id(Kind.LOCALE, "0000000a", 2)
        s = Session(my_id=2, peer_id=1, session_id=7, initiator=False, store=store,
                    config=Config(**FAST), mtu=250, send=lambda f: None, now=0, peer_sync=0)
        s._wanted = {b"\x01" * 8, rec.id, other}
        bad = [
            DATA_HDR.pack(b"\x01" * 8, 5, 2) + b"x",  # index past the count
            DATA_HDR.pack(b"\x01" * 8, 0, 3) + b"x",  # count disagrees with the next frame
            DATA_HDR.pack(b"\x01" * 8, 1, 2) + b"x",
            DATA_HDR.pack(b"\x01" * 8, 0, 2) + b"\x00",  # completes, but too short for a CRC
            DATA_HDR.pack(rec.id, 0, 1) + b"\x00\x00\x00\x00" + enc,  # wrong CRC
            DATA_HDR.pack(other, 0, 1) + good_crc + enc,  # valid record, but not the one asked for
        ]
        for seq, payload in enumerate(bad):
            s.on_frame(Frame(1, T.DATA, session=7, seq=seq, payload=payload), now=0)
        self.assertEqual(s.records_received, 0)
        self.assertEqual(store.db.list("locales"), [])
        self.assertFalse(s.finished)


class RecordTests(unittest.TestCase):
    def test_every_kind_round_trips(self):
        rows = [
            Record(Kind.LOCALE, dict(origin_id="0000000a", seq=7, time_created=1_760_000_000.25,
                                     location="spring-1203", name="Mile 1203 spring", sender="dave",
                                     comment="trocken, ☀️")),
            Record(Kind.MESSAGE, dict(origin_id="0000000a", seq=8, time_created=1.5, sender="dave",
                                      receiver="*", is_dead=0, content="hi")),
            Record(Kind.GEOLOCATION, dict(user="Dave", origin_id="0000000a", time_created=2.5,
                                          latitude=29.65, longitude=-82.32)),
            Record(Kind.TOMBSTONE, dict(origin_id="0000000b", seq=1, target_origin_id="0000000a",
                                        target_seq=8, time_created=3.5, reason="delivered")),
        ]
        for rec in rows:
            back = Record.decode(rec.encode())
            self.assertEqual(back, rec)
            self.assertEqual(back.id, rec.id)

    def test_id_ignores_is_dead(self):
        row = dict(origin_id="0000000a", seq=8, time_created=1.5, sender="dave", receiver="bob", content="hi")
        self.assertEqual(Record(Kind.MESSAGE, dict(row, is_dead=0)).id, Record(Kind.MESSAGE, dict(row, is_dead=1)).id)

    def test_truncated_record_is_rejected(self):
        enc = Record(Kind.MESSAGE, dict(origin_id="a", seq=1, time_created=1.0, sender="d", receiver="b",
                                        is_dead=0, content="hello")).encode()
        for bad in (enc[:-1], enc + b"x", b"", b"\x09" + enc[1:]):
            with self.assertRaises((ValueError, struct.error)):
                Record.decode(bad)


if __name__ == "__main__":
    unittest.main()
