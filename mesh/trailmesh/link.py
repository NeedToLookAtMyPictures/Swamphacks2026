"""Radio link abstraction.

The protocol only needs: send a small frame to one address or to everyone,
receive frames, turn the radio on/off, and (optionally) filter by partner.
ESP-NOW implements this today. A LoRa link would implement the same interface
with a smaller mtu and broadcast-only sends (the device id at the start of
every frame already tells receivers who it is from).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RxFrame:
    payload: bytes
    src: bytes | None  # link-layer address (MAC for ESP-NOW); None if the link has none
    rssi: int | None = None


class Link(ABC):
    mtu: int = 250  # max frame size in bytes, including the protocol header
    radio: str = "ESP-NOW"  # shown to the user as how a record arrived (the app's received_via)

    @abstractmethod
    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        """Send one frame. dest=None means broadcast."""

    @abstractmethod
    def poll(self) -> list[RxFrame]:
        """Return all frames received since the last poll. Must not block."""

    def set_radio(self, on: bool) -> None:
        """Power the radio up or down for duty cycling. Default: always on."""

    def set_partner(self, device_id: int | None) -> None:
        """Ask the hardware to drop frames from anyone but this device (None = accept all)."""

    def close(self) -> None:
        pass
