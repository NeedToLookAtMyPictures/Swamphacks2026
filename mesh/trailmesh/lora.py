"""LoRa radio settings and airtime math. Pure Python, shared by the serial link,
the LoRa Config preset and the simulator.

LoRa here means raw LoRa packets between boards (no LoRaWAN, no gateways). The
trailmesh frames ride directly on them, exactly as they do on ESP-NOW.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

# host -> board CONFIG payload (must match `struct RadioConfig` in firmware/heltec_bridge)
CONFIG = struct.Struct("<IIBBb")  # freq_hz, bw_hz, sf, cr (4/cr), power_dbm

BANDWIDTHS = (125_000, 250_000, 500_000)


@dataclass(frozen=True)
class LoRaParams:
    """Radio settings. Every board in the mesh must use the same freq, bw, sf and cr.

    Default: 915 MHz (US), 500 kHz, SF7. A 500 kHz channel isn't subject to the
    FCC's 400 ms dwell limit, and SF7 gives a full 250-byte frame ~97 ms of airtime.
    SF9 roughly doubles the range and triples the airtime.
    """

    freq_hz: int = 915_000_000
    bw_hz: int = 500_000
    sf: int = 7
    cr: int = 5  # coding rate 4/cr
    power_dbm: int = 20
    preamble: int = 8  # symbols; fixed in the firmware

    def __post_init__(self):
        if not 863_000_000 <= self.freq_hz <= 928_000_000:
            raise ValueError(f"frequency {self.freq_hz} Hz is outside the board's 863-928 MHz range")
        if self.bw_hz not in BANDWIDTHS:
            raise ValueError(f"bandwidth must be one of {BANDWIDTHS} Hz")
        if not 7 <= self.sf <= 12:
            raise ValueError("spreading factor must be 7..12")
        if not 5 <= self.cr <= 8:
            raise ValueError("coding rate must be 5..8 (4/5..4/8)")
        if not -9 <= self.power_dbm <= 22:
            raise ValueError("SX1262 output power must be -9..22 dBm")

    def pack(self) -> bytes:
        return CONFIG.pack(self.freq_hz, self.bw_hz, self.sf, self.cr, self.power_dbm)

    @property
    def symbol_time(self) -> float:
        return (1 << self.sf) / self.bw_hz

    def airtime(self, payload_len: int) -> float:
        """Seconds on air for one packet (explicit header, CRC on), per the SX1262 datasheet."""
        ts = self.symbol_time
        ldro = 1 if ts >= 0.016 else 0  # low data rate optimization, set the same way by RadioLib
        bits = 8 * payload_len - 4 * self.sf + 28 + 16  # +16: payload CRC; explicit header
        n_payload = 8 + max(math.ceil(bits / (4 * (self.sf - 2 * ldro))) * self.cr, 0)
        return (self.preamble + 4.25 + n_payload) * ts
