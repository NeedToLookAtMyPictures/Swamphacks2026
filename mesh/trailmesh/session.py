"""One exchange between two devices, from handshake to DONE.

Both sides run the same steps after the handshake:

  1. cutoff = min(my last server sync, peer's last server sync)
  2. send INV: metadata of every record eligible under the cutoff
  3. on the peer's full INV: send WANT with the ids I'm missing
  4. on the peer's full WANT: send those records as DATA chunks (by priority), then DONE
  5. finished once my DONE is acked and the peer's DONE has arrived

This is a sans-IO state machine: it never touches the radio or the clock
directly, which keeps it testable and link-agnostic (ESP-NOW now, LoRa later).
"""
from __future__ import annotations

import math
import struct
import zlib
from collections import deque
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

from .records import ID_LEN, PRIORITY, Record
from .reliable import ReliableReceiver, ReliableSender, RetriesExhausted
from .store import AppStore
from .wire import (
    DATA_HDR,
    FLAG_LAST,
    HEADER,
    HELLO,
    INV_ENTRY,
    MAX_CHUNKS,
    MAX_FRAME,
    PROTOCOL_VERSION,
    RELIABLE,
    Frame,
    T,
)

if TYPE_CHECKING:
    from .node import Config


class State(Enum):
    CONNECTING = auto()  # initiator waiting for HELLO_ACK
    ACTIVE = auto()
    DONE = auto()
    FAILED = auto()


class Session:
    def __init__(
        self,
        *,
        my_id: int,
        peer_id: int,
        session_id: int,
        initiator: bool,
        store: AppStore,
        config: Config,
        mtu: int,
        send: Callable[[Frame], None],
        now: float,
        peer_sync: int | None = None,
        on_record: Callable[[Record], None] | None = None,
    ):
        self.my_id = my_id
        self.peer_id = peer_id
        self.id = session_id
        self.initiator = initiator
        self.store = store
        self.cfg = config
        self.mtu = min(mtu, MAX_FRAME)  # every frame we build fits one ESP-NOW packet
        self._send = send
        self._on_record = on_record
        self.started = now
        self.last_heard = now

        self.sender = ReliableSender(config.window_frames, config.rto, config.max_retries)
        self.receiver = ReliableReceiver()
        self._outbox: deque[tuple[T, int, bytes]] = deque()
        self._data_iter = None
        self._done_queued = False
        self.peer_done = False

        self._peer_inv: list[tuple[bytes, int, int]] = []
        self._peer_wants: list[bytes] = []
        self._wanted: set[bytes] = set()
        self._partial: dict[bytes, tuple[int, dict[int, bytes]]] = {}  # id -> (count, index -> chunk)
        self.records_received = 0
        self.records_sent = 0

        if initiator:
            self.state = State.CONNECTING
            self._hello_tries = 0
            self._send_hello(now)
        else:
            self.state = State.ACTIVE
            self._send_ctrl(T.HELLO_ACK, HELLO.pack(PROTOCOL_VERSION, store.last_server_sync))
            self._activate(peer_sync or 0)

    @property
    def finished(self) -> bool:
        return self.state in (State.DONE, State.FAILED)

    @property
    def ok(self) -> bool:
        return self.state is State.DONE

    # Inbound ----------------------------------------------------------------

    def on_frame(self, f: Frame, now: float) -> None:
        self.last_heard = now
        if f.type is T.HELLO:
            # Our HELLO_ACK was lost and the initiator is retrying.
            if not self.initiator:
                self._send_ctrl(T.HELLO_ACK, HELLO.pack(PROTOCOL_VERSION, self.store.last_server_sync))
            return
        if f.type is T.HELLO_ACK:
            if self.state is State.CONNECTING and len(f.payload) >= HELLO.size:
                _version, peer_sync = HELLO.unpack_from(f.payload)
                self.state = State.ACTIVE
                self._activate(peer_sync)
            return
        if self.state is State.CONNECTING:
            return  # can't interpret anything until we know the peer's cutoff
        if f.type is T.ACK:
            self.sender.on_ack(f.payload)
        elif f.type in RELIABLE:
            # Keep acking even after we finish, so the peer's last frames get confirmed.
            for g in self.receiver.on_frame(f):
                if not self.finished:
                    self._deliver(g)
            self._send_ctrl(T.ACK, self.receiver.ack_payload())

    def _deliver(self, f: Frame) -> None:
        if f.type is T.INV:
            p = f.payload
            for off in range(0, len(p) - INV_ENTRY.size + 1, INV_ENTRY.size):
                self._peer_inv.append(INV_ENTRY.unpack_from(p, off))
            if f.flags & FLAG_LAST:
                wants = [
                    rid
                    for rid, kind, _created in self._peer_inv
                    if kind in self.cfg.accept_kinds and not self.store.has(rid)
                ]
                self._wanted = set(wants)
                self._queue_list(T.WANT, wants, ID_LEN)
        elif f.type is T.WANT:
            p = f.payload
            self._peer_wants += [p[i : i + ID_LEN] for i in range(0, len(p) - ID_LEN + 1, ID_LEN)]
            if f.flags & FLAG_LAST:
                recs = [r for r in map(self.store.get, self._peer_wants) if r is not None]
                recs.sort(key=lambda r: (PRIORITY[r.kind], -r.created))
                self._data_iter = self._data_frames(recs)
        elif f.type is T.DATA:
            if len(f.payload) < DATA_HDR.size:
                return
            rid, idx, count = DATA_HDR.unpack_from(f.payload)
            if rid not in self._wanted or not idx < count:
                return
            expected_count, chunks = self._partial.setdefault(rid, (count, {}))
            if count != expected_count:
                return  # inconsistent with this record's earlier chunks
            chunks[idx] = f.payload[DATA_HDR.size :]
            if len(chunks) == count:
                del self._partial[rid]
                self._wanted.discard(rid)
                blob = b"".join(chunks[i] for i in range(count))
                # Ids name a row, not its contents, so a CRC32 guards the bytes.
                if len(blob) < 4 or zlib.crc32(blob[4:]) != int.from_bytes(blob[:4], "little"):
                    return  # corrupted; drop it
                try:
                    rec = Record.decode(blob[4:])
                except (struct.error, ValueError):
                    return  # malformed; drop it
                if rec.id != rid:
                    return  # not the record we asked for; drop it
                if self.store.put(rec):
                    self.records_received += 1
                    if self._on_record:
                        self._on_record(rec)
        elif f.type is T.DONE:
            self.peer_done = True

    # Outbound ---------------------------------------------------------------

    def _activate(self, peer_sync: int) -> None:
        cutoff = min(self.store.last_server_sync, peer_sync)
        entries = [INV_ENTRY.pack(rid, kind, created) for rid, kind, created in self.store.inventory(cutoff)]
        self._queue_list(T.INV, entries, INV_ENTRY.size)

    def _queue_list(self, typ: T, items: list[bytes], item_size: int) -> None:
        """Pack fixed-size items into as few frames as fit, flagging the last one."""
        per = (self.mtu - HEADER.size) // item_size
        chunks = [items[i : i + per] for i in range(0, len(items), per)] or [[]]
        for i, chunk in enumerate(chunks):
            self._outbox.append((typ, FLAG_LAST if i == len(chunks) - 1 else 0, b"".join(chunk)))

    def _data_frames(self, recs: list[Record]):
        size = self.mtu - HEADER.size - DATA_HDR.size
        for rec in recs:
            try:
                enc = rec.encode()
            except struct.error:
                continue  # a field too big for its length prefix (e.g. a short string over 64 KB)
            blob = zlib.crc32(enc).to_bytes(4, "little") + enc
            count = max(1, math.ceil(len(blob) / size))
            if count > MAX_CHUNKS:
                continue  # over ~14.8 MB on ESP-NOW; can't be indexed with a u16
            for i in range(count):
                yield T.DATA, 0, DATA_HDR.pack(rec.id, i, count) + blob[i * size : (i + 1) * size]
            self.records_sent += 1
        self._done_queued = True
        yield T.DONE, 0, b""

    def _pump(self, now: float) -> None:
        while self.sender.can_send():
            if self._outbox:
                item = self._outbox.popleft()
            elif self._data_iter is not None:
                item = next(self._data_iter, None)
                if item is None:
                    self._data_iter = None
                    return
            else:
                return
            typ, flags, payload = item
            self._send(self.sender.send(Frame(self.my_id, typ, flags, self.id, 0, payload), now))

    def _send_ctrl(self, typ: T, payload: bytes) -> None:
        self._send(Frame(self.my_id, typ, 0, self.id, 0, payload))

    def _send_hello(self, now: float) -> None:
        self._hello_tries += 1
        self._hello_at = now
        self._send_ctrl(T.HELLO, HELLO.pack(PROTOCOL_VERSION, self.store.last_server_sync))

    # Timers -----------------------------------------------------------------

    def tick(self, now: float) -> None:
        if self.finished:
            return
        if now - self.last_heard > self.cfg.idle_timeout:
            self.state = State.FAILED  # peer walked out of range
            return
        if self.state is State.CONNECTING:
            if now - self._hello_at >= self.cfg.hello_interval:
                if self._hello_tries >= self.cfg.hello_retries:
                    self.state = State.FAILED
                else:
                    self._send_hello(now)
            return
        try:
            for f in self.sender.due(now):
                self._send(f)
        except RetriesExhausted:
            # If only our DONE is unconfirmed and we have the peer's DONE, every
            # record got through; the peer just closed before its ACK reached us.
            if self.peer_done and self.sender.unacked_types() == {T.DONE}:
                self.state = State.DONE
            else:
                self.state = State.FAILED
            return
        self._pump(now)
        if self.peer_done and self._done_queued and self.sender.idle:
            self.state = State.DONE
