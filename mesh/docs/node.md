# node.py: Node and Config

**Purpose.** A device on the mesh. It controls the duty cycle (when the radio is
on), sends beacons, decides whom to exchange with, owns at most one `Session`,
and connects the radio link to the store. Your app interacts mostly with this class.

## Constructor

```python
Node(device_id, link, store, config=None, clock=time.time,
     position=None, on_record=None, on_session_end=None, rng=None)
```

| Argument | Meaning |
|---|---|
| `device_id` | Nonzero u32, unique per user. Raises `ValueError` otherwise. |
| `link` | A `Link` (`EspNowSerialLink` on hardware, `SimLink` in tests) |
| `store` | A `RecordStore` |
| `config` | A `Config`; defaults below |
| `clock` | **Must return GPS- or phone-synced Unix time.** All discovery windows depend on it. |
| `position` | Function returning `(lat, lon, heading)` or None. Used for the automatic pin. |
| `on_record(rec)` | Called for every new record received over the mesh |
| `on_session_end(session)` | Called when a session finishes, successful or not. Check `session.ok`. |
| `rng` | `random.Random`. Pass a seeded one for repeatable tests. |

## App-facing API

| Call | Effect |
|---|---|
| `node.poll(now=None)` | Does all the work. **Call every few ms.** |
| `node.run_forever(poll_interval=0.005)` | `poll()` in a loop with a short sleep. Run it in its own thread if your app has a UI. |
| `node.post(record)` | `store.put(record)`. The record goes out at the next encounter. |

## `poll(now)`: control flow

```
1. for each frame from link.poll(): _on_rx(frame)
2. if there is a session: session.tick(now); if it finished: _end_session
3. drop lingering sessions older than `linger` (3 s)
4. awake = always_on OR a session is active OR now is inside a discovery window
   if awake changed: link.set_radio(awake)
5. if awake and no session: _maybe_beacon(now)
```

**A session keeps the radio on** past the end of the window, until it finishes.
This matches the issue's note that the exchange continues past the window.

## Discovery schedule

This replaces the issue's "LoRa timing" and transmit/listen slot patterns for ESP-NOW.

```
window k is open from (k*period - guard) to (k*period + window + guard)
```

With the defaults (`period=20, window=2, guard=0.5`), the radio is on from
19.5 s to 22.5 s, 39.5 s to 42.5 s, and so on. That's 3 s out of every 20, or 15% duty cycle.

- **Beacons:** when a new window starts, the node picks `beacons_per_window`
  (2) random times inside `[k*period, k*period + window]` and sends a beacon at
  each one. Beacons go only in the core window. The guard time is for
  listening, to cover clock drift.
- **Demo mode (`always_on=True`):** the radio is always on, with a beacon every
  `beacon_interval` × random(0.7–1.3) seconds.

**Why no transmit/listen slots** (the issue's ID-derived patterns): the ESP-NOW
radio receives continuously whenever it's on and not transmitting, and a beacon
is under 1 ms on air. Random beacon times plus the radio's built-in
listen-before-talk make it very unlikely that two devices miss each other in a
shared window. That pattern design may still matter for LoRa.

## `_on_rx(frame)`: routing incoming frames

```
f = Frame.decode(payload); drop if malformed or sent by us
remember sender_id -> link address (MAC)

if a session is active and f matches (sender == peer, session id == session's): give it to the session
elif f matches a lingering session: give it to that session (so its last ACKs still go out)
elif a session is active: drop it (busy)
elif BEACON:  maybe start a session as initiator (rules below)
elif HELLO:   start a session as responder, if the version matches
else:         drop it
```

### When does a beacon start a session?

All of these must hold:
1. The beacon's version equals `PROTOCOL_VERSION`.
2. **Its digest differs from ours** (identical sets mean nothing to exchange).
3. **Our id is lower than the sender's.** Only one side starts, so the two
   never send HELLO at the same time.
4. `now >= next_allowed[peer]` (cooldown or backoff, below).

The higher-id device never initiates. It waits for the lower-id device's
HELLO. Both send beacons in every window, so the lower-id device normally
hears one.

### When is a HELLO accepted?

Whenever we're idle and the version matches. **The responder does not check its
own cooldown for that peer**; the cooldown is enforced only by the initiator,
which is enough because only the initiator starts sessions.

## Starting and ending a session

`_start_session(peer, initiator, ...)`:
1. `_add_own_pin(now)`: if `position` is set and no pin was made in the last
   `pin_interval` (300 s), store a fresh PIN record of our own position. This
   is the issue's "location sharing on every handshake". The pin is in the
   store before the INV is built, so it goes out in this session.
2. `link.set_partner(peer)`: the firmware now drops other devices' frames.
3. Create the `Session`. Its `send` targets the peer's MAC if we know it,
   otherwise it broadcasts.

`_end_session(now)`:
1. `link.set_partner(None)`.
2. `next_allowed[peer] = now + cooldown` (120 s) on success, or
   `now + retry_backoff` (10 s) on failure.
3. Keep the session lingering for `linger` (3 s), so it can still ACK the peer's last frames.
4. Call `on_session_end(session)`.

## Config defaults

| Field | Default | Meaning |
|---|---|---|
| `period` | 20 s | Time between discovery windows |
| `window` | 2 s | Core window length (beacons go out here) |
| `guard` | 0.5 s | Extra listening time on each side for clock drift |
| `beacons_per_window` | 2 | |
| `always_on` | False | Demo mode |
| `beacon_interval` | 1 s | Beacon spacing in demo mode |
| `cooldown` | 120 s | Gap after a successful sync before re-syncing with the same peer |
| `retry_backoff` | 10 s | Gap after a failed sync |
| `hello_interval` / `hello_retries` | 0.3 s / 10 | Handshake retry pacing |
| `rto` / `max_retries` | 0.25 s / 12 | See [reliable.md](reliable.md) |
| `window_frames` | 16 | Max unacked frames in flight |
| `idle_timeout` | 4 s | Peer silent this long: session fails |
| `linger` | 3 s | Keep ACKing a finished session's frames this long |
| `accept_kinds` | all | Kinds we request from peers (e.g. leave PIN out to save space) |
| `pin_interval` | 300 s | Minimum gap between automatic own-position pins |

## Assumptions

- **Clocks are synced** to within `guard`. Nothing in this code syncs time.
  The app must feed `clock` from GPS or the phone.
- **Device ids are unique.** Two devices with the same id would ignore each
  other's frames as their own.
- **`poll()` is called often** (every 5 ms or so). Retransmit and beacon timing
  is only as precise as the polling.
- **There's one radio channel.** The firmware fixes it at channel 1.

## Known gaps

- **Some peer pairs loop forever.** If `accept_kinds` excludes some kinds, two
  devices' digests never match, so they run a (cheap, nothing-to-send) session
  again every `cooldown`.
- **The cooldown is per peer and ignores new data.** If a peer posts something
  new 10 s after a sync, it won't reach us until the 120 s cooldown expires.
- **A node won't start with a lower-id peer it can only hear one way.** If the
  lower-id device never hears the higher-id device's beacons, they never pair,
  even though the higher-id device hears the lower one fine.
- **No LoRa trigger.** Issue #2 has LoRa contact turn ESP-NOW on. Today
  ESP-NOW discovers peers on its own schedule. Once LoRa exists, it could call
  something like `node.wake_for(peer)` to open an ad-hoc window.
