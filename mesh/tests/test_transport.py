"""Transport layer: EspNowSerialLink over any pipe, transport selection, and the
Android USB CDC parsing. No hardware, no pyserial and no Termux needed."""
import os
import queue
import struct
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import termux_usb, transport  # noqa: E402
from trailmesh.espnow_serial import (  # noqa: E402
    CMD_GET_INFO,
    CMD_RADIO,
    CMD_SET_PARTNER,
    EVT_INFO,
    EVT_RECV,
    STATUS,
    EspNowSerialLink,
    LinkLost,
    SlipDecoder,
    slip_encode,
)
from trailmesh.transport import Transport, TransportError  # noqa: E402


class FakeBoard(Transport):
    """An in-memory pipe. The test pushes board->host bytes and reads host->board messages."""

    def __init__(self, reconnectable=True):
        self.reconnectable = reconnectable
        self.to_host: queue.Queue = queue.Queue()
        self.from_host: list[bytes] = []
        self._dec = SlipDecoder()
        self.broken = False
        self.closed = False

    def read(self, size):
        if self.broken:
            raise TransportError("unplugged")
        try:
            return self.to_host.get(timeout=0.01)
        except queue.Empty:
            return b""

    def write(self, data):
        if self.broken:
            raise TransportError("unplugged")
        self.from_host += self._dec.feed(data)

    def close(self):
        self.closed = True

    def push(self, msg: bytes):
        self.to_host.put(slip_encode(msg))


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class LinkOverTransport(unittest.TestCase):
    def setUp(self):
        self.boards: list[FakeBoard] = []
        self.reconnectable = True
        self._registry = list(transport._registry)
        transport.register(lambda port: port == "fake", self._open)

    def tearDown(self):
        transport._registry[:] = self._registry

    def _open(self, port, baud):
        b = FakeBoard(self.reconnectable)
        self.boards.append(b)
        return b

    def link(self):
        lk = EspNowSerialLink("fake", log=None, reconnect_interval=0.05)
        self.addCleanup(lk.close)
        return lk

    def test_startup_resets_board_state(self):
        self.link()
        self.assertEqual(
            self.boards[0].from_host,
            [bytes([CMD_SET_PARTNER]) + struct.pack("<I", 0), bytes([CMD_RADIO, 1]), bytes([CMD_GET_INFO])],
        )

    def test_receive_and_info(self):
        lk = self.link()
        b = self.boards[0]
        mac = bytes(range(6))
        b.push(bytes([EVT_INFO]) + mac + STATUS.pack(1, 6, 0, 1, 2, 3, 4, 5))
        b.push(bytes([EVT_RECV]) + mac + struct.pack("b", -40) + b"\x01\x00\x00\x00hello")
        frames = []
        self.assertTrue(wait_for(lambda: frames.extend(lk.poll()) or frames))
        self.assertEqual(frames[0].payload, b"\x01\x00\x00\x00hello")
        self.assertEqual(frames[0].rssi, -40)
        self.assertEqual(lk.mac, mac)
        self.assertEqual(lk.status["channel"], 6)

    def test_reconnects_and_restores_state(self):
        lk = self.link()
        lk.set_partner(7)
        lk.set_radio(False)
        self.boards[0].broken = True
        self.assertTrue(wait_for(lambda: len(self.boards) == 2 and lk.connected))
        self.assertTrue(self.boards[0].closed)
        self.assertEqual(
            self.boards[1].from_host[:2], [bytes([CMD_SET_PARTNER]) + struct.pack("<I", 7), bytes([CMD_RADIO, 0])]
        )
        self.assertEqual(lk.poll(), [])  # never raises on a reconnectable transport

    def test_unreconnectable_transport_reports_lost(self):
        self.reconnectable = False
        lk = self.link()
        b = self.boards[0]
        b.push(bytes([EVT_RECV]) + bytes(6) + b"\x00" + b"\x02\x00\x00\x00x")
        self.assertTrue(wait_for(lambda: not b.to_host.qsize()))
        b.broken = True
        self.assertTrue(wait_for(lambda: lk.lost))
        self.assertEqual(len(lk.poll()), 1)  # frames received before the loss are still delivered
        with self.assertRaises(LinkLost):
            lk.poll()
        lk.send(b"\x00" * 10)  # sending while lost is dropped, not an error
        self.assertEqual(len(self.boards), 1)  # never tried to reopen


class Selection(unittest.TestCase):
    def test_termux_ports(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(termux_usb.matches("termux-usb"))
            self.assertTrue(termux_usb.matches("/dev/bus/usb/001/002"))
            self.assertFalse(termux_usb.matches("auto"))
            self.assertFalse(termux_usb.matches("/dev/ttyACM0"))
            self.assertFalse(termux_usb.matches("COM3"))
        with mock.patch.dict(os.environ, {"TERMUX_VERSION": "0.118"}):
            self.assertTrue(termux_usb.matches("auto"))

    def test_serial_ports_go_to_pyserial(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            transport, "PySerialTransport", side_effect=lambda port, baud: ("pyserial", port)
        ):
            self.assertEqual(transport.open_transport("socket://127.0.0.1:5000", 460800), ("pyserial", "socket://127.0.0.1:5000"))
            self.assertEqual(transport.open_transport("auto", 460800), ("pyserial", "auto"))

    def find_board(self, *ports):
        found = [mock.Mock(device=dev, vid=vid) for dev, vid in ports]
        with mock.patch("serial.tools.list_ports.comports", return_value=found):
            return transport.PySerialTransport._find_board(None)

    def test_auto_finds_board(self):
        self.assertEqual(self.find_board(("/dev/ttyACM0", 0x303A)), "/dev/ttyACM0")
        # a DevKit behind a CP210x / CH340 bridge (/dev/ttyUSB0 on Linux)
        self.assertEqual(self.find_board(("/dev/ttyUSB0", 0x10C4)), "/dev/ttyUSB0")
        self.assertEqual(self.find_board(("/dev/ttyUSB0", 0x1A86)), "/dev/ttyUSB0")
        # a native-USB board wins over an unrelated bridge chip
        self.assertEqual(self.find_board(("/dev/ttyUSB0", 0x0403), ("/dev/ttyACM0", 0x303A)), "/dev/ttyACM0")
        self.assertEqual(self.find_board(("/dev/ttyUSB0", 0x10C4), ("/dev/ttyACM0", 0x239A)), "/dev/ttyACM0")
        with self.assertRaises(transport.TransportError):
            self.find_board(("/dev/ttyS0", None))
        with self.assertRaises(transport.TransportError):
            self.find_board(("/dev/ttyUSB0", 0x10C4), ("/dev/ttyUSB1", 0x1A86))


def interface(num, cls, n_eps):
    return bytes([9, 4, num, 0, n_eps, cls, 0, 0, 0])


def endpoint(addr, attr):
    return bytes([7, 5, addr, attr, 64, 0, 0])


def config(*parts):
    body = b"".join(parts)
    return bytes([9, 2, (9 + len(body)) & 0xFF, (9 + len(body)) >> 8, 2, 1, 0, 0x80, 50]) + body


class CdcDescriptor(unittest.TestCase):
    def test_tinyusb_cdc(self):  # Feather S3 with TinyUSB: IAD, comm (interrupt ep), data (bulk pair)
        iad = bytes([8, 11, 0, 2, 2, 2, 0, 0])
        cs = bytes([5, 0x24, 0, 0x20, 0x01])  # a class-specific header descriptor
        cfg = config(iad, interface(0, 2, 1), cs, endpoint(0x81, 3), interface(1, 10, 2), endpoint(0x02, 2), endpoint(0x82, 2))
        self.assertEqual(termux_usb.find_cdc(cfg), (0, 1, 0x82, 0x02))

    def test_usb_serial_jtag(self):  # generic ESP32-S3 on its built-in USB-Serial/JTAG: CDC plus a vendor JTAG interface
        cfg = config(
            interface(0, 2, 1), endpoint(0x82, 3),
            interface(1, 10, 2), endpoint(0x01, 2), endpoint(0x81, 2),
            interface(2, 0xFF, 2), endpoint(0x02, 2), endpoint(0x83, 2),
        )
        self.assertEqual(termux_usb.find_cdc(cfg), (0, 1, 0x81, 0x01))

    def test_not_cdc(self):
        with self.assertRaises(TransportError):
            termux_usb.find_cdc(config(interface(0, 0xFF, 2), endpoint(0x01, 2), endpoint(0x81, 2)))


class BridgeChips(unittest.TestCase):
    def test_cp2102_bulk_pair(self):  # CP2102: one vendor interface, bulk in/out
        cfg = config(interface(0, 0xFF, 2), endpoint(0x81, 2), endpoint(0x01, 2))
        self.assertEqual(termux_usb.find_bulk(cfg), (0, 0x81, 0x01))

    def test_ch340_skips_interrupt_endpoint(self):  # CH340: bulk out, bulk in, interrupt in
        cfg = config(interface(0, 0xFF, 3), endpoint(0x02, 2), endpoint(0x82, 2), endpoint(0x81, 3))
        self.assertEqual(termux_usb.find_bulk(cfg), (0, 0x82, 0x02))

    def test_no_bulk(self):
        with self.assertRaises(TransportError):
            termux_usb.find_bulk(config(interface(0, 0xFF, 1), endpoint(0x81, 3)))

    def test_ch340_divisors(self):  # values the Linux ch341 driver computes
        self.assertEqual(termux_usb.ch34x_divisor(9600), 0xB202)
        self.assertEqual(termux_usb.ch34x_divisor(115200), 0xCC03)
        self.assertEqual(termux_usb.ch34x_divisor(460800), 0xF303)
        self.assertEqual(termux_usb.ch34x_divisor(921600), 0xF307)


class TermuxRerun(unittest.TestCase):
    def test_reruns_under_termux_usb_and_exits_with_its_code(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return mock.Mock(stdout='["/dev/bus/usb/001/002"]', stderr="")

        with mock.patch.dict(os.environ, {"TERMUX_VERSION": "1"}), \
                mock.patch.object(termux_usb.shutil, "which", return_value="/bin/termux-usb"), \
                mock.patch.object(termux_usb.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(termux_usb.subprocess, "call", return_value=3) as call, \
                mock.patch.object(termux_usb.sys, "orig_argv", ["python", "examples/run_node.py", "--name", "my node"]):
            os.environ.pop(termux_usb.FD_ENV, None)
            with self.assertRaises(SystemExit) as cm:
                termux_usb.TermuxUsbTransport("auto", 460800)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(calls, [["termux-usb", "-l"]])
        args = call.call_args[0][0]
        self.assertEqual(args[:4], ["termux-usb", "-r", "-E", "-e"])
        self.assertTrue(args[4].endswith("examples/run_node.py --name 'my node'"))
        self.assertEqual(args[5], "/dev/bus/usb/001/002")


if __name__ == "__main__":
    unittest.main()
