"""The mesh's view of the app's database (application/user_app/database.py).

The app's `Database` is the only store on a device. It owns every rule about
the data: message expiry (31 days), tombstones killing messages, a receiver
tombstoning ("delivered") a message addressed to it, newest position per user.
AppStore only translates between its rows and mesh Records, using the app's
public API: export() to read what the device shares, merge() to apply what a
peer sent, get_device()/update_device() for identity and last server sync.
"""
from __future__ import annotations

import zlib

from .records import ID_LEN, TABLE, Kind, Record, record_id


class AppStore:
    def __init__(self, db):
        """db: an initialized application/user_app/database.py `Database`."""
        device = db.get_device()
        if device is None:
            raise ValueError("the app's device identity isn't set up yet (POST /api/device)")
        self.db = db
        self.device_id: str = device["device_id"]
        self._index: dict[bytes, Record] | None = None  # id -> record, for has()/get()
        self._dead_targets: set[bytes] = set()  # ids of messages killed by a tombstone

    @property
    def mesh_id(self) -> int:
        """The u32 the mesh uses for this device (frame sender, firmware filter).

        The app generates device_id as 8 hex digits, which is used as is.
        Any other string is hashed, which is fine as long as ids stay unique.
        """
        try:
            n = int(self.device_id, 16)
            if 0 < n < 2**32:
                return n
        except ValueError:
            pass
        return zlib.crc32(self.device_id.encode()) or 1

    # Reading -------------------------------------------------------------------

    def _shared(self, since: float | None = None) -> list[Record]:
        """Everything the app would share with a peer, as Records.

        export() applies the app's age rules first and leaves out expired messages.
        """
        data = self.db.export(since)
        return [Record.from_row(table, row) for table in TABLE.values() for row in data.get(table, [])]

    def _refresh(self) -> dict[bytes, Record]:
        recs = self._shared()
        self._index = {r.id: r for r in recs}
        self._dead_targets = {
            record_id(Kind.MESSAGE, r.data["target_origin_id"], int(r.data["target_seq"]))
            for r in recs
            if r.kind is Kind.TOMBSTONE
        }
        return self._index

    def _current(self) -> dict[bytes, Record]:
        return self._index if self._index is not None else self._refresh()

    @staticmethod
    def _relayable(rec: Record) -> bool:
        # The app marks a tombstoned message dead (is_dead = 1) instead of deleting it,
        # and keeps sharing it marked dead until the 31-day purge. Over the mesh a dead
        # copy is useless: a peer that has the message already got the tombstone, and a
        # peer that doesn't won't store a dead message that isn't its own. So the mesh
        # stops relaying dead messages, while the app's own rows stay as the app keeps them.
        # TODO: should relays delete the tombstoned messages they have stored
        return not (rec.kind is Kind.MESSAGE and rec.data["is_dead"])

    def inventory(self, cutoff: float) -> list[tuple[bytes, Kind, int]]:
        """(id, kind, created) of every record to offer a peer.

        Mirrors the app's GET /api/sync?since=<cutoff>: only records *created*
        after the cutoff are offered, where cutoff = min of both devices' last
        server sync (both got everything older from the server).
        """
        # TODO: should peer-to-peer sync be based on when this record was last seen by the server or when it was created
        self._refresh()
        recs = self._shared(cutoff or None)
        return [(r.id, r.kind, min(int(r.created), 2**32 - 1)) for r in recs if self._relayable(r)]

    def has(self, rid: bytes) -> bool:
        """True if we hold the record, or it's a message a tombstone has killed."""
        return rid in self._current() or rid in self._dead_targets

    def get(self, rid: bytes) -> Record | None:
        return self._current().get(rid)

    def digest(self) -> bytes:
        """Order-independent fingerprint of the relayable set (XOR of ids).

        Equal digests mean two devices almost certainly hold the same records,
        so they skip the exchange. Read fresh each time, since the app writes
        to its database directly (new messages, positions, comments).
        """
        acc = 0
        for rid, rec in self._refresh().items():
            if self._relayable(rec):
                acc ^= int.from_bytes(rid, "little")
        return acc.to_bytes(ID_LEN, "little")

    # Writing -------------------------------------------------------------------

    def put(self, rec: Record) -> bool:
        """Apply a record from a peer via the app's merge(). True if anything changed.

        The app decides everything: whether to keep it, tombstones killing
        messages, and creating a 'delivered' tombstone if the message is ours.
        """
        try:
            counts = self.db.merge({rec.table: [dict(rec.data)]})
        except ValueError:
            return False  # the app refused the row as malformed
        self._index = None  # merge may also have created records (a delivered tombstone)
        return any(counts.values())

    @property
    def last_server_sync(self) -> int:
        return int(self.db.get_device()["last_server_sync"] or 0)

    @last_server_sync.setter
    def last_server_sync(self, t: float) -> None:
        self.db.update_device(last_server_sync=t)
