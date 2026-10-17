"""In-memory radio simulator for tests and demos (no hardware needed).

Uses a virtual clock so minutes of trail time run in milliseconds.
"""
from __future__ import annotations

import heapq
import itertools
import random
from typing import Callable

from .link import Link, RxFrame


class SimClock:
    def __init__(self, start: float = 1_760_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class SimMedium:
    def __init__(self, clock: SimClock, loss: float = 0.0, latency: float = 0.002, seed: int = 0):
        self.clock = clock
        self.loss = loss
        self.latency = latency
        self.rng = random.Random(seed)
        self.links: dict[bytes, SimLink] = {}
        # Override to model distance / hikers walking apart.
        self.in_range: Callable[[bytes, bytes], bool] = lambda a, b: True

    def link(self, name: str, mtu: int = 250) -> SimLink:
        addr = name.encode()
        lk = SimLink(self, addr, mtu)
        self.links[addr] = lk
        return lk

    def _transmit(self, src: SimLink, payload: bytes, dest: bytes | None) -> None:
        if not src.radio_on:
            return
        targets = self.links.values() if dest is None else [self.links[dest]] if dest in self.links else []
        for lk in targets:
            if lk is src or not self.in_range(src.addr, lk.addr) or self.rng.random() < self.loss:
                continue
            lk._deliver(self.clock.now + self.latency, RxFrame(payload, src.addr, -60))


class SimLink(Link):
    _counter = itertools.count()

    def __init__(self, medium: SimMedium, addr: bytes, mtu: int):
        self.medium = medium
        self.addr = addr
        self.mtu = mtu
        self.radio_on = True
        self.partner: int | None = None
        self._queue: list = []

    def send(self, payload: bytes, dest: bytes | None = None) -> None:
        assert len(payload) <= self.mtu, f"frame of {len(payload)} bytes exceeds mtu {self.mtu}"
        self.medium._transmit(self, payload, dest)

    def _deliver(self, at: float, rx: RxFrame) -> None:
        heapq.heappush(self._queue, (at, next(self._counter), rx))

    def poll(self) -> list[RxFrame]:
        out = []
        while self._queue and self._queue[0][0] <= self.medium.clock.now:
            _, _, rx = heapq.heappop(self._queue)
            if not self.radio_on:
                continue  # radio was asleep when it arrived
            # Same filter the ESP32 firmware applies: first 4 bytes are the sender id.
            if self.partner is not None and int.from_bytes(rx.payload[:4], "little") != self.partner:
                continue
            out.append(rx)
        return out

    def set_radio(self, on: bool) -> None:
        self.radio_on = on

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
