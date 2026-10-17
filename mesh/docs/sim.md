# sim.py: simulated radio

**Purpose.** Lets the full protocol run without hardware, quickly and
repeatably, so tests can cover loss, range changes and multiple hops.

## Classes

### `SimClock`
A callable that returns `.now` (starting at a realistic Unix time). Tests move
time forward by changing `.now`. Pass it to `Node(clock=...)`.

### `SimMedium(clock, loss=0.0, latency=0.002, seed=0)`
The shared airspace.
- `medium.link(name, mtu=250)` creates a `SimLink` whose address is `name.encode()`.
- `medium.in_range`: a function `(addr_a, addr_b) -> bool`, default always
  True. Tests replace it to model hikers meeting and walking apart.
- `loss` is the probability that any **one** delivery is dropped, decided
  independently per receiver and per frame.

### `SimLink`
A `Link` with the same filtering behaviour as the firmware.
- `send`: asserts the frame fits the MTU. If this radio is on, the frame is
  delivered to every in-range link (broadcast) or to the one destination, after
  `latency` seconds.
- `poll`: returns frames whose delivery time has passed. Frames that arrive
  **while the receiving radio is off** are dropped. **The partner filter is
  emulated**: if a partner is set, frames whose first 4 bytes are a different
  sender id are dropped.

### `run(nodes, clock, seconds, dt=0.005, until=None)`
Calls `poll()` on every node, advances the clock by `dt`, and repeats until
`seconds` have passed or `until()` returns True. Returns the simulated time elapsed.

## What the simulator does NOT model

Keep these in mind when reading test results:

| Not modeled | Consequence |
|---|---|
| **Airtime / bandwidth** | A 200 KB transfer finishes in under a second of simulated time. Real ESP-NOW is much slower. **Simulated times say nothing about real speed.** |
| **Collisions** | Two simultaneous transmissions both arrive. The real radio's listen-before-talk mostly prevents collisions, but not completely. |
| **Half duplex** | A sending link still receives. |
| **Distance and signal fading** | Range is a yes/no function, and loss is independent per frame. Real loss comes in bursts. |
| **Clock drift** | Every node shares one perfect clock. |
| **USB serial delay and buffering** | Tested separately in `test_serial_link.py`, using a fake ESP32. |
| **Radio wake-up time** | `set_radio(True)` takes effect immediately. |
