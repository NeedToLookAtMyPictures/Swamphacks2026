"""In-memory radio simulator for tests and demos (no hardware needed).

Uses a virtual clock so minutes of trail time run in milliseconds.

By default frames arrive after a fixed latency (ESP-NOW-like). Pass `airtime`
(e.g. LoRaParams().airtime) to model a slow half-duplex radio like LoRa: each
link sends one frame at a time, a frame occupies the channel for its airtime,
overlapping frames at a receiver are both lost, a radio can't hear while it
transmits, and (with `lbt`) a link waits while it hears someone else.
"""
from __future__ import annotations

import heapq
import itertools
import random
from collections import deque
from typing import Callable

from .link import Link, RxFrame


class SimClock:
    def __init__(self, start: float = 1_760_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class SimMedium:
    def __init__(self, clock: SimClock, loss: float = 0.0, latency: float = 0.002, seed: int = 0,
                 airtime: Callable[[int], float] | None = None, lbt: bool = True):
        self.clock = clock
        self.loss = loss
        self.latency = latency
        self.rng = random.Random(seed)
        self.links: dict[bytes, SimLink] = {}
        # Override to model distance / hikers walking apart.
        self.in_range: Callable[[bytes, bytes], bool] = lambda a, b: True
        self.airtime = airtime
        self.lbt = lbt
        self._on_air: list[tuple[SimLink, float, float]] = []  # (sender, start, end) of recent transmissions
        self.collisions = 0

    def link(self, name: str, mtu: int = 250) -> SimLink:
        addr = name.encode()
        lk = SimLink(self, addr, mtu)
        lk.radio = "LoRa" if self.airtime is not None else "ESP-NOW"
        self.links[addr] = lk
        return lk

    def _transmit(self, src: SimLink, payload: bytes, dest: bytes | None) -> None:
        if not src.radio_on:
            return
        if self.airtime is not None:
            src._txq.append(payload)  # a slow radio: sent from _service, one at a time
            self._service(src)
            return
        targets = self.links.values() if dest is None else [self.links[dest]] if dest in self.links else []
        for lk in targets:
            if lk is src or not self.in_range(src.addr, lk.addr) or self.rng.random() < self.loss:
                continue
            lk._deliver(self.clock.now + self.latency, RxFrame(payload, src.addr, -60))

    # Airtime model ------------------------------------------------------------

    def _service(self, src: SimLink) -> None:
        """Start src's next queued frame if its radio is free (and, with lbt, the channel sounds clear)."""
        now = self.clock.now
        if not src._txq or now < src._tx_busy_until or now < src._tx_retry_at:
            return
        self._on_air = [t for t in self._on_air if t[2] > now - 60]
        # TODO: listen-before-talk here is perfect; the firmware's CAD misses some packets.
        if self.lbt and any(s is not src and start <= now < end and self.in_range(s.addr, src.addr)
                            for s, start, end in self._on_air):
            frame = self.airtime(250)
            src._tx_retry_at = now + self.rng.uniform(0.25, 1.25) * frame
            return
        payload = src._txq.popleft()
        end = now + self.airtime(len(payload))
        tx = (src, now, end)
        self._on_air.append(tx)
        src._tx_busy_until = end
        for lk in self.links.values():
            if lk is not src and self.in_range(src.addr, lk.addr):
                lk._deliver(end, (tx, RxFrame(payload, src.addr, -90)))

    def _survives(self, tx: tuple, rx: SimLink) -> bool:
        """A frame is lost if the receiver transmitted, or heard anyone else, while it was on the air."""
        src, start, end = tx
        for other in self._on_air:
            s, s_start, s_end = other
            if other is tx or s_start >= end or s_end <= start:
                continue
            if s is rx or self.in_range(s.addr, rx.addr):
                self.collisions += 1
                return False
        return self.rng.random() >= self.loss


class SimLink(Link):
    _counter = itertools.count()

    def __init__(self, medium: SimMedium, addr: bytes, mtu: int):
        self.medium = medium
        self.addr = addr
        self.mtu = mtu
        self.radio_on = True
        self.partner: int | None = None
        self._queue: list = []
        self._txq: deque[bytes] = deque()  # airtime model only
        self._tx_busy_until = 0.0
        self._tx_retry_at = 0.0

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        assert len(payload) <= self.mtu, f"frame of {len(payload)} bytes exceeds mtu {self.mtu}"
        self.medium._transmit(self, payload, dest)

    def _deliver(self, at: float, rx: RxFrame) -> None:
        heapq.heappush(self._queue, (at, next(self._counter), rx))

    def poll(self) -> list[RxFrame]:
        if self.medium.airtime is not None:
            self.medium._service(self)
        out = []
        while self._queue and self._queue[0][0] <= self.medium.clock.now:
            _, _, rx = heapq.heappop(self._queue)
            if isinstance(rx, tuple):  # airtime model: (transmission, frame)
                tx, rx = rx
                if not self.medium._survives(tx, self):
                    continue
            if not self.radio_on:
                continue  # radio was asleep when it arrived
            # Same filter the ESP32 firmware applies: first 4 bytes are the sender id.
            if self.partner is not None and int.from_bytes(rx.payload[:4], "little") != self.partner:
                continue
            out.append(rx)
        return out

    def set_radio(self, on: bool) -> None:
        self.radio_on = on
        if not on:
            self._txq.clear()  # like the LoRa firmware: queued frames are lost when the radio sleeps

    def set_partner(self, device_id: int | None) -> None:
        self.partner = device_id


def run(nodes, clock: SimClock, seconds: float, dt: float = 0.005, until: Callable[[], bool] | None = None) -> float:
    """Advance the simulation. Returns elapsed simulated seconds."""
    start = clock.now
    end = start + seconds
    while clock.now < end:
        for n in nodes:
            n.poll()
        if until is not None and until():
            break
        clock.now += dt
    return clock.now - start
