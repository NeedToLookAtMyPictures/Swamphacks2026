"""Android transport: the ESP32-S3's native USB, driven through Termux:API.

Android apps can't open serial ports (there is no /dev/ttyACM0 without root).
What Termux offers instead is `termux-usb`: after the user allows it, it starts
a program and hands it an open file descriptor for the USB device. This module
wraps that descriptor with libusb and speaks USB CDC-ACM itself, the same USB
class the board presents to laptops, so the firmware doesn't change.

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
DESC_CONFIGURATION, DESC_INTERFACE, DESC_ENDPOINT = 2, 4, 5
CLASS_CDC_COMM, CLASS_CDC_DATA = 0x02, 0x0A
EP_DIR_IN, EP_TYPE_MASK, EP_TYPE_BULK = 0x80, 0x03, 0x02
REQ_GET_DESCRIPTOR, REQ_SET_LINE_CODING, REQ_SET_CONTROL_LINE_STATE = 0x06, 0x20, 0x22
DEVICE_TO_HOST_STANDARD, HOST_TO_DEVICE_CLASS_INTERFACE = 0x80, 0x21
LINE_DTR = 0x0001  # DTR on, RTS off. Arduino-ESP32's TinyUSB serial only sends to the host
#                    while DTR is on, and the toggle sequences that reboot the board all use RTS.

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


def find_cdc(config: bytes) -> tuple[int | None, int, int, int]:
    """(comm interface or None, data interface, bulk IN endpoint, bulk OUT endpoint)
    from a raw configuration descriptor. Raises TransportError if there's no CDC data interface."""
    comm = None
    iface_class: dict[int, int] = {}
    eps: dict[int, dict[str, int]] = {}  # data interface -> {"in": addr, "out": addr}
    current = None
    i = 0
    while i + 2 <= len(config):
        length, kind = config[i], config[i + 1]
        if length < 2:
            break
        body = config[i : i + length]
        if kind == DESC_INTERFACE and length >= 9:
            current = body[2]
            iface_class[current] = body[5]
            if body[5] == CLASS_CDC_COMM and comm is None:
                comm = current
        elif kind == DESC_ENDPOINT and length >= 7 and current is not None:
            if iface_class.get(current) == CLASS_CDC_DATA and body[3] & EP_TYPE_MASK == EP_TYPE_BULK:
                eps.setdefault(current, {})["in" if body[2] & EP_DIR_IN else "out"] = body[2]
        i += length
    for data, found in eps.items():
        if "in" in found and "out" in found:
            return comm, data, found["in"], found["out"]
    raise TransportError("the USB device has no CDC serial interface: is it an ESP32-S3 running the bridge firmware?")


class TermuxUsbTransport(Transport):
    """USB CDC-ACM over a file descriptor from termux-usb."""

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
            comm, data, self._ep_in, self._ep_out = find_cdc(self._config_descriptor())
            for iface in (comm, data):
                if iface is None:
                    continue
                lib.libusb_detach_kernel_driver(self._handle, iface)  # fails harmlessly if none is attached
                self._check(lib.libusb_claim_interface(self._handle, iface), f"claim interface {iface}")
                self._claimed.append(iface)
            for ep in (self._ep_in, self._ep_out):
                lib.libusb_clear_halt(self._handle, ep)  # reset data toggles left by a previous run
            if comm is not None:
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

    def _config_descriptor(self) -> bytes:
        def get(n: int) -> bytes:
            buf = ctypes.create_string_buffer(n)
            got = self._check(
                self._lib.libusb_control_transfer(
                    self._handle, DEVICE_TO_HOST_STANDARD, REQ_GET_DESCRIPTOR, DESC_CONFIGURATION << 8, 0, buf, n,
                    CTRL_TIMEOUT_MS,
                ),
                "read configuration descriptor",
            )
            return buf.raw[:got]

        head = get(9)
        if len(head) < 4:
            raise TransportError("short configuration descriptor")
        return get(int.from_bytes(head[2:4], "little"))  # wTotalLength

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
