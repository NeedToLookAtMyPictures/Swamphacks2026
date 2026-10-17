"""Records: the units of data gossiped between devices.

A record is immutable and identified by a hash of its contents, so two devices
can compare holdings by id alone and duplicates are harmless.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from functools import cached_property


class Kind(IntEnum):
    LOCALE_UPDATE = 1
    MESSAGE = 2
    PIN = 3


# Lower is sent first. Safety-relevant data goes first so a hiker who walks
# out of range mid-exchange has already received what matters.
PRIORITY = {
    Kind.LOCALE_UPDATE: 0,
    Kind.MESSAGE: 1,
    Kind.PIN: 2,
}

ID_LEN = 8
_HDR = struct.Struct("<BIIIB")  # kind, origin, created, server_time, key_len
_PIN = struct.Struct("<iiH")  # lat*1e7, lon*1e7, heading degrees


@dataclass(frozen=True)
class Record:
    kind: Kind
    origin: int  # device id of the author
    created: int  # unix seconds (GPS time) when authored
    body: bytes
    # Supersede key: a newer record with the same (kind, key) replaces this one.
    # Empty means the record is never replaced (e.g. messages, pins).
    key: bytes = b""
    # When the server first saw this record (0 = not yet). Used for the sync
    # cutoff. Not part of the id, since it gets filled in after authoring.
    server_time: int = 0

    @cached_property
    def id(self) -> bytes:
        h = hashlib.sha256()
        h.update(struct.pack("<BIIB", self.kind, self.origin, self.created, len(self.key)))
        h.update(self.key)
        h.update(self.body)
        return h.digest()[:ID_LEN]

    def encode(self) -> bytes:
        return (
            _HDR.pack(self.kind, self.origin, self.created, self.server_time, len(self.key))
            + self.key
            + self.body
        )

    @classmethod
    def decode(cls, data: bytes) -> Record:
        kind, origin, created, server_time, klen = _HDR.unpack_from(data)
        off = _HDR.size
        return cls(Kind(kind), origin, created, data[off + klen :], data[off : off + klen], server_time)

    # Convenience constructors ------------------------------------------------

    @classmethod
    def locale_update(cls, origin: int, created: int, locale_id: str, field: str, text: str) -> Record:
        """E.g. locale_update(me, t, "pct-mile-1203-spring", "water", "dry as of noon")."""
        return cls(Kind.LOCALE_UPDATE, origin, created, text.encode(), f"{locale_id}:{field}".encode())

    @classmethod
    def message(cls, origin: int, created: int, text: str, lat: float, lon: float) -> Record:
        return cls(Kind.MESSAGE, origin, created, _PIN.pack(round(lat * 1e7), round(lon * 1e7), 0) + text.encode())

    @classmethod
    def pin(cls, origin: int, created: int, lat: float, lon: float, heading: int) -> Record:
        return cls(Kind.PIN, origin, created, _PIN.pack(round(lat * 1e7), round(lon * 1e7), heading % 360))

    def position(self) -> tuple[float, float, int] | None:
        """(lat, lon, heading) for pins and messages."""
        if self.kind not in (Kind.PIN, Kind.MESSAGE):
            return None
        lat, lon, heading = _PIN.unpack_from(self.body)
        return lat / 1e7, lon / 1e7, heading

    def text(self) -> str:
        if self.kind is Kind.MESSAGE:
            return self.body[_PIN.size :].decode(errors="replace")
        return self.body.decode(errors="replace")
