"""Over-the-air frame format. Shared by every link type (ESP-NOW now, LoRa later).

Every frame starts with the sender's device id, so the radio firmware can drop
frames from anyone but the current partner by checking the first 4 bytes.

    sender u32 | type u8 | flags u8 | session u16 | seq u32 | payload...
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .records import ID_LEN

PROTOCOL_VERSION = 1
MAX_FRAME = 250  # ESP-NOW v1 payload limit; no frame we send may exceed it


class T(IntEnum):
    BEACON = 1  # broadcast, unreliable: "I'm here, this is what I hold"
    HELLO = 2  # handshake request (initiator -> responder)
    HELLO_ACK = 3  # handshake accept
    ACK = 4  # acknowledges reliable frames
    INV = 5  # reliable: inventory of record metadata
    WANT = 6  # reliable: ids of records the sender is missing
    DATA = 7  # reliable: one chunk of one record
    DONE = 8  # reliable: sender has finished sending everything


RELIABLE = frozenset({T.INV, T.WANT, T.DATA, T.DONE})
FLAG_LAST = 0x01  # last frame of an INV or WANT list

HEADER = struct.Struct("<IBBHI")
BEACON = struct.Struct(f"<BI{ID_LEN}s")  # version, last_server_sync, digest
HELLO = struct.Struct("<BI")  # version, last_server_sync
ACK = struct.Struct("<II")  # next expected seq (cumulative), selective-ack bitmap of the 32 after it
INV_ENTRY = struct.Struct(f"<{ID_LEN}sBI")  # id, kind, created
DATA_HDR = struct.Struct(f"<{ID_LEN}sHH")  # record id, chunk index, chunk count
MAX_CHUNKS = 0xFFFF  # chunk count is a u16


@dataclass
class Frame:
    sender: int
    type: T
    flags: int = 0
    session: int = 0
    seq: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        return HEADER.pack(self.sender, self.type, self.flags, self.session, self.seq) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> Frame | None:
        """Returns None for anything malformed (stray 2.4 GHz traffic, other protocols)."""
        if len(data) < HEADER.size:
            return None
        sender, typ, flags, session, seq = HEADER.unpack_from(data)
        try:
            typ = T(typ)
        except ValueError:
            return None
        return cls(sender, typ, flags, session, seq, bytes(data[HEADER.size :]))
