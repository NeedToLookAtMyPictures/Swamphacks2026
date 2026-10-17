"""Persistent record store (SQLite)."""
from __future__ import annotations

import sqlite3
import threading
import time

from .records import ID_LEN, Kind, Record


class RecordStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._digest: bytes | None = None
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id BLOB PRIMARY KEY, kind INT, origin INT, created INT,
                    server_time INT, key BLOB, body BLOB, received INT);
                CREATE INDEX IF NOT EXISTS records_key ON records(kind, key);
                -- ids we have seen but dropped because a newer record superseded them,
                -- so we don't keep requesting them from peers.
                CREATE TABLE IF NOT EXISTS tombstones (id BLOB PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v);
                """
            )

    # Queries -----------------------------------------------------------------

    def has(self, rid: bytes) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone() is not None
                or self._db.execute("SELECT 1 FROM tombstones WHERE id=?", (rid,)).fetchone() is not None
            )

    def get(self, rid: bytes) -> Record | None:
        with self._lock:
            row = self._db.execute(
                "SELECT kind, origin, created, body, key, server_time FROM records WHERE id=?", (rid,)
            ).fetchone()
        return None if row is None else Record(Kind(row[0]), row[1], row[2], row[3], row[4], row[5])

    def all(self, kind: Kind | None = None) -> list[Record]:
        sql = "SELECT kind, origin, created, body, key, server_time FROM records"
        args: tuple = ()
        if kind is not None:
            sql += " WHERE kind=?"
            args = (int(kind),)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created", args).fetchall()
        return [Record(Kind(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def inventory(self, cutoff: int) -> list[tuple[bytes, Kind, int]]:
        """(id, kind, created) of records eligible to exchange with a peer.

        A record is skipped only if the server had it at or before the cutoff:
        both peers synced with the server after that, so both already have it.
        Filtering on `created` instead would wrongly skip records that were
        written offline before the cutoff but reached the server after it.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, created FROM records WHERE server_time=0 OR server_time>?", (cutoff,)
            ).fetchall()
        return [(r[0], Kind(r[1]), r[2]) for r in rows]

    def digest(self) -> bytes:
        """Order-independent fingerprint of the held set (XOR of ids).

        Equal digests mean two devices almost certainly hold the same records,
        so they can skip the exchange entirely.
        """
        with self._lock:
            if self._digest is None:
                acc = 0
                for (rid,) in self._db.execute("SELECT id FROM records"):
                    acc ^= int.from_bytes(rid, "little")
                self._digest = acc.to_bytes(ID_LEN, "little")
            return self._digest

    # Mutations ---------------------------------------------------------------

    def put(self, rec: Record) -> bool:
        """Store a record. Returns True if it was new and kept."""
        rid = rec.id
        with self._lock, self._db:
            row = self._db.execute("SELECT server_time FROM records WHERE id=?", (rid,)).fetchone()
            if row is not None:
                if rec.server_time and not row[0]:
                    self._db.execute("UPDATE records SET server_time=? WHERE id=?", (rec.server_time, rid))
                return False
            if self._db.execute("SELECT 1 FROM tombstones WHERE id=?", (rid,)).fetchone():
                return False
            if rec.key:
                old = self._db.execute(
                    "SELECT id, created FROM records WHERE kind=? AND key=?", (int(rec.kind), rec.key)
                ).fetchone()
                if old is not None:
                    if (old[1], old[0]) > (rec.created, rid):  # existing is newer; ties broken by id
                        self._db.execute("INSERT OR IGNORE INTO tombstones VALUES (?)", (rid,))
                        return False
                    self._db.execute("DELETE FROM records WHERE id=?", (old[0],))
                    self._db.execute("INSERT OR IGNORE INTO tombstones VALUES (?)", (old[0],))
            self._db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                (rid, int(rec.kind), rec.origin, rec.created, rec.server_time, rec.key, rec.body, int(time.time())),
            )
            self._digest = None
            return True

    def close(self) -> None:
        self._db.close()

    @property
    def last_server_sync(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k='last_server_sync'").fetchone()
        return int(row[0]) if row else 0

    @last_server_sync.setter
    def last_server_sync(self, t: int) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('last_server_sync', ?)", (int(t),))
