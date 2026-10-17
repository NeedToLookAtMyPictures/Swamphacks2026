"""Byte pipes between the host and the ESP32 bridge.

EspNowSerialLink speaks the bridge protocol (SLIP-framed commands) and never
cares how the bytes travel. A Transport is just the pipe:

  * PySerialTransport (here): opens the board's USB CDC port the usual way
    (COM3, /dev/ttyACM0, /dev/cu.usbmodem...). Used on laptops, and by the
    tests through pyserial URLs such as socket://127.0.0.1:5000.
  * TermuxUsbTransport (termux_usb.py): Android, where apps can't open serial
    ports. It drives the same USB CDC interface directly through Termux:API.

open_transport() picks one from the port string. A new kind of pipe (LoRa
modem, Bluetooth, ...) is a new Transport plus one register() call; the link
and everything above it stay unchanged.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Callable

# USB vendor ids of the boards we use, for --port auto on laptops.
ESP_USB_VIDS = {
    0x303A,  # Espressif: any ESP32-S3 on its built-in USB (generic ESP32-S3FN8 boards)
    0x239A,  # Adafruit: Feather ESP32-S3 running TinyUSB firmware
    0x10C4,  # Silicon Labs CP2102: the USB-UART chip on Heltec WiFi LoRa 32 V3 boards
}


class TransportError(OSError):
    """The pipe failed or can't be opened (board unplugged, wrong port, no permission)."""


class PortInUse(TransportError):
    """The serial port is locked by another program."""


class Transport(ABC):
    """A byte stream to the board. Implementations must be safe to read from one
    thread while another thread writes, and close() may be called from either."""

    # False if this pipe can't be reopened by the running program after it breaks.
    # Android is like that: only a newly started program can be granted the USB device.
    reconnectable: bool = True

    @abstractmethod
    def read(self, size: int) -> bytes:
        """Return up to `size` bytes, or b"" after a short timeout (~50 ms) with no data.

        Raises TransportError once the pipe is broken.
        """

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write all of `data`. Raises TransportError if it can't."""

    @abstractmethod
    def close(self) -> None:
        """Release the pipe. Must not raise, and may be called more than once."""


def on_termux() -> bool:
    return "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", "")


# Registry ---------------------------------------------------------------------

Matcher = Callable[[str], bool]
Opener = Callable[[str, int], Transport]
_registry: list[tuple[Matcher, Opener]] = []


def register(matches: Matcher, opener: Opener) -> None:
    """Add a transport. It's tried before the built-in ones, in registration order."""
    _registry.append((matches, opener))


def _builtin() -> list[tuple[Matcher, Opener]]:
    from . import termux_usb  # imported here: it imports this module

    # pyserial last: it takes everything the others don't claim.
    return [(termux_usb.matches, termux_usb.TermuxUsbTransport), (lambda port: True, PySerialTransport)]


def open_transport(port: str, baud: int) -> Transport:
    """Open the pipe named by `port`.

    "auto"                        the one ESP32 plugged in (on Android: through Termux)
    "termux-usb", /dev/bus/usb/.. Android, through Termux:API
    anything else                 pyserial: COM3, /dev/ttyACM0, socket://host:port, ...
    """
    for matches, opener in _registry + _builtin():
        if matches(port):
            return opener(port, baud)
    raise TransportError(f"no transport for port {port!r}")


# pyserial -----------------------------------------------------------------------


class PySerialTransport(Transport):
    """The board's USB CDC port opened as a serial port. Works on Windows, macOS and Linux."""

    reconnectable = True

    def __init__(self, port: str, baud: int):
        try:
            import serial  # pyserial; imported here so Android and the simulator don't need it
        except ImportError as e:
            raise TransportError("pyserial is not installed: pip install pyserial") from e
        self._serial = serial
        # SerialException subclasses OSError; TypeError: pyserial can raise it if
        # the port is closed under it.
        self._errors = (OSError, TypeError, serial.SerialException)
        if port == "auto":
            port = self._find_board()
        self.port = port
        try:
            # serial_for_url takes a device name or a pyserial URL (socket://... in the tests).
            # exclusive=True locks the port: two programs reading one board steal each
            # other's bytes and both see endless fake disconnects. With the lock, the
            # second program gets a clear "in use" error instead. (Windows ports are
            # always exclusive.)
            self._ser = serial.serial_for_url(port, baud, timeout=0.05, exclusive=True)
        except (*self._errors, ValueError) as e:
            if "lock" in str(e).lower():
                raise PortInUse(
                    f"{port} is already open in another program (another main.py / run_node.py / "
                    f"diagnose.py, or a serial monitor). Close it first."
                ) from e
            raise TransportError(f"can't open {port}: {e}") from e

    def _find_board(self) -> str:
        from serial.tools import list_ports

        found = [p for p in list_ports.comports() if p.vid in ESP_USB_VIDS]
        if len(found) == 1:
            return found[0].device
        if not found:
            raise TransportError("no ESP32 board found on USB; plug one in or pass its port")
        names = ", ".join(p.device for p in found)
        raise TransportError(f"several ESP32 boards found ({names}); pass the port of the one to use")

    def read(self, size: int) -> bytes:
        try:
            return self._ser.read(size)
        except self._errors as e:
            raise TransportError(str(e)) from e

    def write(self, data: bytes) -> None:
        try:
            self._ser.write(data)
        except self._errors as e:
            raise TransportError(str(e)) from e

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass
