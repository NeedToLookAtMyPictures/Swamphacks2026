"""Records: the units of data gossiped between devices.

Each kind mirrors one shared table of the app's database
(application/user_app/database.py): a record's `data` is that table's row,
minus the local-only `time_received` column. The app owns every rule about
the data (expiry, tombstones, newest position wins); the mesh only moves rows.
If a shared table's columns change in the app, FIELDS below must change too.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from functools import cached_property


class Kind(IntEnum):
    LOCALE = 1  # app table `locales`: a review/comment on a location
    MESSAGE = 2  # app table `messages`: to a name, or "*" for everyone
    GEOLOCATION = 3  # app table `geolocations`: a user's position, newest wins
    TOMBSTONE = 4  # app table `tombstones`: kills the target message everywhere


TABLE = {
    Kind.LOCALE: "locales",
    Kind.MESSAGE: "messages",
    Kind.GEOLOCATION: "geolocations",
    Kind.TOMBSTONE: "tombstones",
}
KIND_OF_TABLE = {table: kind for kind, table in TABLE.items()}

# Lower is sent first, so a hiker who walks out of range mid-exchange has
# already received what matters. Tombstones are cleanup, so they go last.
PRIORITY = {
    Kind.LOCALE: 0,
    Kind.MESSAGE: 1,
    Kind.GEOLOCATION: 2,
    Kind.TOMBSTONE: 3,
}

ID_LEN = 8

# Field codecs. Short strings (ids, names) have a u16 length, long text a u32 one.
_STR, _TEXT, _U8, _U32, _F64 = "str", "text", "u8", "u32", "f64"
_NUM = {_U8: struct.Struct("<B"), _U32: struct.Struct("<I"), _F64: struct.Struct("<d")}
_LEN = {_STR: struct.Struct("<H"), _TEXT: struct.Struct("<I")}

# Column order on the wire, per kind. Names match the app's columns exactly.
FIELDS = {
    Kind.LOCALE: (
        ("origin_id", _STR), ("seq", _U32), ("time_created", _F64), ("location", _STR),
        ("name", _STR), ("sender", _STR), ("comment", _TEXT),
    ),
    Kind.MESSAGE: (
        ("origin_id", _STR), ("seq", _U32), ("time_created", _F64), ("sender", _STR),
        ("receiver", _STR), ("is_dead", _U8), ("content", _TEXT),
    ),
    Kind.GEOLOCATION: (
        ("user", _STR), ("origin_id", _STR), ("time_created", _F64), ("latitude", _F64),
        ("longitude", _F64),
    ),
    Kind.TOMBSTONE: (
        ("origin_id", _STR), ("seq", _U32), ("target_origin_id", _STR), ("target_seq", _U32),
        ("time_created", _F64), ("reason", _STR),
    ),
}


def record_id(kind: Kind, *identity) -> bytes:
    """8-byte id from the fields that identify a row in the app's database.

    Locales, messages and tombstones are identified by (origin_id, seq), so
    any copy of the same row has the same id, even if a mutable column like
    `is_dead` differs. A geolocation is replaced whenever its user moves, so
    each position gets its own id from (user, origin_id, time_created).
    """
    h = hashlib.sha256(bytes([kind]))
    for part in identity:
        h.update(repr(part).encode())
        h.update(b"\x00")
    return h.digest()[:ID_LEN]


@dataclass(frozen=True)
class Record:
    kind: Kind
    data: dict  # one row of the app table TABLE[kind], without time_received

    @property
    def table(self) -> str:
        return TABLE[self.kind]

    @property
    def created(self) -> float:
        return self.data["time_created"]

    @cached_property
    def id(self) -> bytes:
        d = self.data
        if self.kind is Kind.GEOLOCATION:
            return record_id(self.kind, d["user"].lower(), d["origin_id"], float(d["time_created"]))
        return record_id(self.kind, d["origin_id"], int(d["seq"]))

    @classmethod
    def from_row(cls, table: str, row: dict) -> Record:
        kind = KIND_OF_TABLE[table]
        return cls(kind, {name: row[name] for name, _ in FIELDS[kind]})

    def encode(self) -> bytes:
        out = bytearray([self.kind])
        for name, codec in FIELDS[self.kind]:
            value = self.data[name]
            if codec in _LEN:
                raw = str(value).encode()
                out += _LEN[codec].pack(len(raw)) + raw  # struct.error if too long
            elif codec == _F64:
                out += _NUM[codec].pack(float(value))
            else:
                out += _NUM[codec].pack(int(value))
        return bytes(out)

    @classmethod
    def decode(cls, data: bytes) -> Record:
        """Raises struct.error or ValueError on malformed input."""
        kind = Kind(data[0]) if data else Kind(0)
        off = 1
        row = {}
        for name, codec in FIELDS[kind]:
            if codec in _LEN:
                (n,) = _LEN[codec].unpack_from(data, off)
                off += _LEN[codec].size
                if off + n > len(data):
                    raise ValueError(f"{name} runs past the end of the record")
                row[name] = data[off : off + n].decode()
                off += n
            else:
                (row[name],) = _NUM[codec].unpack_from(data, off)
                off += _NUM[codec].size
        if off != len(data):
            raise ValueError("trailing bytes after the record")
        return cls(kind, row)
