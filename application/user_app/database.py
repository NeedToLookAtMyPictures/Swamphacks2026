"""Local SQLite database for the user app.

Schema follows the "Database Configuration" issue: four tables that are shared
between devices (locales, messages, geolocations, tombstones) and one that is
never shared (device_info).

Shared records are identified by (origin_id, seq): origin_id is the device that
created the record and seq is that device's next sequence number at the time.

People are identified by their name, which is unique and immutable. Messages
are addressed to a name, so they reach that person on whatever device they use.
"""

import contextlib
import re
import secrets
import sqlite3
import time
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "user.db"

MESSAGE_TTL = 2_678_400    # 31 days, in seconds
TOMBSTONE_TTL = 2_678_400  # 31 days, in seconds
SCHEMA_VERSION = 3

# Names are people's addresses: short (they go over LoRa) and compared ignoring case
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Receiver for messages to everyone. Not a valid name, so no person can have it.
BROADCAST = "*"

SCHEMA = """
CREATE TABLE IF NOT EXISTS locales (
    origin_id     TEXT    NOT NULL,
    seq           INTEGER NOT NULL,
    time_created  REAL    NOT NULL,
    location      TEXT    NOT NULL,  -- ID of the location
    name          TEXT    NOT NULL,  -- name of the location
    sender        TEXT    NOT NULL COLLATE NOCASE,  -- name
    comment       TEXT    NOT NULL DEFAULT '',
    time_received REAL    NOT NULL,
    PRIMARY KEY (origin_id, seq)
);

CREATE TABLE IF NOT EXISTS messages (
    origin_id     TEXT    NOT NULL,
    seq           INTEGER NOT NULL,
    time_created  REAL    NOT NULL,
    sender        TEXT    NOT NULL COLLATE NOCASE,  -- name
    receiver      TEXT    NOT NULL COLLATE NOCASE,  -- name
    content       TEXT    NOT NULL,
    is_dead       INTEGER NOT NULL DEFAULT 0,  -- set by age or by a tombstone
    time_received REAL    NOT NULL,
    received      INTEGER NOT NULL DEFAULT 0,  -- local only: delivered tombstone came within 31 days
    PRIMARY KEY (origin_id, seq)
);

-- One row per user: only the most recent position is kept.
-- origin_id is the device that reported it, used to break ties.
CREATE TABLE IF NOT EXISTS geolocations (
    user          TEXT    PRIMARY KEY COLLATE NOCASE,  -- name
    origin_id     TEXT    NOT NULL,
    time_created  REAL    NOT NULL,
    latitude      REAL    NOT NULL,
    longitude     REAL    NOT NULL,
    time_received REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS tombstones (
    origin_id        TEXT    NOT NULL,
    seq              INTEGER NOT NULL,
    target_origin_id TEXT    NOT NULL,
    target_seq       INTEGER NOT NULL,
    time_created     REAL    NOT NULL,
    reason           TEXT    NOT NULL,
    time_received    REAL    NOT NULL,
    PRIMARY KEY (origin_id, seq)
);

-- Never shared. Exactly one row, describing this device and its user.
CREATE TABLE IF NOT EXISTS device_info (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    device_id        TEXT    NOT NULL,  -- immutable, used as origin_id
    name             TEXT    NOT NULL COLLATE NOCASE,  -- immutable, unique: the user's address
    next_seq         INTEGER NOT NULL DEFAULT 1,
    time_created     REAL    NOT NULL,
    last_server_sync REAL               -- NULL until the first server sync
);

CREATE TRIGGER IF NOT EXISTS device_info_immutable
BEFORE UPDATE OF device_id, name, time_created ON device_info
WHEN NEW.device_id IS NOT OLD.device_id
  OR NEW.name IS NOT OLD.name
  OR NEW.time_created IS NOT OLD.time_created
BEGIN
    SELECT RAISE(ABORT, 'device_id, name and time_created are immutable');
END;

CREATE TRIGGER IF NOT EXISTS device_info_seq_only_increases
BEFORE UPDATE OF next_seq ON device_info
WHEN NEW.next_seq < OLD.next_seq
BEGIN
    SELECT RAISE(ABORT, 'next_seq can only increase');
END;
"""

SHARED_TABLES = ("locales", "messages", "geolocations", "tombstones")
COLUMNS = {
    "locales": ("origin_id", "seq", "time_created", "location", "name", "sender",
                "comment", "time_received"),
    "messages": ("origin_id", "seq", "time_created", "sender", "receiver", "content",
                 "is_dead", "time_received"),
    "geolocations": ("user", "origin_id", "time_created", "latitude", "longitude",
                     "time_received"),
    "tombstones": ("origin_id", "seq", "target_origin_id", "target_seq", "time_created",
                   "reason", "time_received"),
}
# Fields a sender may leave out; time_received is always set locally
OPTIONAL = ("comment", "is_dead", "time_received")


class DeviceNotInitialized(Exception):
    pass


def check_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValueError("names are 1-32 letters, digits, '_' or '-'")
    return name


class Database:
    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone()
            if has_tables and version == 2:
                conn.execute("ALTER TABLE messages ADD COLUMN received INTEGER NOT NULL DEFAULT 0")
            elif has_tables and version != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} uses an old schema; delete it and restart")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextlib.contextmanager
    def _connect(self):
        """A connection that commits on success, rolls back on error, and closes."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- device info ---------------------------------------------------------

    def get_device(self):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM device_info WHERE id = 1").fetchone()
        if row is None:
            return None
        device = dict(row)
        del device["id"]
        return device

    def init_device(self, name, device_id=None):
        """Create this device's identity. Fails if it already exists.

        Names must be unique between people. That can only be guaranteed by the
        server at setup; offline we can only refuse names we've already seen.
        """
        check_name(name)
        if any(c["name"].lower() == name.lower() for c in self.contacts()):
            raise ValueError(f"the name {name!r} is already taken")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO device_info (id, device_id, name, time_created) VALUES (1, ?, ?, ?)",
                (device_id or secrets.token_hex(4), name, time.time()),
            )
        return self.get_device()

    def update_device(self, last_server_sync=None):
        """Update the mutable device fields."""
        self._require_device()
        with self._connect() as conn:
            if last_server_sync is not None:
                conn.execute("UPDATE device_info SET last_server_sync = ? WHERE id = 1",
                             (last_server_sync,))
        return self.get_device()

    def _require_device(self):
        device = self.get_device()
        if device is None:
            raise DeviceNotInitialized()
        return device

    def _take_seq(self, conn):
        """Atomically reserve this device's next sequence number."""
        row = conn.execute(
            "UPDATE device_info SET next_seq = next_seq + 1 WHERE id = 1 "
            "RETURNING device_id, name, next_seq - 1 AS seq"
        ).fetchone()
        if row is None:
            raise DeviceNotInitialized()
        return row["device_id"], row["name"], row["seq"]

    # -- creating local records ------------------------------------------------

    def add_locale(self, location, name, comment=""):
        now = time.time()
        with self._connect() as conn:
            origin_id, me, seq = self._take_seq(conn)
            record = dict(origin_id=origin_id, seq=seq, time_created=now, location=location,
                          name=name, sender=me, comment=comment, time_received=now)
            self._insert(conn, "locales", record)
        return record

    def add_message(self, receiver, content):
        """Send a message to a name: someone from contacts(), or any name at all.

        It travels through the network until the device with that name sees it.
        With receiver=BROADCAST it is for everyone: nobody marks it delivered, so
        it spreads until it expires after 31 days.
        """
        if receiver != BROADCAST:
            check_name(receiver)
        now = time.time()
        with self._connect() as conn:
            origin_id, me, seq = self._take_seq(conn)
            if receiver.lower() == me.lower():
                raise ValueError("can't send a message to yourself")
            record = dict(origin_id=origin_id, seq=seq, time_created=now, sender=me,
                          receiver=receiver, content=content, is_dead=0, time_received=now)
            self._insert(conn, "messages", record)
        return record

    def add_tombstone(self, target_origin_id, target_seq, reason):
        with self._connect() as conn:
            return self._create_tombstone(conn, target_origin_id, target_seq, reason)

    def _create_tombstone(self, conn, target_origin_id, target_seq, reason):
        """Create a tombstone on an already-open connection (e.g. inside merge)."""
        now = time.time()
        origin_id, _, seq = self._take_seq(conn)
        record = dict(origin_id=origin_id, seq=seq, target_origin_id=target_origin_id,
                      target_seq=target_seq, time_created=now, reason=reason,
                      time_received=now)
        self._insert(conn, "tombstones", record)
        self._apply_tombstone(conn, record)
        return record

    def mark_delivered(self, origin_id, seq):
        """Called on the receiver's device: kill the message with a 'delivered' tombstone."""
        return self.add_tombstone(origin_id, seq, "delivered")

    def set_location(self, latitude, longitude):
        """Update this user's own current position."""
        device = self._require_device()
        now = time.time()
        record = dict(user=device["name"], origin_id=device["device_id"], time_created=now,
                      latitude=latitude, longitude=longitude, time_received=now)
        with self._connect() as conn:
            self._upsert_geolocation(conn, record)
        return record

    # -- reading ----------------------------------------------------------------

    def get(self, table, origin_id, seq):
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE origin_id = ? AND seq = ?",
                               (origin_id, seq)).fetchone()
        return dict(row) if row else None

    def list(self, table, since=None):
        """All rows in a shared table, optionally only those created after `since`."""
        assert table in SHARED_TABLES
        query, params = f"SELECT * FROM {table}", ()
        if since is not None:
            query, params = query + " WHERE time_created > ?", (since,)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY time_created", params)]

    def get_location(self, user):
        """A user's last known position, or None."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM geolocations WHERE user = ?", (user,)).fetchone()
        return dict(row) if row else None

    def contacts(self):
        """Everyone this device knows about, from any data it holds, except itself.

        Includes their last known position when we have one.
        """
        device = self.get_device()
        me = device["name"] if device else None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, MAX(last_heard) AS last_heard FROM ("
                "  SELECT user AS name, time_created AS last_heard FROM geolocations"
                "  UNION ALL SELECT sender, time_created FROM messages"
                "  UNION ALL SELECT receiver, NULL FROM messages WHERE receiver != '*'"
                "  UNION ALL SELECT sender, time_created FROM locales"
                ") WHERE name IS NOT ? GROUP BY name COLLATE NOCASE ORDER BY name COLLATE NOCASE",
                (me,)).fetchall()
            positions = {r["user"].lower(): dict(r) for r in conn.execute(
                "SELECT user, latitude, longitude, time_created FROM geolocations")}
        result = []
        for row in rows:
            pos = positions.get(row["name"].lower())
            result.append({
                "name": row["name"],
                "last_heard": row["last_heard"],
                "latitude": pos["latitude"] if pos else None,
                "longitude": pos["longitude"] if pos else None,
                "position_time": pos["time_created"] if pos else None,
            })
        return result

    def export(self, since=None):
        """Everything this device would share with another device or the server.

        Dead messages (expired or tombstoned) are never shared; the tombstone is
        what tells other devices to kill their copy.
        """
        self.purge()
        data = {table: self.list(table, since) for table in SHARED_TABLES}
        data["messages"] = [m for m in data["messages"] if not m["is_dead"]]
        for rows in data.values():
            for row in rows:
                row.pop("time_received", None)  # local bookkeeping only
                row.pop("received", None)
        return data

    # -- merging records from other devices ----------------------------------------

    def merge(self, data):
        """Merge records received from another device or the server.

        Returns how many records of each table were new or changed.
        """
        now = time.time()
        me = self._require_device()["name"]
        counts = dict.fromkeys(SHARED_TABLES, 0)
        with self._connect() as conn:
            # Tombstones first, so they also kill messages arriving in this batch
            for record in data.get("tombstones", []):
                record = self._clean("tombstones", record, now)
                if self._insert(conn, "tombstones", record, ignore=True):
                    counts["tombstones"] += 1
                self._apply_tombstone(conn, record)

            for record in data.get("locales", []):
                if self._insert(conn, "locales", self._clean("locales", record, now), ignore=True):
                    counts["locales"] += 1

            for record in data.get("messages", []):
                record = self._clean("messages", record, now)
                if self._merge_message(conn, record, me):
                    counts["messages"] += 1
                self._deliver_if_mine(conn, record, me)

            for record in data.get("geolocations", []):
                if self._upsert_geolocation(conn, self._clean("geolocations", record, now)):
                    counts["geolocations"] += 1
        self.purge()
        return counts

    def _deliver_if_mine(self, conn, record, me):
        """A message has reached the person it was addressed to: kill it everywhere.

        Creates the 'delivered' tombstone, which then spreads like any other record.
        Broadcasts have no single receiver, so they are never delivered.
        """
        if record["receiver"].lower() != me.lower():
            return
        row = conn.execute("SELECT is_dead FROM messages WHERE origin_id = ? AND seq = ?",
                           (record["origin_id"], record["seq"])).fetchone()
        if row and not row["is_dead"]:
            self._create_tombstone(conn, record["origin_id"], record["seq"], "delivered")

    def _merge_message(self, conn, record, me):
        mine = me.lower() in (record["sender"].lower(), record["receiver"].lower())
        tombstoned = conn.execute(
            "SELECT 1 FROM tombstones WHERE target_origin_id = ? AND target_seq = ?",
            (record["origin_id"], record["seq"])).fetchone()
        record["is_dead"] = int(bool(record["is_dead"]) or bool(tombstoned))

        existing = conn.execute("SELECT is_dead FROM messages WHERE origin_id = ? AND seq = ?",
                                (record["origin_id"], record["seq"])).fetchone()
        if existing:
            # Dead on either side means dead on both
            if record["is_dead"] and not existing["is_dead"]:
                conn.execute("UPDATE messages SET is_dead = 1 WHERE origin_id = ? AND seq = ?",
                             (record["origin_id"], record["seq"]))
                return True
            return False
        # Don't pick up messages that are already dead, unless they are ours
        if record["is_dead"] and not mine:
            return False
        if time.time() - record["time_created"] > MESSAGE_TTL and not mine:
            return False
        return self._insert(conn, "messages", record)

    def _apply_tombstone(self, conn, tombstone):
        # A delivered tombstone arriving before the message expires proves it was
        # received; after that, the sender can only tell it was received or died
        conn.execute("UPDATE messages SET is_dead = 1, received = received OR "
                     "(? = 'delivered' AND time_created >= ?) WHERE origin_id = ? AND seq = ?",
                     (tombstone["reason"], time.time() - MESSAGE_TTL,
                      tombstone["target_origin_id"], tombstone["target_seq"]))

    def _upsert_geolocation(self, conn, record):
        """Keep only the newest position per user; ties go to the higher device ID."""
        cur = conn.execute(
            "INSERT INTO geolocations (user, origin_id, time_created, latitude, longitude, "
            "time_received) VALUES (:user, :origin_id, :time_created, :latitude, :longitude, "
            ":time_received) "
            "ON CONFLICT (user) DO UPDATE SET origin_id = excluded.origin_id, "
            "time_created = excluded.time_created, latitude = excluded.latitude, "
            "longitude = excluded.longitude, time_received = excluded.time_received "
            "WHERE excluded.time_created > geolocations.time_created "
            "OR (excluded.time_created = geolocations.time_created "
            "AND excluded.origin_id > geolocations.origin_id)",
            record)
        return cur.rowcount > 0

    # -- housekeeping -------------------------------------------------------------

    def purge(self):
        """Apply the age rules: expire messages and drop old tombstones."""
        now = time.time()
        device = self.get_device()
        me = device["name"] if device else None
        with self._connect() as conn:
            # Old messages are deleted, except on the sender's and receiver's devices,
            # where they are kept but marked dead
            conn.execute("UPDATE messages SET is_dead = 1 WHERE time_created < ?",
                         (now - MESSAGE_TTL,))
            conn.execute("DELETE FROM messages WHERE time_created < ? "
                         "AND sender IS NOT ? AND receiver IS NOT ?",
                         (now - MESSAGE_TTL, me, me))
            conn.execute("DELETE FROM tombstones WHERE time_created < ?",
                         (now - TOMBSTONE_TTL,))

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _clean(table, record, now):
        missing = [c for c in COLUMNS[table] if c not in OPTIONAL and record.get(c) is None]
        if missing:
            raise ValueError(f"{table} record is missing {', '.join(missing)}")
        clean = {c: record.get(c) for c in COLUMNS[table]}
        if table == "locales":
            clean["comment"] = clean["comment"] or ""
        if table == "messages":
            clean["is_dead"] = int(bool(clean["is_dead"]))
        clean["time_received"] = now
        return clean

    @staticmethod
    def _insert(conn, table, record, ignore=False):
        cols = COLUMNS[table]
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        cur = conn.execute(
            f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
            record)
        return cur.rowcount > 0
