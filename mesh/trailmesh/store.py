"""The mesh's view of the app's database (application/user_app/database.py).

The app's `Database` is the only store on a device. It owns every rule about
the data: message expiry (31 days), tombstones killing messages, a receiver
tombstoning ("delivered") a message addressed to it, newest position per user.
AppStore only translates between its rows and mesh Records, using the app's
public API: export() to read what the device shares, merge() to apply what a
peer sent, list() for when this device received each row, and
get_device()/update_device() for identity and last server sync.

Two mesh rules go beyond the app's API:
  * Relays delete tombstoned messages (the app only marks them dead). The app
    has no delete call, so that one statement runs directly on its database file.
  * The sync cutoff also counts when a row was *received*, not just created.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
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
        self._received: dict[bytes, float] = {}  # id -> when this device received the row

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

    def _delete_tombstoned_relays(self) -> None:
        """A relay deletes every message a tombstone has killed.

        Only the sender and receiver keep a tombstoned message (the app marks it
        dead). Everyone else deletes it; the tombstone itself stays and is still
        relayed. The deleted message can't come back: has() still recognizes it by
        the tombstone, and the app's merge() refuses a tombstoned message that
        isn't ours. Messages marked dead without a tombstone are left alone, since
        nothing would stop a peer's live copy from bringing them back.
        """
        me = self.db.get_device()["name"]
        with contextlib.closing(sqlite3.connect(self.db.path, timeout=10)) as conn, conn:
            # sender and receiver are COLLATE NOCASE columns, as in the app's purge()
            conn.execute(
                "DELETE FROM messages WHERE sender IS NOT ? AND receiver IS NOT ? AND EXISTS ("
                "  SELECT 1 FROM tombstones t"
                "  WHERE t.target_origin_id = messages.origin_id AND t.target_seq = messages.seq)",
                (me, me),
            )

    def _refresh(self) -> dict[bytes, Record]:
        self._delete_tombstoned_relays()
        recs = self._shared()
        self._index = {r.id: r for r in recs}
        self._dead_targets = {
            record_id(Kind.MESSAGE, r.data["target_origin_id"], int(r.data["target_seq"]))
            for r in recs
            if r.kind is Kind.TOMBSTONE
        }
        # export() drops time_received, so read it from the full rows
        self._received = {
            Record.from_row(table, row).id: row["time_received"]
            for table in TABLE.values()
            for row in self.db.list(table)
        }
        return self._index

    def _current(self) -> dict[bytes, Record]:
        return self._index if self._index is not None else self._refresh()

    @staticmethod
    def _relayable(rec: Record) -> bool:
        # What's left marked dead is the sender's or receiver's own copy, which they keep.
        # Nobody else needs it: a peer that holds the message already got the tombstone,
        # and a peer that doesn't won't store a dead message that isn't its own.
        return not (rec.kind is Kind.MESSAGE and rec.data["is_dead"])

    def inventory(self, cutoff: float, exclude: frozenset = frozenset()) -> list[tuple[bytes, Kind, int]]:
        """(id, kind, created) of every record to offer a peer.

        cutoff = min of both devices' last server sync. Both got everything from
        the server up to then, so a record is offered only if this device created
        or received it after the cutoff. Counting *received* means data picked up
        while offline (from other peers, or written before the cutoff by a device
        that was offline) still moves on through the mesh. The Node raises the
        cutoff to the last sync with this peer, and excludes what the peer itself
        sent us then (see Node._end_session).
        """
        index = self._refresh()
        return [
            (rid, r.kind, min(int(r.created), 2**32 - 1))
            for rid, r in index.items()
            if self._relayable(r)
            and rid not in exclude
            and max(r.created, self._received.get(rid, r.created)) > cutoff
        ]

    def has(self, rid: bytes) -> bool:
        """True if we hold the record, or it's a message a tombstone has killed."""
        return rid in self._current() or rid in self._dead_targets

    def get(self, rid: bytes) -> Record | None:
        return self._current().get(rid)

    def digests(self) -> tuple[bytes, bytes]:
        """Order-independent fingerprints (XOR of ids) of the relayable set:
        (everything but positions, positions only).

        Equal digests mean two devices almost certainly hold the same records,
        so they skip the exchange. Positions change every minute, so the Node
        gives a position-only difference its own cooldown. Read fresh each
        time, since the app writes to its database directly.
        """
        data = pos = 0
        for rid, rec in self._refresh().items():
            if self._relayable(rec):
                if rec.kind is Kind.GEOLOCATION:
                    pos ^= int.from_bytes(rid, "little")
                else:
                    data ^= int.from_bytes(rid, "little")
        return data.to_bytes(ID_LEN, "little"), pos.to_bytes(ID_LEN, "little")

    def digest(self) -> bytes:
        """One fingerprint of the whole relayable set."""
        data, pos = self.digests()
        return bytes(a ^ b for a, b in zip(data, pos))

    def own_position(self, max_age: float) -> tuple[float, float] | None:
        """This user's latest (latitude, longitude) from the app, if it's at most max_age seconds old."""
        row = self.db.get_location(self.db.get_device()["name"])
        if row is None or self.now() - row["time_created"] > max_age:
            return None
        return row["latitude"], row["longitude"]

    @staticmethod
    def now() -> float:
        """The app database's clock, which stamps time_received and last_server_sync."""
        return time.time()

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
