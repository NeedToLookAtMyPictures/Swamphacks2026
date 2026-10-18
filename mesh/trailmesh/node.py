"""A device on the mesh: duty-cycled discovery plus one exchange session at a time.

Discovery (replaces the TX/RX slot patterns from the original design):
  * All devices share GPS/phone-synced time and wake for a short window at the
    same moments (every `period` seconds), padded by `guard` for clock drift.
  * Inside the window each device broadcasts a few BEACONs at random offsets.
    The radio receives the rest of the time, and listen-before-talk (ESP-NOW's
    hardware, or channel activity detection in the LoRa firmware) handles
    collisions, so no transmit/listen slots are needed.
  * A beacon whose digests differ from ours makes its sender a candidate, if
    our id is lower and that peer's cooldown has passed. `select_delay` after
    the first candidate, the node sends HELLO to the one it synced with least
    recently, so everyone in a group of hikers gets paired over time. The
    exchange keeps the radio awake past the window until it finishes.

Per peer, the node remembers its last good sync: the next one sends only what
changed since then, and a run of failures backs off exponentially.

Two radios (a Heltec V3 board: `link` = LoRa, `fast_link` = ESP-NOW): discovery
and the handshake always go over LoRa. HELLO and HELLO_ACK carry each side's
position; if both are fresh and within `espnow_range` (60 m), the responder
says so in its HELLO_ACK and both sides move the rest of the exchange to
ESP-NOW, with ESP-NOW timings (`fast_config`). Otherwise the whole exchange
runs over LoRa, positions first. The ESP-NOW radio is on only during an
ESP-NOW exchange. If one fails, that peer gets LoRa for `espnow_retry`
seconds. Without a fast_link the node uses `link` for everything, which is how
a board is pinned to LoRa only or ESP-NOW only.

Mixed boards (`espnow_fallback`, --multiple-boards): plain ESP32s have no LoRa,
so they never hear a Heltec's LoRa beacons. While LoRa hasn't produced a
successful exchange recently (`_fallback_active`), the node also turns ESP-NOW
on during discovery and sends each beacon on both radios. A peer heard only on
ESP-NOW, or one whose last exchange over LoRa failed, gets an exchange that runs
entirely over ESP-NOW, handshake included. Peers heard on LoRa keep the 60 m switch.

The data lives in the app's database, which the app writes to directly (new
messages, comments, its own position once a minute). The node just syncs it.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .link import Link, RxFrame
from .lora import LoRaParams
from .records import Kind, Record
from .session import Session
from .store import AppStore
from .wire import (
    ACK,
    BEACON,
    HEADER,
    HELLO,
    HELLO_FAST_OK,
    HELLO_HAS_FIX,
    HELLO_USE_FAST,
    MAX_FRAME,
    PROTOCOL_VERSION,
    Frame,
    T,
)

HANDSHAKE = frozenset({T.BEACON, T.HELLO, T.HELLO_ACK})  # always on the discovery link


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in meters between two (latitude, longitude) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


@dataclass
class Config:
    # Discovery schedule (seconds). Window opens at every multiple of `period`.
    period: float = 20.0
    window: float = 2.0
    guard: float = 0.5  # widen the window both ways to absorb clock drift
    beacons_per_window: int = 2
    always_on: bool = False  # demo mode: radio always on, beacon every ~beacon_interval
    beacon_interval: float = 1.0
    select_delay: float = 1.0  # collect beacons this long, then pick the least recently synced peer

    # After a successful exchange, how long until we sync with that peer again:
    cooldown: float = 10.0  # if anything but positions differs
    # TODO: update to 10 minute locational cooldown
    location_cooldown: float = 10.0  # if only positions differ
    retry_backoff: float = 10.0  # after a failed exchange; doubles with each failure in a row
    max_backoff: float = 300.0

    # Session / reliability. These suit ESP-NOW; Config.lora() derives LoRa values from airtime.
    hello_interval: float = 0.3
    hello_retries: int = 10
    rto: float = 0.25
    max_retries: int = 12
    window_frames: int = 16
    idle_timeout: float = 4.0
    linger: float = 3.0  # keep acking a finished session's stragglers this long
    ack_delay: float = 0.0  # 0: ACK each reliable frame at once; >0: ACK when the peer pauses this long
    ack_max: float = 0.0  # never hold a delayed ACK longer than this

    accept_kinds: frozenset = field(default_factory=lambda: frozenset(Kind))
    positions_first: bool = False  # send positions (pins) before every other record; on for LoRa

    # Two radios (Node with a fast_link): switch the exchange to ESP-NOW when the peer is this close.
    espnow_range: float = 60.0  # meters
    position_max_age: float = 300.0  # an older own position doesn't count (the app writes one a minute)
    espnow_retry: float = 300.0  # after an ESP-NOW exchange with a peer fails, use LoRa with it this long
    # Mixed boards: also discover over ESP-NOW (beacons on both radios) while LoRa isn't working.
    espnow_fallback: bool = False

    @classmethod
    def lora(cls, params: LoRaParams | None = None, **overrides) -> Config:
        """Timings for a LoRa link, derived from the airtime of `params` (default LoRaParams()).

        LoRa is half duplex and slow (a 250-byte frame is ~0.1 s at SF7/500 kHz,
        ~0.3 s at SF9), so: a small send window, ACKs batched per burst, and
        timeouts that cover both sides' bursts.

        TODO: these multipliers come from the simulator (sim.py airtime model), not from
        hardware. Tune window_frames / rto / ack_delay on real boards.
        TODO: the discovery window (2 s every 20 s) is the ESP-NOW one; with LoRa's range a
        longer period would save power, and a 6-hiker cluster spends many windows on retries.
        """
        p = params or LoRaParams()
        frame = p.airtime(MAX_FRAME)
        ack = p.airtime(HEADER.size + ACK.size)
        hello = p.airtime(HEADER.size + HELLO.size)
        beacon = p.airtime(HEADER.size + BEACON.size)
        window_frames = 4
        burst = window_frames * frame
        ack_delay = 1.2 * frame + 0.05  # longer than the gap between two frames of one burst
        rto = 2 * burst + ack_delay + 2 * ack + 0.5  # both sides stream; the peer's burst may go first
        window = max(2.0, 40 * beacon)
        values = dict(
            window=window,
            select_delay=window / 2,
            beacon_interval=max(1.0, 20 * beacon),
            hello_interval=2 * hello + frame + 0.3,  # the responder's first INV may hold the channel
            hello_retries=6,
            rto=rto,
            max_retries=8,
            window_frames=window_frames,
            idle_timeout=max(4.0, 4 * rto),
            linger=max(3.0, 2 * rto),
            ack_delay=ack_delay,
            ack_max=burst + ack_delay,
            positions_first=True,  # a slow link may lose the peer mid-exchange: pins matter most
        )
        values.update(overrides)
        return cls(**values)


class Node:
    def __init__(
        self,
        link: Link,
        store: AppStore,
        config: Config | None = None,
        clock: Callable[[], float] = time.time,
        on_record: Callable[[Record], None] | None = None,
        on_delivered: Callable[[str, int], None] | None = None,
        on_session_end: Callable[[Session], None] | None = None,
        rng: random.Random | None = None,
        fast_link: Link | None = None,
        fast_config: Config | None = None,
    ):
        """
        link:         discovery and handshake radio (and the exchange, unless fast_link takes over).
        fast_link:    optional ESP-NOW link for exchanges with peers within cfg.espnow_range.
        fast_config:  timings for exchanges over fast_link (default Config()).
        store:        the app's database, wrapped: AppStore(Database(...)).
        clock:        must return GPS/phone-synced unix time; all devices' windows depend on it.
        on_record:    called for every record a peer sent that changed the app's database.
        on_delivered: called with (origin_id, seq) of one of our own messages when the
                      receiver's 'delivered' tombstone reaches us. Until then the message
                      is pending; the app treats it as lost if still pending after 31 days.
        """
        self.id = store.mesh_id
        self.link = link
        self.store = store
        self.cfg = config or Config()
        self.clock = clock
        self.on_record = on_record
        self.on_delivered = on_delivered
        self.on_session_end = on_session_end
        self.rng = rng or random.Random()
        self.fast_link = fast_link
        self.fast_cfg = fast_config or Config()

        self.session: Session | None = None
        self._lingering: list[tuple[float, Session]] = []
        self._addr: dict[int, bytes | None] = {}  # device id -> link address
        self._fast_addr: dict[int, bytes | None] = {}  # device id -> fast_link address
        self._no_fast_until: dict[int, float] = {}  # peer -> LoRa only until then (an ESP-NOW exchange failed)
        self._lora_ok_at = float("-inf")  # last successful exchange found over LoRa (espnow_fallback)
        self._lora_failed: set[int] = set()  # peers whose last exchange over LoRa failed (espnow_fallback)
        self._fast_on: bool | None = None
        # Per peer (device id):
        self._next_data: dict[int, float] = {}  # earliest time to initiate again if non-position data differs
        self._next_pos: dict[int, float] = {}  # ... if only positions differ
        self._fails: dict[int, int] = {}  # failed exchanges in a row
        self._last_ok: dict[int, float] = {}  # time of the last successful exchange
        self._resync: dict[int, tuple[float, frozenset]] = {}  # (since, exclude) for a change-only INV
        # Peers whose beacon asked for a sync: id -> (heard at, non-position data differs, links heard on).
        # Links: "lora" for `link`, "espnow" for `fast_link` (espnow_fallback only).
        self._candidates: dict[int, tuple[float, bool, frozenset]] = {}
        self._select_at: float | None = None
        self._my_digests: tuple[bytes, bytes] | None = None  # read at most once per poll
        self._radio_on: bool | None = None
        self._window_idx: int | None = None
        self._beacon_times: list[float] = []
        self._next_beacon = 0.0

    # Main loop ----------------------------------------------------------------

    def run_forever(self, poll_interval: float = 0.005) -> None:
        while True:
            self.poll()
            time.sleep(poll_interval)

    def poll(self, now: float | None = None) -> None:
        """Process received frames and timers. Call every few milliseconds."""
        now = self.clock() if now is None else now
        self._my_digests = None
        for rx in self.link.poll():
            self._on_rx(rx, now)
        if self.fast_link is not None:
            for rx in self.fast_link.poll():
                self._on_rx(rx, now, fast=True)

        if self.session is not None:
            self.session.tick(now)
            if self.session.finished:
                self._end_session(now)
        self._lingering = [(t, s) for t, s in self._lingering if t > now]
        if self.session is None and self._select_at is not None and now >= self._select_at:
            self._select_peer(now)

        awake = self.cfg.always_on or self.session is not None or self._in_window(now)
        if awake != self._radio_on:
            self.link.set_radio(awake)
            self._radio_on = awake
        if self.fast_link is not None:
            # ESP-NOW is on only while an exchange (or a finished one still acking) uses it,
            # or for discovery while falling back from LoRa.
            fast = any(s.fast for s in [self.session, *(s for _, s in self._lingering)] if s is not None)
            fast = fast or (awake and self.session is None and self._fallback_active(now))
            if fast != self._fast_on:
                self.fast_link.set_radio(fast)
                self._fast_on = fast
        if awake and self.session is None:
            self._maybe_beacon(now)

    # Discovery ------------------------------------------------------------------

    def _fallback_active(self, now: float) -> bool:
        """Discover over ESP-NOW too: no exchange found over LoRa has succeeded lately.

        "Lately" is the previous discovery window (or a few beacons in demo mode), so
        plain ESP32s nearby get a chance at least every other window.
        """
        if not self.cfg.espnow_fallback or self.fast_link is None:
            return False
        recent = 3 * self.cfg.beacon_interval if self.cfg.always_on else self.cfg.period
        return now - self._lora_ok_at > recent

    def _in_window(self, now: float) -> bool:
        phase = (now + self.cfg.guard) % self.cfg.period
        return phase < self.cfg.window + 2 * self.cfg.guard

    def _maybe_beacon(self, now: float) -> None:
        if self.cfg.always_on:
            if now >= self._next_beacon:
                self._send_beacon(now)
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
            self._send_beacon(now)

    def _send_beacon(self, now: float) -> None:
        payload = BEACON.pack(PROTOCOL_VERSION, self.store.last_server_sync, *self._digests())
        frame = Frame(self.id, T.BEACON, payload=payload).encode()
        self.link.send(frame, None)
        if self._fallback_active(now):
            self.fast_link.send(frame, None)

    def _digests(self) -> tuple[bytes, bytes]:
        if self._my_digests is None:
            self._my_digests = self.store.digests()  # reads the app's database
        return self._my_digests

    def _on_beacon(self, f: Frame, now: float, fast: bool = False) -> None:
        version, _peer_sync, data_digest, pos_digest = BEACON.unpack_from(f.payload)
        peer = f.sender
        if version != PROTOCOL_VERSION or self.id > peer:
            return  # lower id initiates, so both sides don't HELLO at once
        data_due = now >= self._next_data.get(peer, 0)
        pos_due = now >= self._next_pos.get(peer, 0)
        if not (data_due or pos_due):
            return  # checked first, since comparing digests reads the app's database
        my_data, my_pos = self._digests()
        data_differs = data_digest != my_data
        if (data_due and data_differs) or (pos_due and pos_digest != my_pos):
            heard = self._candidates[peer][2] if peer in self._candidates else frozenset()
            self._candidates[peer] = (now, data_differs, heard | {"espnow" if fast else "lora"})
            if self._select_at is None:
                self._select_at = self._selection_time(now)
        else:
            self._candidates.pop(peer, None)

    def _selection_time(self, now: float) -> float:
        at = now + self.cfg.select_delay
        if not self.cfg.always_on:
            # Decide while the candidates' radios are still on: by the end of this window's beacons.
            idx = int((now + self.cfg.guard) // self.cfg.period)
            at = min(at, idx * self.cfg.period + self.cfg.window)
        return max(at, now)

    def _fresh_since(self, now: float) -> float:
        """Beacons heard before this came from a window whose radios are off again."""
        if self.cfg.always_on:
            return now - 2 * self.cfg.beacon_interval  # beacons are at most 1.3 intervals apart
        idx = int((now + self.cfg.guard) // self.cfg.period)
        return idx * self.cfg.period - self.cfg.guard

    def _select_peer(self, now: float) -> None:
        self._select_at = None
        oldest = self._fresh_since(now)
        fresh = {peer: c for peer, c in self._candidates.items() if c[0] >= oldest}
        self._candidates.clear()
        if not fresh:
            return
        # Least recently synced first (never synced before all others), then peers with new
        # non-position data, then random, so a group of hikers all get paired over time.
        peer = min(fresh, key=lambda p: (self._last_ok.get(p, float("-inf")), not fresh[p][1], self.rng.random()))
        # A peer heard on LoRa stays a LoRa peer (the 60 m switch decides from there), unless
        # our last LoRa exchange with it failed and it can be reached over ESP-NOW instead.
        heard = fresh[peer][2]
        espnow_only = "lora" not in heard or ("espnow" in heard and peer in self._lora_failed)
        self._start_session(peer, initiator=True, now=now, espnow_only=espnow_only)

    # Receive path -----------------------------------------------------------------

    def _on_rx(self, rx: RxFrame, now: float, fast: bool = False) -> None:
        f = Frame.decode(rx.payload)
        if f is None or f.sender == self.id:
            return
        (self._fast_addr if fast else self._addr)[f.sender] = rx.src

        s = self.session
        if s is not None and f.sender == s.peer_id and f.session == s.id:
            if f.type is T.HELLO_ACK and s.initiator and not s.fast and s.inventory_time is None:
                if len(f.payload) >= HELLO.size and HELLO.unpack_from(f.payload)[5] & HELLO_USE_FAST:
                    if self.fast_link is not None:  # the responder chose ESP-NOW: follow it
                        s.reconfigure(self.fast_cfg)
                        s.fast = True
            s.on_frame(f, now)
            return
        for _, old in self._lingering:
            if f.sender == old.peer_id and f.session == old.id:
                old.on_frame(f, now)
                return
        if s is not None:
            return  # busy; also what the firmware's partner filter enforces
        if fast and not self.cfg.espnow_fallback:
            return  # without the fallback, discovery happens on LoRa only

        if f.type is T.BEACON and len(f.payload) >= BEACON.size:
            self._on_beacon(f, now, fast)
        elif f.type is T.HELLO and len(f.payload) >= HELLO.size:
            version, peer_sync, target, lat, lon, flags = HELLO.unpack_from(f.payload)
            if version == PROTOCOL_VERSION and target == self.id:  # on LoRa everyone hears every HELLO
                peer_pos = (lat / 1e7, lon / 1e7) if flags & HELLO_HAS_FIX else None
                self._start_session(f.sender, initiator=False, now=now, session_id=f.session, peer_sync=peer_sync,
                                    peer_fast_ok=bool(flags & HELLO_FAST_OK), peer_pos=peer_pos, espnow_only=fast)

    # Sessions ---------------------------------------------------------------------

    def _fast_ok(self, peer: int, now: float) -> bool:
        return self.fast_link is not None and now >= self._no_fast_until.get(peer, 0)

    def _start_session(
        self, peer: int, *, initiator: bool, now: float, session_id: int | None = None, peer_sync: int | None = None,
        peer_fast_ok: bool = False, peer_pos: tuple[float, float] | None = None, espnow_only: bool = False,
    ) -> None:
        """espnow_only: the whole exchange, handshake included, goes over fast_link (espnow_fallback)."""
        self._candidates.clear()
        self._select_at = None
        self.link.set_partner(peer)
        if self.fast_link is not None:
            self.fast_link.set_partner(peer)
        since, exclude = self._resync.get(peer, (None, frozenset()))

        my_pos = self.store.own_position(self.cfg.position_max_age) if self.fast_link is not None else None
        flags = (HELLO_HAS_FIX if my_pos else 0) | (HELLO_FAST_OK if self._fast_ok(peer, now) else 0)
        # The responder decides, from both positions, whether the exchange moves to ESP-NOW.
        use_fast = espnow_only or (
            not initiator
            and peer_fast_ok
            and bool(flags & HELLO_FAST_OK)
            and my_pos is not None
            and peer_pos is not None
            and distance_m(my_pos, peer_pos) <= self.cfg.espnow_range
        )
        if use_fast and not espnow_only:
            flags |= HELLO_USE_FAST
        lat, lon = my_pos or (0.0, 0.0)
        session: Session | None = None

        def send(f: Frame) -> None:
            if espnow_only or (session is not None and session.fast and f.type not in HANDSHAKE):
                self.fast_link.send(f.encode(), self._fast_addr.get(peer))
            else:
                self.link.send(f.encode(), self._addr.get(peer))

        session = Session(
            my_id=self.id,
            peer_id=peer,
            session_id=session_id if session_id is not None else self.rng.randrange(1, 0x10000),
            initiator=initiator,
            store=self.store,
            config=self.fast_cfg if use_fast else self.cfg,
            mtu=min(self.link.mtu, self.fast_link.mtu) if self.fast_link is not None else self.link.mtu,
            send=send,
            now=now,
            peer_sync=peer_sync,
            on_record=self._on_new_record,
            since=since,
            exclude=exclude,
            hello_info=(round(lat * 1e7), round(lon * 1e7), flags),
        )
        if peer_pos is not None:
            session.peer_position = peer_pos
        session.fast = use_fast
        session.espnow_only = espnow_only
        session.radios = (self.link.radio, (self.fast_link or self.link).radio)
        self.session = session

    def _on_new_record(self, rec: Record) -> None:
        d = rec.data
        if (
            self.on_delivered
            and rec.kind is Kind.TOMBSTONE
            and d["reason"] == "delivered"
            and d["target_origin_id"] == self.store.device_id
        ):
            self.on_delivered(d["target_origin_id"], int(d["target_seq"]))
        if self.on_record:
            self.on_record(rec)

    def _end_session(self, now: float) -> None:
        s = self.session
        assert s is not None
        self.session = None
        self._my_digests = None  # the session may have changed the database
        self.link.set_partner(None)
        if self.fast_link is not None:
            self.fast_link.set_partner(None)
        peer = s.peer_id
        if s.fast and not s.ok and not s.espnow_only:
            self._no_fast_until[peer] = now + self.cfg.espnow_retry  # out of ESP-NOW range after all
        if s.espnow_only:
            self._lora_failed.discard(peer)  # LoRa's turn again next time
        elif s.ok:
            self._lora_ok_at = now
            self._lora_failed.discard(peer)
        else:
            self._lora_failed.add(peer)  # with espnow_fallback, try ESP-NOW with this peer next
        if s.ok:
            self._fails.pop(peer, None)
            self._last_ok[peer] = now
            self._next_data[peer] = now + self.cfg.cooldown
            self._next_pos[peer] = now + self.cfg.location_cooldown
            if s.inventory_time is None or (s.delta and not (s.records_received or s.records_sent)):
                # Our digests differed, yet a change-only exchange moved nothing: something
                # older is missing on one side. Offer everything next time.
                self._resync.pop(peer, None)
            else:
                # The peer acked everything we offered. Next time, offer only what arrives
                # after this INV, minus what the peer itself just sent us.
                self._resync[peer] = (s.inventory_time - 0.01, frozenset(s.got_ids))
        else:
            # TODO: most LoRa failures in a crowd are a HELLO to a peer that is busy with someone
            # else. A BUSY reply (or a "busy" flag in beacons) would avoid counting those as
            # failures and backing off.
            n = self._fails[peer] = self._fails.get(peer, 0) + 1
            backoff = min(self.cfg.retry_backoff * 2 ** (n - 1), self.cfg.max_backoff)
            self._next_data[peer] = self._next_pos[peer] = now + backoff
        self._lingering.append((now + self.cfg.linger, s))
        if self.on_session_end:
            self.on_session_end(s)
