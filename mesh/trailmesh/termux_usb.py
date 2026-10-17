"""Android transport: the board's USB serial port, driven through Termux:API.

Android apps can't open serial ports (there is no /dev/ttyACM0 without root).
What Termux offers instead is `termux-usb`: after the user allows it, it starts
a program and hands it an open file descriptor for the USB device. This module
wraps that descriptor with libusb and speaks the USB serial protocol itself, so
the firmware doesn't change. Two kinds of board are supported:

  * USB CDC-ACM, the standard class: ESP32-S3 boards on their native USB
    (Adafruit Feather S3, generic ESP32-S3FN8 boards).
  * Silicon Labs CP210x USB-UART chips, which use their own vendor protocol:
    Heltec WiFi LoRa 32 V3 and most other boards with a CP2102 next to the USB port.

Getting the descriptor is automatic: if this program wasn't started by
termux-usb, open() reruns it through `termux-usb -r -E -e <this command>`
(Android may show the "allow access" dialog), waits for it, and exits with its
exit code. The user just runs the script as usual.

Only one board per program run, and no reconnecting: Android grants USB access
to a newly started program, not to one that is already running. If the board
is unplugged, the link reports it lost and the program has to be started again.

Needs (in Termux): pkg install python termux-api libusb, plus the Termux:API
app installed from the same place as Termux (F-Droid or GitHub; the Play Store
builds don't match).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading

from .transport import Transport, TransportError, on_termux

FD_ENV = "TERMUX_USB_FD"  # set by `termux-usb -E` for the program it starts

# libusb constants
OPTION_NO_DEVICE_DISCOVERY = 2  # named LIBUSB_OPTION_WEAK_AUTHORITY before libusb 1.0.26; same value
ERROR_TIMEOUT = -7
ERROR_INTERRUPTED = -10

# USB / CDC-ACM constants
DESC_DEVICE, DESC_CONFIGURATION, DESC_INTERFACE, DESC_ENDPOINT = 1, 2, 4, 5
CLASS_CDC_COMM, CLASS_CDC_DATA, CLASS_VENDOR = 0x02, 0x0A, 0xFF
EP_DIR_IN, EP_TYPE_MASK, EP_TYPE_BULK = 0x80, 0x03, 0x02
REQ_GET_DESCRIPTOR, REQ_SET_LINE_CODING, REQ_SET_CONTROL_LINE_STATE = 0x06, 0x20, 0x22
DEVICE_TO_HOST_STANDARD, HOST_TO_DEVICE_CLASS_INTERFACE = 0x80, 0x21
LINE_DTR = 0x0001  # DTR on, RTS off. Arduino-ESP32's TinyUSB serial only sends to the host
#                    while DTR is on, and the toggle sequences that reboot the board all use RTS.

# CP210x (Silicon Labs) vendor requests, from the CP210x interface spec (AN571)
VID_SILABS = 0x10C4
HOST_TO_DEVICE_VENDOR_INTERFACE = 0x41
CP_IFC_ENABLE, CP_SET_LINE_CTL, CP_SET_MHS, CP_SET_BAUDRATE = 0x00, 0x03, 0x07, 0x1E
CP_LINE_8N1 = 0x0800  # 8 data bits, no parity, 1 stop bit
CP_MHS_RUN = 0x0300  # DTR and RTS both off (both mask bits set). Boards like the Heltec V3 wire
#                      DTR/RTS to EN and IO0 for auto-flashing; with both off the board just runs.

READ_TIMEOUT_MS = 50  # matches the 50 ms pyserial read timeout the link expects
WRITE_TIMEOUT_MS = 2000
CTRL_TIMEOUT_MS = 1000
READ_SIZE = 512  # a multiple of the 64-byte full-speed packet size, so reads never overflow
#                 (smaller read sizes are rounded up to one 64-byte packet)

_fd_taken = False  # a program run only ever gets one device from termux-usb


def matches(port: str) -> bool:
    return port == "termux-usb" or port.startswith("/dev/bus/usb/") or (port == "auto" and on_termux())


# Getting the device from Termux ---------------------------------------------------


def _termux_usb(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    if shutil.which("termux-usb") is None:
        raise TransportError("termux-usb not found: run `pkg install termux-api` and install the Termux:API app")
    try:
        return subprocess.run(["termux-usb", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # termux-api commands hang when the Termux:API app is missing or from a different source.
        raise TransportError(
            "termux-usb didn't answer: is the Termux:API app installed, from the same place as Termux?"
        ) from e


def _pick_device(port: str) -> str:
    if port.startswith("/dev/bus/usb/"):
        return port
    out = _termux_usb("-l", timeout=15)
    try:
        devices = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as e:
        raise TransportError(f"unexpected output from termux-usb -l: {out.stdout!r} {out.stderr!r}") from e
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise TransportError("no USB device found: plug the board into the phone (USB-C OTG)")
    # Android won't tell us vendor ids before permission is granted, so we can't pick.
    raise TransportError(f"several USB devices found ({', '.join(devices)}); pass the board's path as the port")


def _rerun_with_device(port: str) -> None:
    """Run this same command again under termux-usb, then exit with its exit code."""
    device = _pick_device(port)
    # orig_argv keeps the interpreter options and "-m module" as they were typed.
    command = shlex.join([sys.executable, *sys.orig_argv[1:]])
    print(f"[link] asking Android for access to {device} (accept the dialog if it appears)", file=sys.stderr)
    # The rerun shares this terminal and gets ctrl-c itself; this process only waits for it.
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        code = subprocess.call(["termux-usb", "-r", "-E", "-e", command, device])
    finally:
        signal.signal(signal.SIGINT, old)
    raise SystemExit(code)


# libusb ---------------------------------------------------------------------------


def _load_libusb():
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    try:
        found = ctypes.util.find_library("usb-1.0")
    except Exception:
        found = None
    for name in (found, os.path.join(prefix, "lib", "libusb-1.0.so"), "libusb-1.0.so", "libusb-1.0.so.0"):
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    else:
        raise TransportError("libusb not found: run `pkg install libusb`")

    P, I, U8, U16, UI = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint
    sigs = {
        # libusb_set_option is variadic: no argtypes, called with its two fixed arguments only
        "libusb_set_option": (None, I),
        "libusb_init": ([ctypes.POINTER(P)], I),
        "libusb_exit": ([P], None),
        "libusb_wrap_sys_device": ([P, ctypes.c_ssize_t, ctypes.POINTER(P)], I),  # sys_dev is intptr_t
        "libusb_close": ([P], None),
        "libusb_detach_kernel_driver": ([P, I], I),
        "libusb_claim_interface": ([P, I], I),
        "libusb_release_interface": ([P, I], I),
        "libusb_clear_halt": ([P, U8], I),
        "libusb_control_transfer": ([P, U8, U8, U16, U16, P, U16, UI], I),
        "libusb_bulk_transfer": ([P, U8, P, I, ctypes.POINTER(I), UI], I),
        "libusb_error_name": ([I], ctypes.c_char_p),
    }
    for fn, (args, res) in sigs.items():
        f = getattr(lib, fn)
        if args is not None:
            f.argtypes = args
        f.restype = res
    return lib


def _scan(config: bytes) -> tuple[dict[int, int], dict[int, dict[str, int]]]:
    """From a raw configuration descriptor: {interface: class} and
    {interface: {"in": addr, "out": addr}} for its bulk endpoints."""
    iface_class: dict[int, int] = {}
    eps: dict[int, dict[str, int]] = {}
    current = None
    i = 0
    while i + 2 <= len(config):
        length, kind = config[i], config[i + 1]
        if length < 2:
            break
        body = config[i : i + length]
        if kind == DESC_INTERFACE and length >= 9:
            current = body[2]
            iface_class.setdefault(current, body[5])
        elif kind == DESC_ENDPOINT and length >= 7 and current is not None:
            if body[3] & EP_TYPE_MASK == EP_TYPE_BULK:
                eps.setdefault(current, {})["in" if body[2] & EP_DIR_IN else "out"] = body[2]
        i += length
    return iface_class, eps


def _bulk_pair(iface_class, eps, cls) -> tuple[int, int, int] | None:
    """(interface, bulk IN, bulk OUT) of the first interface of class `cls` with both endpoints."""
    for iface, found in eps.items():
        if iface_class.get(iface) == cls and "in" in found and "out" in found:
            return iface, found["in"], found["out"]
    return None


def find_cdc(config: bytes) -> tuple[int | None, int, int, int]:
    """(comm interface or None, data interface, bulk IN endpoint, bulk OUT endpoint)
    from a raw configuration descriptor. Raises TransportError if there's no CDC data interface."""
    iface_class, eps = _scan(config)
    pair = _bulk_pair(iface_class, eps, CLASS_CDC_DATA)
    if pair is None:
        raise TransportError("the USB device has no CDC serial interface")
    comm = next((i for i, c in iface_class.items() if c == CLASS_CDC_COMM), None)
    return (comm, *pair)


def find_serial(vid: int, config: bytes) -> tuple[str, int | None, int, int, int]:
    """(kind, control interface or None, data interface, bulk IN, bulk OUT), where kind is
    "cdc" or "cp210x". Raises TransportError for any other USB device."""
    try:
        return ("cdc", *find_cdc(config))
    except TransportError:
        pass
    if vid == VID_SILABS:
        iface_class, eps = _scan(config)
        pair = _bulk_pair(iface_class, eps, CLASS_VENDOR)
        if pair is not None:
            return ("cp210x", pair[0], *pair)  # one interface takes both the requests and the data
    raise TransportError(
        f"the USB device (vendor 0x{vid:04x}) is not a supported serial port: expected an ESP32-S3 on its "
        "native USB (CDC) or a board with a Silicon Labs CP210x USB-UART chip (0x10c4)"
    )


class TermuxUsbTransport(Transport):
    """USB serial (CDC-ACM or CP210x) over a file descriptor from termux-usb."""

    reconnectable = False

    def __init__(self, port: str, baud: int):
        global _fd_taken
        fd = os.environ.pop(FD_ENV, None)  # popped: programs we start must not reuse it
        if fd is None:
            if _fd_taken:
                raise TransportError("Android gives a program access to one board; it can't be reopened. Restart it.")
            _rerun_with_device(port)  # doesn't return
        _fd_taken = True
        self.port = port
        self._fd = int(fd)
        self._lib = lib = _load_libusb()
        self._ctx = ctypes.c_void_p()
        self._handle = ctypes.c_void_p()
        self._claimed: list[int] = []
        self._cv = threading.Condition()
        self._busy = 0  # transfers in progress; close() waits for them
        self._closed = False
        self._rxbuf = ctypes.create_string_buffer(READ_SIZE)
        try:
            # Without root, libusb can't scan /dev/bus/usb; it only uses the descriptor we give it.
            lib.libusb_set_option(None, OPTION_NO_DEVICE_DISCOVERY)
            self._check(lib.libusb_init(ctypes.byref(self._ctx)), "libusb_init")
            self._check(
                lib.libusb_wrap_sys_device(self._ctx, self._fd, ctypes.byref(self._handle)), "libusb_wrap_sys_device"
            )
            kind, comm, data, self._ep_in, self._ep_out = find_serial(self._vendor_id(), self._config_descriptor())
            for iface in dict.fromkeys((comm, data)):  # CP210x: comm and data are the same interface
                if iface is None:
                    continue
                lib.libusb_detach_kernel_driver(self._handle, iface)  # fails harmlessly if none is attached
                self._check(lib.libusb_claim_interface(self._handle, iface), f"claim interface {iface}")
                self._claimed.append(iface)
            for ep in (self._ep_in, self._ep_out):
                lib.libusb_clear_halt(self._handle, ep)  # reset data toggles left by a previous run
            if kind == "cp210x":
                self._cp210x_setup(comm, baud)
            elif comm is not None:
                if baud == 1200:
                    raise TransportError("1200 baud reboots Arduino-ESP32 boards into the bootloader")
                coding = ctypes.create_string_buffer(struct.pack("<IBBB", baud, 0, 0, 8), 7)  # 8N1
                lib.libusb_control_transfer(
                    self._handle, HOST_TO_DEVICE_CLASS_INTERFACE, REQ_SET_LINE_CODING, 0, comm, coding, 7,
                    CTRL_TIMEOUT_MS,
                )  # ignored by native USB; harmless if refused
                self._check(
                    lib.libusb_control_transfer(
                        self._handle, HOST_TO_DEVICE_CLASS_INTERFACE, REQ_SET_CONTROL_LINE_STATE, LINE_DTR, comm,
                        None, 0, CTRL_TIMEOUT_MS,
                    ),
                    "set DTR",
                )
        except BaseException:
            self._free()
            raise

    def _check(self, rc: int, what: str) -> int:
        if rc < 0:
            raise TransportError(f"{what} failed: {self._lib.libusb_error_name(rc).decode()}")
        return rc

    def _descriptor(self, kind: int, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = self._check(
            self._lib.libusb_control_transfer(
                self._handle, DEVICE_TO_HOST_STANDARD, REQ_GET_DESCRIPTOR, kind << 8, 0, buf, n, CTRL_TIMEOUT_MS
            ),
            "read USB descriptor",
        )
        return buf.raw[:got]

    def _vendor_id(self) -> int:
        dev = self._descriptor(DESC_DEVICE, 18)
        if len(dev) < 10:
            raise TransportError("short device descriptor")
        return int.from_bytes(dev[8:10], "little")  # idVendor

    def _config_descriptor(self) -> bytes:
        head = self._descriptor(DESC_CONFIGURATION, 9)
        if len(head) < 4:
            raise TransportError("short configuration descriptor")
        return self._descriptor(DESC_CONFIGURATION, int.from_bytes(head[2:4], "little"))  # wTotalLength

    def _cp210x_setup(self, iface: int, baud: int) -> None:
        """Turn the CP210x's UART on at `baud`, 8N1. Unlike native USB, the baud rate here is real
        and must match the firmware's BAUD."""

        def request(req: int, value: int, data: bytes = b"") -> None:
            buf = ctypes.create_string_buffer(data, len(data)) if data else None
            self._check(
                self._lib.libusb_control_transfer(
                    self._handle, HOST_TO_DEVICE_VENDOR_INTERFACE, req, value, iface, buf, len(data), CTRL_TIMEOUT_MS
                ),
                f"CP210x request 0x{req:02x}",
            )

        request(CP_IFC_ENABLE, 1)
        request(CP_SET_BAUDRATE, 0, struct.pack("<I", baud))
        request(CP_SET_LINE_CTL, CP_LINE_8N1)
        request(CP_SET_MHS, CP_MHS_RUN)

    # Transfers ----------------------------------------------------------------

    def _begin(self) -> None:
        with self._cv:
            if self._closed:
                raise TransportError("closed")
            self._busy += 1

    def _end(self) -> None:
        with self._cv:
            self._busy -= 1
            self._cv.notify_all()

    def read(self, size: int) -> bytes:
        self._begin()
        try:
            got = ctypes.c_int(0)
            n = min(size, READ_SIZE) // 64 * 64 or 64
            rc = self._lib.libusb_bulk_transfer(
                self._handle, self._ep_in, self._rxbuf, n, ctypes.byref(got), READ_TIMEOUT_MS
            )
            if rc in (0, ERROR_TIMEOUT, ERROR_INTERRUPTED):
                return self._rxbuf.raw[: got.value]  # a timeout can still carry data
            self._check(rc, "read")
            return b""
        finally:
            self._end()

    def write(self, data: bytes) -> None:
        self._begin()
        try:
            buf = ctypes.create_string_buffer(data, len(data))
            got = ctypes.c_int(0)
            rc = self._lib.libusb_bulk_transfer(
                self._handle, self._ep_out, buf, len(data), ctypes.byref(got), WRITE_TIMEOUT_MS
            )
            if rc == ERROR_TIMEOUT:
                # The board hasn't taken data for 2 s; it's wedged or not running the bridge.
                # Treated as lost: a half-written message can't be cancelled safely.
                raise TransportError(f"write timed out ({got.value} of {len(data)} bytes taken)")
            self._check(rc, "write")
            if got.value != len(data):
                raise TransportError(f"short write ({got.value} of {len(data)} bytes)")
        finally:
            self._end()

    def close(self) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
            idle = self._cv.wait_for(lambda: self._busy == 0, timeout=WRITE_TIMEOUT_MS / 1000 + 1)
        if idle:  # freeing libusb under a running transfer would crash; leak instead
            self._free()

    def _free(self) -> None:
        lib = self._lib if hasattr(self, "_lib") else None
        if lib is not None and self._handle:
            for iface in self._claimed:
                lib.libusb_release_interface(self._handle, iface)
            lib.libusb_close(self._handle)  # doesn't close the descriptor
            self._handle = ctypes.c_void_p()
        if lib is not None and self._ctx:
            lib.libusb_exit(self._ctx)
            self._ctx = ctypes.c_void_p()
        try:
            os.close(self._fd)
        except OSError:
            pass
