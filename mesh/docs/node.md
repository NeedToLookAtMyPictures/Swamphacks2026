# node.py: Node and Config

**Purpose.** A device on the mesh. It controls the duty cycle (when the radio is
on), sends beacons, decides whom to exchange with, owns at most one `Session`,
and connects the radio link to the app's database (through `AppStore`).

## Constructor

```python
Node(link, store, config=None, clock=time.time,
     on_record=None, on_delivered=None, on_session_end=None, rng=None)
```

| Argument | Meaning |
|---|---|
| `link` | A `Link` (`EspNowSerialLink` on hardware, `SimLink` in tests) |
| `store` | `AppStore(Database(...))`: the app's database ([store.md](store.md)) |
| `config` | A `Config`; defaults below |
| `clock` | **Must return GPS- or phone-synced Unix time.** All discovery windows depend on it. |
| `on_record(rec)` | Called for every record a peer sent that changed the app's database |
| `on_delivered(origin_id, seq)` | Called when the receiver's `delivered` tombstone for one of **our** messages reaches us (see below) |
| `on_session_end(session)` | Called when a session finishes, successful or not. Check `session.ok`. |
| `rng` | `random.Random`. Pass a seeded one for repeatable tests. |

The node's id on the air is `store.mesh_id`, derived from the app's `device_id`.

## App-facing API

| Call | Effect |
|---|---|
| `node.poll(now=None)` | Does all the work. **Call every few ms.** |
| `node.run_forever(poll_interval=0.005)` | `poll()` in a loop with a short sleep. Run it in its own thread if your app has a UI. |

There's no "post" call. The app writes to its own database as usual
(`add_message`, `add_locale`, `set_location`), and the node offers whatever is
new at the next encounter. The node doesn't make location pins of its own; the
app already saves the user's position once a minute.

## Delivery receipts

A sender's message is **pending** until the receiver's `delivered` tombstone
travels back through the mesh to the sender. When it arrives, the app's
`merge()` marks the message dead (delivered), and the node calls
`on_delivered(origin_id, seq)`. A message still pending 31 days after
`time_created` is **lost**. That's for the app to decide, since it holds
`time_created`. Tombstones with another reason (for example one the server
issues) mark the message dead but don't call `on_delivered`.

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

A beacon makes its sender a **candidate** if all of these hold:
1. The beacon's version equals `PROTOCOL_VERSION`.
2. **Our id is lower than the sender's.** Only one side starts, so the two
   never send HELLO at the same time.
3. Either the **data digest** (everything but positions) differs and
   `now >= next_data[peer]`, or the **position digest** differs and
   `now >= next_pos[peer]` (cooldowns and backoff, below). Identical sets mean
   nothing to exchange. The time checks run first, because computing our
   digests reads the app's database.

`select_delay` (1 s) after the first candidate, but no later than the end of
the window's beacons, the node picks one of the candidates heard in this window:
**least recently synced first** (never synced before all others), then peers
with non-position changes, then at random. Then it sends HELLO. That way a
group of hikers all get paired over time, rather than the same two re-syncing.

The higher-id device never initiates. It waits for the lower-id device's
HELLO. Both send beacons in every window, so the lower-id device normally
hears one.

### When is a HELLO accepted?

Whenever we're idle, the version matches, and the HELLO's **target id is
ours** (on LoRa every device hears every HELLO). **The responder does not check its
own cooldown for that peer**; the cooldown is enforced only by the initiator,
which is enough because only the initiator starts sessions.

## Starting and ending a session

`_start_session(peer, initiator, ...)`:
1. `link.set_partner(peer)`: the firmware now drops other devices' frames.
2. Create the `Session`. Its `send` targets the peer's MAC if we know it,
   otherwise it broadcasts.

`_end_session(now)`:
1. `link.set_partner(None)`.
2. On success: `next_data = now + cooldown` (10 s), `next_pos = now +
   location_cooldown` (10 s; TODO: 10 min), and remember `(INV time, ids the
   peer sent us)`. The next session with this peer then lists only records
   created or received after that time, minus those ids (a **change-only
   INV**). If a change-only session moves nothing, even though the digests
   differed, the next one lists everything again.
   On failure: back off `retry_backoff × 2^(failures in a row − 1)`, capped
   at `max_backoff` (10, 20, 40 … 300 s).
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
| `cooldown` | 10 s | After a successful sync, gap before re-syncing with that peer if anything but positions differs |
| `location_cooldown` | 10 s | Same, if only positions differ. TODO: 10 min |
| `select_delay` | 1 s | Collect beacons this long before picking a peer |
| `retry_backoff` / `max_backoff` | 10 s / 300 s | Gap after a failed sync, doubling per failure in a row |
| `hello_interval` / `hello_retries` | 0.3 s / 10 | Handshake retry pacing |
| `rto` / `max_retries` | 0.25 s / 12 | See [reliable.md](reliable.md) |
| `window_frames` | 16 | Max unacked frames in flight |
| `idle_timeout` | 4 s | Peer silent this long: session fails |
| `linger` | 3 s | Keep ACKing a finished session's frames this long |
| `ack_delay` / `ack_max` | 0 / 0 | Delayed ACKs (LoRa); 0 = ACK every frame at once |

`Config.lora(params)` derives LoRa values from airtime; see [lora.md](lora.md).
| `accept_kinds` | all | Kinds we request from peers (e.g. leave GEOLOCATION out to save airtime) |

## Assumptions

- **Clocks are synced** to within `guard`. Nothing in this code syncs time.
  The app must feed `clock` from GPS or the phone.
- **Device ids are unique.** Two devices with the same `mesh_id` would ignore
  each other's frames as their own.
- **`poll()` is called often** (every 5 ms or so). Retransmit and beacon timing
  is only as precise as the polling.
- **There's one radio channel.** The firmware fixes it at channel 1.

## Known gaps

- **Some peer pairs loop forever.** If `accept_kinds` excludes some kinds, two
  devices' digests never match, so they run a (cheap, nothing-to-send) session
  again every `cooldown`.
- **The cooldown is per peer and ignores new data.** If a peer writes something
  new 10 s after a sync (including the `delivered` tombstone the sync itself
  created), it won't reach us until the 120 s cooldown expires.
- **A node won't start with a lower-id peer it can only hear one way.** If the
  lower-id device never hears the higher-id device's beacons, they never pair,
  even though the higher-id device hears the lower one fine.
- **No LoRa trigger.** Issue #2 has LoRa contact turn ESP-NOW on. Today
  ESP-NOW discovers peers on its own schedule. Once LoRa exists, it could call
  something like `node.wake_for(peer)` to open an ad-hoc window.
