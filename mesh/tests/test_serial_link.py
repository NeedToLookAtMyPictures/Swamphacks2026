"""Exercises EspNowSerialLink end to end against a fake ESP32 pair.

Each fake board is a local TCP server, and the link reaches it through pyserial's
"socket://" URL. That runs the real serial code on every OS (pseudo-terminals
would limit the test to Linux and macOS).
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from app_db import database, new_db  # noqa: E402
from trailmesh import AppStore, Config, Node  # noqa: E402
from trailmesh.espnow_serial import (  # noqa: E402
    BROADCAST, CMD_GET_INFO, CMD_RADIO, CMD_SEND, CMD_SET_PARTNER, EVT_INFO, EVT_RECV, SlipDecoder, slip_encode,
)
from trailmesh.wire import MAX_FRAME  # noqa: E402

try:
    import serial  # noqa: F401
    from trailmesh.espnow_serial import EspNowSerialLink
except ImportError:  # pragma: no cover
    EspNowSerialLink = None


class FakeEsp32Pair:
    """Two fake bridges sharing one fake radio channel, mirroring the firmware's behaviour."""

    def __init__(self):
        self.boards = []
        self.frame_sizes = []  # every payload handed to the radio, to check the 250-byte limit
        for i in range(2):
            srv = socket.create_server(("127.0.0.1", 0))
            self.boards.append({"srv": srv, "port": f"socket://127.0.0.1:{srv.getsockname()[1]}",
                                "mac": bytes([2, 0, 0, 0, 0, i]), "partner": 0, "radio": True,
                                "conn": None, "connected": threading.Event()})
        self._stop = False
        self._threads = [threading.Thread(target=self._serve, args=(b,), daemon=True) for b in self.boards]
        for t in self._threads:
            t.start()

    def _write(self, board, msg):
        board["connected"].wait(2)
        board["conn"].sendall(slip_encode(msg))

    def _serve(self, board):
        board["conn"], _ = board["srv"].accept()
        board["connected"].set()
        dec = SlipDecoder()
        while not self._stop:
            try:
                data = board["conn"].recv(4096)
            except OSError:
                return
            if not data:
                return
            for m in dec.feed(data):
                if m[0] == CMD_SEND and board["radio"]:
                    dest, payload = m[1:7], m[7:]
                    self.frame_sizes.append(len(payload))
                    for other in self.boards:
                        if other is board or not other["radio"]:
                            continue
                        if dest != BROADCAST and dest != other["mac"]:
                            continue
                        sender = struct.unpack_from("<I", payload)[0]
                        if other["partner"] and sender != other["partner"]:
                            continue
                        self._write(other, bytes([EVT_RECV]) + board["mac"] + b"\xc4" + payload)
                elif m[0] == CMD_SET_PARTNER:
                    board["partner"] = struct.unpack_from("<I", m, 1)[0]
                elif m[0] == CMD_RADIO:
                    board["radio"] = bool(m[1])
                elif m[0] == CMD_GET_INFO:
                    self._write(board, bytes([EVT_INFO]) + board["mac"])

    def close(self):
        self._stop = True
        for b in self.boards:
            for s in (b["conn"], b["srv"]):
                if s is not None:
                    s.close()


@unittest.skipIf(EspNowSerialLink is None, "pyserial not installed")
@unittest.skipIf(database is None, "the app's database.py isn't available (no checkout or git main)")
class SerialLinkTests(unittest.TestCase):
    def test_slip_roundtrip_with_escapes(self):
        msg = bytes([0xC0, 0xDB, 1, 2, 0xDC, 0xDD, 0xC0])
        self.assertEqual(SlipDecoder().feed(slip_encode(msg) + slip_encode(b"x")), [msg, b"x"])

    def test_two_nodes_over_serial(self):
        fake = FakeEsp32Pair()
        links = [EspNowSerialLink(b["port"], log=None) for b in fake.boards]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        try:
            a = Node(links[0], AppStore(new_db(tmp.name, "alice", "00000001")), Config(always_on=True))
            b = Node(links[1], AppStore(new_db(tmp.name, "bob", "00000002")), Config(always_on=True))
            small = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "dry")
            big = a.store.db.add_locale("spring-1203", "Mile 1203 spring", "w" * 5000)  # 5 KB, ~23 chunks
            got = lambda r: b.store.db.get("locales", r["origin_id"], r["seq"])  # noqa: E731
            deadline = time.time() + 15
            while time.time() < deadline and not (got(small) and got(big)):
                a.poll()
                b.poll()
                time.sleep(0.002)
            self.assertIsNotNone(got(small))
            self.assertEqual(got(big)["comment"], big["comment"])
            self.assertEqual(links[0].mac, fake.boards[0]["mac"])
            self.assertLessEqual(max(fake.frame_sizes), MAX_FRAME)
            self.assertGreater(len(fake.frame_sizes), 5000 // MAX_FRAME, "big record must be split")
        finally:
            for lk in links:
                lk.close()
            fake.close()


if __name__ == "__main__":
    unittest.main()
