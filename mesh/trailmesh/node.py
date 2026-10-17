"""A device on the mesh: duty-cycled discovery plus one exchange session at a time.

Discovery (replaces the TX/RX slot patterns from the original design):
  * All devices share GPS/phone-synced time and wake for a short window at the
    same moments (every `period` seconds), padded by `guard` for clock drift.
  * Inside the window each device broadcasts a few BEACONs at random offsets.
    The radio receives the rest of the time, and ESP-NOW's listen-before-talk
    handles collisions, so no transmit/listen slots are needed.
  * If a beacon's digest differs from ours, the device with the lower id sends
    HELLO. The exchange keeps the radio awake past the window until it finishes.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .link import Link, RxFrame
from .records import Kind, Record
from .session import Session
from .store import RecordStore
from .wire import BEACON, HELLO, PROTOCOL_VERSION, Frame, T


@dataclass
class Config:
    # Discovery schedule (seconds). Window opens at every multiple of `period`.
    period: float = 20.0
    window: float = 2.0
    guard: float = 0.5  # widen the window both ways to absorb clock drift
    beacons_per_window: int = 2
    always_on: bool = False  # demo mode: radio always on, beacon every ~beacon_interval
    beacon_interval: float = 1.0

    cooldown: float = 120.0  # after a successful exchange, wait this long before resyncing with that peer
    retry_backoff: float = 10.0  # after a failed exchange

    # Session / reliability. These suit ESP-NOW; LoRa needs much longer timeouts.
    hello_interval: float = 0.3
    hello_retries: int = 10
    rto: float = 0.25
    max_retries: int = 12
    window_frames: int = 16
    idle_timeout: float = 4.0
    linger: float = 3.0  # keep acking a finished session's stragglers this long

    accept_kinds: frozenset = field(default_factory=lambda: frozenset(Kind))
    pin_interval: float = 300.0  # min seconds between auto-generated location pins


class Node:
    def __init__(
        self,
        device_id: int,
        link: Link,
        store: RecordStore,
        config: Config | None = None,
        clock: Callable[[], float] = time.time,
        position: Callable[[], tuple[float, float, int] | None] | None = None,
        on_record: Callable[[Record], None] | None = None,
        on_session_end: Callable[[Session], None] | None = None,
        rng: random.Random | None = None,
    ):
        """
        device_id:  32-bit id derived from the user id (must be nonzero and unique).
        clock:      must return GPS/phone-synced unix time; all devices' windows depend on it.
        position:   returns (lat, lon, heading) or None; used for the automatic pin on each handshake.
        on_record:  called for every new record received over the mesh.
        """
        if not 0 < device_id < 2**32:
            raise ValueError("device_id must be a nonzero u32")
        self.id = device_id
        self.link = link
        self.store = store
        self.cfg = config or Config()
        self.clock = clock
        self.position = position
        self.on_record = on_record
        self.on_session_end = on_session_end
        self.rng = rng or random.Random()

        self.session: Session | None = None
        self._lingering: list[tuple[float, Session]] = []
        self._addr: dict[int, bytes | None] = {}  # device id -> link address
        self._next_allowed: dict[int, float] = {}  # device id -> earliest time we may initiate again
        self._radio_on: bool | None = None
        self._window_idx: int | None = None
        self._beacon_times: list[float] = []
        self._next_beacon = 0.0
        self._last_pin = -1e18

    # Main loop ----------------------------------------------------------------

    def run_forever(self, poll_interval: float = 0.005) -> None:
        while True:
            self.poll()
            time.sleep(poll_interval)

    def poll(self, now: float | None = None) -> None:
        """Process received frames and timers. Call every few milliseconds."""
        now = self.clock() if now is None else now
        for rx in self.link.poll():
            self._on_rx(rx, now)

        if self.session is not None:
            self.session.tick(now)
            if self.session.finished:
                self._end_session(now)
        self._lingering = [(t, s) for t, s in self._lingering if t > now]

        awake = self.cfg.always_on or self.session is not None or self._in_window(now)
        if awake != self._radio_on:
            self.link.set_radio(awake)
            self._radio_on = awake
        if awake and self.session is None:
            self._maybe_beacon(now)

    # Discovery ------------------------------------------------------------------

    def _in_window(self, now: float) -> bool:
        phase = (now + self.cfg.guard) % self.cfg.period
        return phase < self.cfg.window + 2 * self.cfg.guard

    def _maybe_beacon(self, now: float) -> None:
        if self.cfg.always_on:
            if now >= self._next_beacon:
                self._send_beacon()
                self._next_beacon = now + self.cfg.beacon_interval * self.rng.uniform(0.7, 1.3)
            return
        idx = int((now + self.cfg.guard) // self.cfg.period)
        if idx != self._window_idx:
            # Random send times inside the window so devices at a crowded campsite don't all collide.
            self._window_idx = idx
            start = idx * self.cfg.period
            self._beacon_times = sorted(
                self.rng.uniform(start, start + self.cfg.window) for _ in range(self.cfg.beacons_per_window)
            )
        while self._beacon_times and self._beacon_times[0] <= now:
            self._beacon_times.pop(0)
            self._send_beacon()

    def _send_beacon(self) -> None:
        payload = BEACON.pack(PROTOCOL_VERSION, self.store.last_server_sync, self.store.digest())
        self.link.send(Frame(self.id, T.BEACON, payload=payload).encode(), None)

    # Receive path -----------------------------------------------------------------

    def _on_rx(self, rx: RxFrame, now: float) -> None:
        f = Frame.decode(rx.payload)
        if f is None or f.sender == self.id:
            return
        self._addr[f.sender] = rx.src

        s = self.session
        if s is not None and f.sender == s.peer_id and f.session == s.id:
            s.on_frame(f, now)
            return
        for _, old in self._lingering:
            if f.sender == old.peer_id and f.session == old.id:
                old.on_frame(f, now)
                return
        if s is not None:
            return  # busy; also what the firmware's partner filter enforces

        if f.type is T.BEACON and len(f.payload) >= BEACON.size:
            version, _peer_sync, digest = BEACON.unpack_from(f.payload)
            if (
                version == PROTOCOL_VERSION
                and digest != self.store.digest()
                and self.id < f.sender  # lower id initiates, so both sides don't HELLO at once
                and now >= self._next_allowed.get(f.sender, 0)
            ):
                self._start_session(f.sender, initiator=True, now=now)
        elif f.type is T.HELLO and len(f.payload) >= HELLO.size:
            version, peer_sync = HELLO.unpack_from(f.payload)
            if version == PROTOCOL_VERSION:
                self._start_session(f.sender, initiator=False, now=now, session_id=f.session, peer_sync=peer_sync)

    # Sessions ---------------------------------------------------------------------

    def _start_session(
        self, peer: int, *, initiator: bool, now: float, session_id: int | None = None, peer_sync: int | None = None
    ) -> None:
        self._add_own_pin(now)
        self.link.set_partner(peer)
        addr = self._addr.get(peer)
        self.session = Session(
            my_id=self.id,
            peer_id=peer,
            session_id=session_id if session_id is not None else self.rng.randrange(1, 0x10000),
            initiator=initiator,
            store=self.store,
            config=self.cfg,
            mtu=self.link.mtu,
            send=lambda f: self.link.send(f.encode(), addr),
            now=now,
            peer_sync=peer_sync,
            on_record=self.on_record,
        )

    def _end_session(self, now: float) -> None:
        s = self.session
        assert s is not None
        self.session = None
        self.link.set_partner(None)
        self._next_allowed[s.peer_id] = now + (self.cfg.cooldown if s.ok else self.cfg.retry_backoff)
        self._lingering.append((now + self.cfg.linger, s))
        if self.on_session_end:
            self.on_session_end(s)

    def _add_own_pin(self, now: float) -> None:
        """Location sharing: every handshake carries a fresh pin of our own position."""
        if self.position is None or now - self._last_pin < self.cfg.pin_interval:
            return
        pos = self.position()
        if pos is not None:
            self.store.put(Record.pin(self.id, int(now), *pos))
            self._last_pin = now

    # App API ------------------------------------------------------------------------

    def post(self, record: Record) -> None:
        """Add a locally authored record; it spreads on the next encounter."""
        self.store.put(record)
