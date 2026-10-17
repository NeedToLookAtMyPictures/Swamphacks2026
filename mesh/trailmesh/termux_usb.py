"""Android transport: the ESP32's USB, driven through Termux:API.

Android apps can't open serial ports (there is no /dev/ttyACM0 without root).
What Termux offers instead is `termux-usb`: after the user allows it, it starts
a program and hands it an open file descriptor for the USB device. This module
wraps that descriptor with libusb and speaks the board's USB protocol itself,
so the firmware doesn't change: USB CDC-ACM for an ESP32-S3 on its native USB,
or the vendor protocol of the USB-serial bridge chip (CP210x, CH340) on boards
like the classic ESP32 DevKit, the ones that show up as /dev/ttyUSB0 on Linux.

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
CLASS_CDC_COMM, CLASS_CDC_DATA = 0x02, 0x0A
EP_DIR_IN, EP_TYPE_MASK, EP_TYPE_BULK = 0x80, 0x03, 0x02
REQ_GET_DESCRIPTOR, REQ_SET_LINE_CODING, REQ_SET_CONTROL_LINE_STATE = 0x06, 0x20, 0x22
DEVICE_TO_HOST_STANDARD, HOST_TO_DEVICE_CLASS_INTERFACE = 0x80, 0x21
HOST_TO_DEVICE_VENDOR, DEVICE_TO_HOST_VENDOR, HOST_TO_DEVICE_VENDOR_INTERFACE = 0x40, 0xC0, 0x41
LINE_DTR = 0x0001  # DTR on, RTS off. Arduino-ESP32's TinyUSB serial only sends to the host
#                    while DTR is on, and the toggle sequences that reboot the board all use RTS.

# USB-serial bridge chips, by vendor id. Their requests follow the Linux drivers
# (cp210x.c, ch341.c). DTR and RTS are left off: on ESP32 DevKits they drive the
# auto-reset circuit, and off/off (like on/on) lets the chip run.
CP210X, CH34X = "CP210x", "CH340"
BRIDGES = {0x10C4: CP210X, 0x1A86: CH34X}
FTDI_VID = 0x0403
CP210X_IFC_ENABLE, CP210X_SET_LINE_CTL, CP210X_SET_MHS, CP210X_SET_BAUDRATE = 0x00, 0x03, 0x07, 0x1E
CP210X_UART_ENABLE, CP210X_8N1, CP210X_DTR_RTS_OFF = 0x0001, 0x0800, 0x0300  # 0x0300: change both, to off
CH34X_READ_VERSION, CH34X_WRITE_REG, CH34X_SERIAL_INIT, CH34X_MODEM_CTRL = 0x5F, 0x9A, 0xA1, 0xA4
CH34X_REG_DIVISOR_PRESCALER, CH34X_REG_LCR2_LCR = 0x1312, 0x2518
CH34X_LCR_8N1 = 0xC3  # rx and tx on, 8 data bits
CH34X_DTR_RTS_OFF = 0xFFFF  # the register is active-low
CH34X_CLOCK = 48_000_000

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
    raise TransportError(
        "the USB device has no CDC serial interface and isn't a CP210x or CH340 USB-serial chip: "
        "is it an ESP32 running the bridge firmware?"
    )


def find_bulk(config: bytes) -> tuple[int, int, int]:
    """(interface, bulk IN endpoint, bulk OUT endpoint) of the first interface with
    a bulk pair, from a raw configuration descriptor. That's the UART of a bridge chip."""
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
        elif kind == DESC_ENDPOINT and length >= 7 and current is not None and body[3] & EP_TYPE_MASK == EP_TYPE_BULK:
            found = eps.setdefault(current, {})
            found["in" if body[2] & EP_DIR_IN else "out"] = body[2]
            if "in" in found and "out" in found:
                return current, found["in"], found["out"]
        i += length
    raise TransportError("the USB-serial chip has no bulk endpoints")


def ch34x_divisor(baud: int) -> int:
    """The CH340's prescaler/divisor register value for a baud rate (ch341_get_divisor in Linux)."""

    def clk_div(ps: int, fact: int) -> int:
        return 1 << (12 - 3 * ps - fact)

    baud = max(-(-CH34X_CLOCK // (clk_div(0, 0) * 256)), min(baud, CH34X_CLOCK // (clk_div(3, 0) * 2)))
    # the highest base clock (fact = 1) that gives a divisor under 512
    ps = next(ps for ps in (3, 2, 1, 0) if baud > CH34X_CLOCK // (clk_div(ps, 1) * 512))
    fact, cd = 1, clk_div(ps, 1)
    div = CH34X_CLOCK // (cd * baud)
    if div < 9 or div > 255:
        div, cd, fact = div // 2, cd * 2, 0
    if div < 2:
        raise TransportError(f"the CH340 can't do {baud} baud")
    # the next divisor if it lands closer to the requested rate
    if 16 * CH34X_CLOCK // (cd * div) - 16 * baud >= 16 * baud - 16 * CH34X_CLOCK // (cd * (div + 1)):
        div += 1
    if fact == 1 and div % 2 == 0:  # same rate from the lower base clock, which tolerates errors better
        div, fact = div // 2, 0
    return (0x100 - div) << 8 | fact << 2 | ps


class TermuxUsbTransport(Transport):
    """USB CDC-ACM, or a CP210x / CH340 bridge, over a file descriptor from termux-usb."""

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
            vid = self._vendor_id()
            if vid == FTDI_VID:
                raise TransportError("FTDI USB-serial chips aren't supported on Android, only CP210x and CH340")
            bridge = BRIDGES.get(vid)
            if bridge:
                comm = None
                data, self._ep_in, self._ep_out = find_bulk(self._config_descriptor())
            else:
                comm, data, self._ep_in, self._ep_out = find_cdc(self._config_descriptor())
            for iface in (comm, data):
                if iface is None:
                    continue
                lib.libusb_detach_kernel_driver(self._handle, iface)  # fails harmlessly if none is attached
                self._check(lib.libusb_claim_interface(self._handle, iface), f"claim interface {iface}")
                self._claimed.append(iface)
            for ep in (self._ep_in, self._ep_out):
                lib.libusb_clear_halt(self._handle, ep)  # reset data toggles left by a previous run
            if bridge == CP210X:
                self._setup_cp210x(data, baud)
            elif bridge == CH34X:
                self._setup_ch34x(baud)
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

    def _control(self, request_type: int, request: int, value: int, index: int, data: bytes, what: str) -> None:
        buf = ctypes.create_string_buffer(data, len(data)) if data else None
        self._check(
            self._lib.libusb_control_transfer(
                self._handle, request_type, request, value, index, buf, len(data), CTRL_TIMEOUT_MS
            ),
            what,
        )

    def _setup_cp210x(self, iface: int, baud: int) -> None:
        def out(request: int, value: int, data: bytes = b"") -> None:
            self._control(HOST_TO_DEVICE_VENDOR_INTERFACE, request, value, iface, data, f"CP210x request {request:#x}")

        out(CP210X_IFC_ENABLE, CP210X_UART_ENABLE)
        out(CP210X_SET_BAUDRATE, 0, struct.pack("<I", baud))
        out(CP210X_SET_LINE_CTL, CP210X_8N1)
        out(CP210X_SET_MHS, CP210X_DTR_RTS_OFF)

    def _setup_ch34x(self, baud: int) -> None:
        def out(request: int, value: int, index: int = 0) -> None:
            self._control(HOST_TO_DEVICE_VENDOR, request, value, index, b"", f"CH340 request {request:#x}")

        buf = ctypes.create_string_buffer(2)
        self._check(
            self._lib.libusb_control_transfer(
                self._handle, DEVICE_TO_HOST_VENDOR, CH34X_READ_VERSION, 0, 0, buf, 2, CTRL_TIMEOUT_MS
            ),
            "CH340 read version",
        )
        version = buf.raw[0]
        out(CH34X_SERIAL_INIT, 0)
        divisor = ch34x_divisor(baud)
        if version > 0x27:
            divisor |= 0x80  # send each byte on, instead of waiting for a full 32-byte packet
        out(CH34X_WRITE_REG, CH34X_REG_DIVISOR_PRESCALER, divisor)
        if version >= 0x30:  # older chips keep the line settings elsewhere, defaulting to 8N1
            out(CH34X_WRITE_REG, CH34X_REG_LCR2_LCR, CH34X_LCR_8N1)
        out(CH34X_MODEM_CTRL, CH34X_DTR_RTS_OFF)

    def _vendor_id(self) -> int:
        buf = ctypes.create_string_buffer(18)
        got = self._check(
            self._lib.libusb_control_transfer(
                self._handle, DEVICE_TO_HOST_STANDARD, REQ_GET_DESCRIPTOR, DESC_DEVICE << 8, 0, buf, 18,
                CTRL_TIMEOUT_MS,
            ),
            "read device descriptor",
        )
        if got < 10:
            raise TransportError("short device descriptor")
        return int.from_bytes(buf.raw[8:10], "little")

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
