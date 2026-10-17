"""Run with:  python -m unittest discover -s tests   (from the mesh/ directory)"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import Config, Kind, Node, Record, RecordStore  # noqa: E402
from trailmesh.session import Session  # noqa: E402
from trailmesh.sim import SimClock, SimMedium, run  # noqa: E402
from trailmesh.wire import DATA_HDR, MAX_FRAME, Frame, T  # noqa: E402

FAST = dict(always_on=True)


def make(medium, clock, dev_id, name, **cfg):
    return Node(dev_id, medium.link(name), RecordStore(), Config(**cfg), clock=clock, rng=random.Random(dev_id))


def ids(node):
    return {r.id for r in node.store.all()}


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimClock()
        self.t0 = int(self.clock.now)

    def test_two_nodes_converge(self):
        m = SimMedium(self.clock)
        a, b = make(m, self.clock, 1, "a", **FAST), make(m, self.clock, 2, "b", **FAST)
        a.post(Record.locale_update(1, self.t0, "spring-1203", "water", "dry"))
        a.post(Record.message(1, self.t0, "bear at shelter", 34.1, -116.9))
        b.post(Record.pin(2, self.t0, 34.2, -116.8, 90))
        run([a, b], self.clock, 10, until=lambda: ids(a) == ids(b) and len(ids(a)) == 3)
        self.assertEqual(ids(a), ids(b))
        self.assertEqual(len(ids(a)), 3)

    def test_large_record_is_chunked_over_lossy_link(self):
        m = SimMedium(self.clock, loss=0.3, seed=1)
        a, b = make(m, self.clock, 1, "a", **FAST), make(m, self.clock, 2, "b", **FAST)
        big = Record(Kind.MESSAGE, 1, self.t0, bytes(random.Random(0).randbytes(20_000)))
        a.post(big)
        run([a, b], self.clock, 60, until=lambda: b.store.has(big.id))
        self.assertEqual(b.store.get(big.id).body, big.body)

    def test_1mb_record_is_split_into_250_byte_frames(self):
        m = SimMedium(self.clock)
        # The link would accept 1500-byte frames; the session must still cap them at 250.
        a = make(m, self.clock, 1, "a", **FAST)
        b = make(m, self.clock, 2, "b", **FAST)
        a.link.mtu = b.link.mtu = 1500
        sizes = []
        for n in (a, b):
            send = n.link.send
            n.link.send = lambda payload, *args, _send=send: (sizes.append(len(payload)), _send(payload, *args))[1]
        big = Record(Kind.MESSAGE, 1, self.t0, random.Random(0).randbytes(1_000_000))
        a.post(big)
        run([a, b], self.clock, 600, until=lambda: b.store.has(big.id))
        self.assertEqual(b.store.get(big.id).body, big.body)
        self.assertLessEqual(max(sizes), MAX_FRAME)
        self.assertGreater(len(sizes), 1_000_000 // MAX_FRAME)

    def test_malformed_chunks_are_dropped(self):
        rid = b"\x01" * 8
        s = Session(my_id=2, peer_id=1, session_id=7, initiator=False, store=RecordStore(),
                    config=Config(**FAST), mtu=250, send=lambda f: None, now=0, peer_sync=0)
        s._wanted = {rid}
        bad = [
            DATA_HDR.pack(rid, 5, 2) + b"x",  # index past the count
            DATA_HDR.pack(rid, 0, 3) + b"x",  # count disagrees with the next frame
            DATA_HDR.pack(rid, 1, 2) + b"x",
            DATA_HDR.pack(rid, 0, 2) + b"\x00",  # completes the record, but it doesn't decode
        ]
        for seq, payload in enumerate(bad):
            s.on_frame(Frame(1, T.DATA, session=7, seq=seq, payload=payload), now=0)
        self.assertEqual(s.records_received, 0)
        self.assertFalse(s.finished)

    def test_key_over_255_bytes_is_rejected(self):
        with self.assertRaises(ValueError):
            Record(Kind.LOCALE_UPDATE, 1, self.t0, b"dry", b"k" * 256)

    def test_gossip_carries_data_across_hops(self):
        m = SimMedium(self.clock)
        reach = {frozenset((b"a", b"b"))}
        m.in_range = lambda x, y: frozenset((x, y)) in reach
        a, b, c = (make(m, self.clock, i, n, **FAST) for i, n in ((1, "a"), (2, "b"), (3, "c")))
        rec = Record.locale_update(1, self.t0, "spring-1203", "water", "dry")
        a.post(rec)
        run([a, b, c], self.clock, 10, until=lambda: b.store.has(rec.id))
        self.assertFalse(c.store.has(rec.id))
        reach.clear()
        reach.add(frozenset((b"b", b"c")))  # B walks on and meets C; A is long gone
        run([a, b, c], self.clock, 10, until=lambda: c.store.has(rec.id))
        self.assertTrue(c.store.has(rec.id))

    def test_cutoff_skips_records_both_got_from_server(self):
        m = SimMedium(self.clock)
        a, b = make(m, self.clock, 1, "a", **FAST), make(m, self.clock, 2, "b", **FAST)
        old = Record.message(9, self.t0 - 500, "old news", 0, 0, )
        old = Record(old.kind, old.origin, old.created, old.body, server_time=self.t0 - 400)
        late = Record(Kind.MESSAGE, 9, self.t0 - 500, b"written offline, uploaded later", server_time=self.t0 - 50)
        a.post(old)
        a.post(late)
        a.store.last_server_sync = self.t0 - 10
        b.store.last_server_sync = self.t0 - 100  # cutoff = t0-100
        run([a, b], self.clock, 10, until=lambda: b.store.has(late.id))
        run([a, b], self.clock, 2)
        self.assertTrue(b.store.has(late.id), "server_time after cutoff must be exchanged even if created before")
        self.assertFalse(b.store.has(old.id), "server had it before cutoff, so B is assumed to have it")

    def test_newer_locale_update_supersedes_older(self):
        m = SimMedium(self.clock)
        a, b = make(m, self.clock, 1, "a", **FAST), make(m, self.clock, 2, "b", **FAST)
        older = Record.locale_update(2, self.t0 - 100, "spring-1203", "water", "flowing")
        newer = Record.locale_update(1, self.t0, "spring-1203", "water", "dry")
        b.post(older)
        a.post(newer)
        run([a, b], self.clock, 10, until=lambda: ids(a) == ids(b))
        for n in (a, b):
            [r] = n.store.all(Kind.LOCALE_UPDATE)
            self.assertEqual(r.text(), "dry")

    def test_walk_out_of_range_mid_transfer_then_resume(self):
        m = SimMedium(self.clock)
        in_range = [True]
        m.in_range = lambda x, y: in_range[0]
        ends = []
        cfg = dict(FAST, retry_backoff=1.0)
        a = make(m, self.clock, 1, "a", **cfg)
        b = Node(2, m.link("b"), RecordStore(), Config(**cfg), clock=self.clock, on_session_end=ends.append)
        urgent = Record.locale_update(1, self.t0, "spring-1203", "water", "dry")
        bulk = Record(Kind.MESSAGE, 1, self.t0, bytes(200_000))
        a.post(urgent)
        a.post(bulk)
        run([a, b], self.clock, 10, until=lambda: b.store.has(urgent.id))
        in_range[0] = False
        run([a, b], self.clock, 10, until=lambda: bool(ends))
        self.assertTrue(b.store.has(urgent.id), "high-priority record should land before the bulk message")
        self.assertFalse(b.store.has(bulk.id))
        self.assertFalse(ends[0].ok)
        in_range[0] = True
        run([a, b], self.clock, 120, until=lambda: b.store.has(bulk.id))
        self.assertTrue(b.store.has(bulk.id))

    def test_duty_cycled_nodes_meet_in_shared_window(self):
        m = SimMedium(self.clock)
        a = make(m, self.clock, 1, "a", period=20, window=2)
        b = make(m, self.clock, 2, "b", period=20, window=2)
        rec = Record.message(1, self.t0, "hi", 0, 0)
        a.post(rec)
        elapsed = run([a, b], self.clock, 60, until=lambda: b.store.has(rec.id))
        self.assertTrue(b.store.has(rec.id))
        self.assertLessEqual(elapsed, 25)

    def test_identical_sets_skip_exchange(self):
        m = SimMedium(self.clock)
        sessions = []
        a = Node(1, m.link("a"), RecordStore(), Config(**FAST), clock=self.clock, on_session_end=sessions.append)
        b = Node(2, m.link("b"), RecordStore(), Config(**FAST), clock=self.clock, on_session_end=sessions.append)
        rec = Record.message(1, self.t0, "hi", 0, 0)
        a.post(rec)
        b.post(rec)
        run([a, b], self.clock, 10)
        self.assertEqual(sessions, [])

    def test_frames_fit_lora_sized_mtu(self):
        m = SimMedium(self.clock)
        a = Node(1, m.link("a", mtu=100), RecordStore(), Config(**FAST), clock=self.clock)
        b = Node(2, m.link("b", mtu=100), RecordStore(), Config(**FAST), clock=self.clock)
        for i in range(30):
            a.post(Record.message(1, self.t0 + i, f"msg {i}" * 10, 0, 0))
        run([a, b], self.clock, 20, until=lambda: ids(a) == ids(b))
        self.assertEqual(ids(a), ids(b))


if __name__ == "__main__":
    unittest.main()
