"""LoRa support (airtime, Config.lora, the 60 m switch to ESP-NOW, HeltecSerialLink) and the Node's peer scheduling
(least recently synced first, position cooldown, failure backoff, change-only re-syncs)."""
import contextlib
import os
import random
import sqlite3
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from app_db import database, new_db  # noqa: E402
from test_serial_link import FakeEsp32Pair  # noqa: E402
from trailmesh import AppStore, Config, Node  # noqa: E402
from trailmesh.espnow_serial import CMD_GET_INFO, EVT_INFO, STATUS  # noqa: E402
from trailmesh.lora import LoRaParams  # noqa: E402
from trailmesh.sim import SimClock, SimMedium, run  # noqa: E402
from trailmesh.wire import INV_ENTRY, Frame, T  # noqa: E402

try:
    import serial  # noqa: F401
    from trailmesh.heltec_serial import CMD_CONFIG, LORA_STATUS, HeltecSerialLink
except ImportError:  # pragma: no cover
    HeltecSerialLink = None

FAST = dict(always_on=True, cooldown=5.0, select_delay=0.05)


class AirtimeTests(unittest.TestCase):
    def test_matches_semtech_calculator(self):
        self.assertAlmostEqual(LoRaParams(bw_hz=125_000, sf=7).airtime(255), 0.3996, places=3)
        self.assertAlmostEqual(LoRaParams(bw_hz=125_000, sf=12).airtime(255), 9.019, places=2)  # LDRO on
        self.assertAlmostEqual(LoRaParams().airtime(250), 0.0973, places=3)

    def test_rejects_settings_the_board_cant_use(self):
        for bad in (dict(sf=6), dict(bw_hz=200_000), dict(freq_hz=433_000_000), dict(power_dbm=30)):
            with self.assertRaises(ValueError):
                LoRaParams(**bad)

    def test_lora_config_scales_with_airtime(self):
        fast, slow = Config.lora(LoRaParams(sf=7)), Config.lora(LoRaParams(sf=9))
        self.assertGreater(fast.rto, fast.window_frames * LoRaParams(sf=7).airtime(250))
        self.assertGreater(slow.rto, fast.rto)
        self.assertGreater(fast.ack_delay, 0)
        self.assertEqual(Config.lora(window_frames=2).window_frames, 2)


@unittest.skipIf(database is None, "the app's database.py isn't available (no checkout or git main)")
class NodeTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.ends = []

    def node(self, medium, name, dev_id, config):
        return Node(medium.link(name), AppStore(new_db(self.dir, name, f"{dev_id:08x}")), config, clock=self.clock,
                    rng=random.Random(dev_id), on_session_end=lambda s: self.ends.append((self.clock.now, dev_id, s)))

    def count_frames(self, nodes):
        frames = []
        for n in nodes:
            send = n.link.send
            n.link.send = lambda p, *a, _s=send, _n=n: (frames.append((_n.id, Frame.decode(p))), _s(p, *a))[1]
        return frames

    def test_lora_exchange_batches_acks(self):
        p = LoRaParams()
        m = SimMedium(self.clock, airtime=p.airtime)
        a, b = (self.node(m, n, i, Config.lora(p, always_on=True)) for i, n in ((1, "alice"), (2, "bob")))
        frames = self.count_frames([a, b])
        loc = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "w" * 3000)
        got = lambda: b.store.db.get("locales", loc["origin_id"], loc["seq"])  # noqa: E731
        run([a, b], self.clock, 60, until=lambda: got() and len(self.ends) == 2)
        self.assertEqual(got()["comment"], loc["comment"])
        reliable = sum(f.type in (T.INV, T.WANT, T.DATA, T.DONE) for _, f in frames)
        acks = sum(f.type is T.ACK for _, f in frames)
        self.assertLess(acks, reliable / 2, "one ACK should cover a burst")

    def test_only_the_named_peer_answers_a_hello(self):
        """LoRa broadcasts everything, so a third device hears the HELLO too and must ignore it."""
        p = LoRaParams()
        m = SimMedium(self.clock, airtime=p.airtime)
        a, b, c = (self.node(m, n, i, Config.lora(p, always_on=True)) for i, n in ((1, "a"), (2, "b"), (3, "c")))
        b.store.db.add_locale("x", "X", "only b has this")
        run([a, b, c], self.clock, 30, until=lambda: len(self.ends) >= 2)
        pairs = {frozenset((dev, s.peer_id)) for _, dev, s in self.ends}
        self.assertEqual(len(pairs), 1, f"one session per pair, got {pairs}")

    def test_prefers_peer_never_synced(self):
        m = SimMedium(self.clock)
        reach = {frozenset((b"a", b"b"))}
        m.in_range = lambda x, y: frozenset((x, y)) in reach
        a, b, c = (self.node(m, n, i, Config(**FAST)) for i, n in ((1, "a"), (2, "b"), (3, "c")))
        b.store.db.add_locale("x", "X", "from b")
        run([a, b, c], self.clock, 10, until=lambda: self.ends)
        run([a, b, c], self.clock, 6)  # past the cooldown
        reach.update({frozenset((b"a", b"c")), frozenset((b"b", b"c"))})
        b.store.db.add_locale("y", "Y", "more from b")  # a is due to sync with b again...
        c.store.db.add_locale("z", "Z", "from c")  # ...but has never met c
        self.ends.clear()
        run([a, b, c], self.clock, 10, until=lambda: any(dev == 1 for _, dev, _ in self.ends))
        first = next(s for _, dev, s in self.ends if dev == 1)
        self.assertEqual(first.peer_id, c.id)

    def test_position_only_change_waits_for_location_cooldown(self):
        m = SimMedium(self.clock)
        cfg = Config(**dict(FAST, cooldown=60.0, location_cooldown=3.0))
        a, b = self.node(m, "a", 1, cfg), self.node(m, "b", 2, cfg)
        b.store.db.add_locale("x", "X", "hi")
        run([a, b], self.clock, 10, until=lambda: self.ends)
        start = self.clock.now
        b.store.db.set_location(29.6, -82.3)
        run([a, b], self.clock, 30, until=lambda: len(self.ends) >= 4)
        later = [t for t, dev, s in self.ends if dev == 1][1]
        self.assertGreaterEqual(later - start, 2.0)
        self.assertLess(later - start, 10.0, "a position-only change shouldn't wait for the 60 s cooldown")

    def test_failures_back_off_exponentially(self):
        m = SimMedium(self.clock)
        cfg = Config(**dict(FAST, retry_backoff=1.0, hello_retries=2))
        a, b = self.node(m, "a", 1, cfg), self.node(m, "b", 2, cfg)
        send = b.link.send
        b.link.send = lambda p, *x: send(p, *x) if Frame.decode(p).type is T.BEACON else None  # b never answers
        b.store.db.add_locale("x", "X", "hi")
        run([a, b], self.clock, 40)
        times = [t for t, dev, s in self.ends if dev == 1]
        gaps = [t2 - t1 for t1, t2 in zip(times, times[1:])]
        self.assertGreaterEqual(len(gaps), 3)
        self.assertGreater(gaps[-1], gaps[0] * 3, f"gaps between failed attempts should grow: {gaps}")

    def test_resync_offers_only_what_changed(self):
        m = SimMedium(self.clock)
        a, b = self.node(m, "a", 1, Config(**FAST)), self.node(m, "b", 2, Config(**FAST))
        for i in range(20):
            a.store.db.add_locale(f"s{i}", f"S{i}", f"c{i}")
        time.sleep(0.05)  # the records are older than the INV by more than the re-sync margin
        run([a, b], self.clock, 10, until=lambda: len(self.ends) == 2)
        frames = self.count_frames([a, b])
        a.store.db.add_locale("new", "New", "one more")
        run([a, b], self.clock, 20, until=lambda: len(self.ends) == 4)
        self.assertTrue(all(s.ok for _, _, s in self.ends))
        inv = {dev: sum(len(f.payload) // INV_ENTRY.size for d, f in frames if d == dev and f.type is T.INV)
               for dev in (1, 2)}
        self.assertEqual(inv, {1: 1, 2: 0}, "neither side re-lists the 20 records from the first sync")

    def test_falls_back_to_full_inventory(self):
        """If a change-only sync moves nothing although digests differ, the next sync lists everything."""
        m = SimMedium(self.clock)
        a, b = self.node(m, "a", 1, Config(**FAST)), self.node(m, "b", 2, Config(**FAST))
        loc = a.store.db.add_locale("x", "X", "hi")
        got = lambda: b.store.db.get("locales", loc["origin_id"], loc["seq"])  # noqa: E731
        run([a, b], self.clock, 10, until=lambda: got() and len(self.ends) == 2)
        with contextlib.closing(sqlite3.connect(b.store.db.path)) as conn, conn:
            conn.execute("DELETE FROM locales")  # b loses the row; only a full INV can bring it back
        run([a, b], self.clock, 60, until=got)
        self.assertIsNotNone(got())


@unittest.skipIf(database is None, "the app's database.py isn't available (no checkout or git main)")
class RadioSwitchTests(unittest.TestCase):
    """Two radios per node: LoRa (airtime model) for discovery, ESP-NOW (fast medium) when within 60 m."""

    def setUp(self):
        self.clock = SimClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        p = LoRaParams()
        self.lora, self.espnow = SimMedium(self.clock, airtime=p.airtime), SimMedium(self.clock)
        self.ends = []
        self.nodes = [
            Node(self.lora.link(name), AppStore(new_db(self.dir, name, f"{i:08x}")),
                 Config.lora(p, always_on=True), clock=self.clock, rng=random.Random(i),
                 fast_link=self.espnow.link(name), on_session_end=self.ends.append)
            for i, name in ((1, "alice"), (2, "bob"))
        ]
        self.espnow_frames = []
        for n in self.nodes:
            send = n.fast_link.send
            n.fast_link.send = lambda payload, *a, _s=send: (self.espnow_frames.append(payload), _s(payload, *a))[1]

    def sync(self, a_pos, b_pos):
        a, b = self.nodes
        a.store.db.set_location(*a_pos)
        b.store.db.set_location(*b_pos)
        loc = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "w" * 2000)
        got = lambda: b.store.db.get("locales", loc["origin_id"], loc["seq"])  # noqa: E731
        run(self.nodes, self.clock, 60, until=lambda: got() and len(self.ends) >= 2)
        self.assertIsNotNone(got())
        return self.ends[0]

    def test_close_peers_exchange_over_espnow(self):
        s = self.sync((29.650000, -82.320000), (29.650200, -82.320000))  # ~22 m apart
        self.assertTrue(s.fast)
        self.assertGreater(len(self.espnow_frames), 5)
        run(self.nodes, self.clock, 5)  # past the finished session's linger
        self.assertFalse(any(n.fast_link.radio_on for n in self.nodes), "ESP-NOW is off again afterwards")

    def test_distant_peers_stay_on_lora(self):
        s = self.sync((29.650000, -82.320000), (29.660000, -82.320000))  # ~1.1 km apart
        self.assertFalse(s.fast)
        self.assertEqual(self.espnow_frames, [])
        a, b = self.nodes
        self.assertEqual(b.store.db.get_location("alice")["latitude"], 29.65, "positions travel over LoRa too")


@unittest.skipIf(HeltecSerialLink is None, "pyserial not installed")
class HeltecSerialLinkTests(unittest.TestCase):
    def test_configures_board_and_recovers_from_reboot(self):
        configs = []

        class FakeLoRa(FakeEsp32Pair):
            def _serve(self, board):
                board["conn"], _ = board["srv"].accept()
                board["connected"].set()
                from trailmesh.espnow_serial import SlipDecoder
                dec = SlipDecoder()
                while not self._stop:
                    try:
                        data = board["conn"].recv(4096)
                    except OSError:
                        return
                    if not data:
                        return
                    for msg in dec.feed(data):
                        if msg[0] == CMD_CONFIG:
                            configs.append(msg[1:])
                        elif msg[0] == 0x03:
                            board["radio"] = bool(msg[1])
                        elif msg[0] == CMD_GET_INFO:
                            st = STATUS.pack(board["radio"], 6, 0, 0, 0, 0, 0, 0)
                            lora = LORA_STATUS.pack(0, 0, 0, 0, 0, 0, 915_000_000, 500_000, 7, 5, 20, 0, 0, 0, 0)
                            self._write(board, bytes([EVT_INFO]) + board["mac"] + st + lora)

        fake = FakeLoRa(ports=(0,))
        params = LoRaParams(sf=9)
        link = HeltecSerialLink(fake.boards[0]["port"], params, log=None)
        try:
            deadline = time.time() + 3
            while link.lora_status is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(configs[0], params.pack(), "CONFIG is sent on connect")
            # The board reports SF7 (as after a reboot), so the link sends our settings again.
            deadline = time.time() + 3
            while len(configs) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(configs), 2)
            self.assertEqual(struct.unpack_from("<IIBBb", configs[1])[2], 9)
        finally:
            link.close()
            fake.close()


if __name__ == "__main__":
    unittest.main()
