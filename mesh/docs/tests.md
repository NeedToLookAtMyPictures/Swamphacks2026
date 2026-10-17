# Tests

Run from `mesh/`:

```bash
python -m unittest discover -s tests -v
```

## The app's database

The tests run against the **real** app database
(`application/user_app/database.py`), because that's what the mesh syncs on a
device. `tests/app_db.py` loads it:
- from `../application/user_app/database.py` if that file is in this checkout;
- otherwise from the `main` branch with `git show` (this branch doesn't contain
  the app yet), into a temporary file.

If neither works, the tests that need it are skipped. The app's code is never
modified. Each simulated device gets its own database file in a temporary folder.

## test_exchange.py (simulated radio)

All of these use `SimMedium` (see [sim.md](sim.md)). Most run in demo mode
(`always_on=True`, with a 5 s cooldown so repeat syncs happen quickly).

| Test | Setup | Proves | Does not prove |
|---|---|---|---|
| `test_two_nodes_converge` | A has a locale comment, B has a position | Both end up with both records. Data flows both ways in one session. | |
| `test_delivered_tombstone_kills_message_and_reaches_sender` | A writes to carol; B relays; then B meets C; then B meets A and D (who never saw the message) | C receives it and its app creates a `delivered` tombstone. Relay B **deletes** the message but still carries the tombstone to A and D. At A, `on_delivered` fires and A's copy is kept, marked dead; C also keeps its copy. No device offers the dead message any more. **The core tombstone flow.** | |
| `test_server_tombstone_kills_message_without_delivery` | B holds a `revoked` tombstone (as if from the server) for A's message | A's copy is marked dead, B never takes the message, and `on_delivered` does **not** fire | |
| `test_broadcast_message_is_never_tombstoned` | A messages `*` | B receives it alive, and no tombstone is created | |
| `test_expired_message_is_not_offered` | A's message, with the app's clock 32 days later | A keeps it (dead) but doesn't offer it; B never receives it | |
| `test_large_record_is_chunked_over_lossy_link` | 20 KB comment, **30% loss** | Chunking, reassembly, ACK/retransmit recovery; the bytes arrive identical | Real-world speed |
| `test_1mb_record_is_split_into_250_byte_frames` | 1 MB message; the links would accept 1500-byte frames | It arrives byte-identical, and **no frame exceeds 250 bytes** even though the link would allow it | |
| `test_gossip_carries_data_across_hops` | A–B in range, then only B–C | C gets A's record even though A and C never met. **This is the core gossip claim.** | |
| `test_cutoff_counts_records_created_or_received_after_older_sync` | A last synced 2 days ago, B 1 day ago. A has a 3-day-old comment and one it got offline 1.5 days ago; B has a 5-day-old comment it downloaded 1 day ago | The cutoff is A's sync. B gets A's offline comment but not the 3-day-old one; A gets B's comment because B *received* it after the cutoff, even though it was created long before | |
| `test_newest_position_wins` | A has a newer position for a user than B | Both end up with the newer one (the app's rule) | |
| `test_walk_out_of_range_mid_transfer_then_resume` | A has an urgent locale comment and a 200 KB message; the range is cut once the comment arrives | The comment arrives first (priority order). The session fails cleanly. After range returns, the message completes in a new session. | |
| `test_duty_cycled_nodes_meet_in_shared_window` | **Real schedule** (20 s period, 2 s window) | Synced windows find each other within one period | Clock drift (the simulator's clock is perfect) |
| `test_identical_sets_skip_exchange` | Both hold the same record | Equal digests mean no session at all | |
| `test_frames_fit_lora_sized_mtu` | 100-byte MTU, 30 comments | Every frame fits a smaller MTU (SimLink asserts it); the sync still converges | LoRa timing |
| `test_malformed_chunks_are_dropped` | DATA frames with an index past the count, a changing count, a blob too short for its CRC, a wrong CRC, and a valid record that wasn't requested | Each is dropped without raising; nothing reaches the app | |

`RecordTests` check that every kind encodes and decodes back to the same row
and id, that `is_dead` doesn't change a message's id, and that truncated or
padded bytes and unknown kinds are rejected.

**Checking the tests can fail:**
- reversing the priority sort in `session.py` makes
  `test_walk_out_of_range_mid_transfer_then_resume` fail;
- letting `AppStore` offer dead messages, or turning off the relay deletion,
  makes `test_delivered_tombstone_kills_message_and_reaches_sender` fail;
- ignoring receive time in the cutoff makes
  `test_cutoff_counts_records_created_or_received_after_older_sync` fail.

## test_serial_link.py (real serial code, fake ESP32)

Skipped if `pyserial` isn't installed, or if the app's database can't be loaded.

| Test | Proves |
|---|---|
| `test_slip_roundtrip_with_escapes` | SLIP encode/decode handles `0xC0` and `0xDB` inside messages |
| `test_two_nodes_over_serial` | Two `EspNowSerialLink`s, each talking to a fake ESP32 written in Python, sync a small locale comment and a 5 KB one. The 5 KB record is split into ~23 frames, and the fake checks that every frame handed to the radio is ≤ 250 bytes. This exercises the real serial framing, reader thread, command encoding and GET_INFO. Each fake board is a local TCP server reached through pyserial's `socket://` URL, so the test runs on Windows, macOS and Linux. |

The fake ESP32 (`FakeEsp32Pair`) copies the firmware's documented behaviour:
SEND delivers to the other board when the MAC matches (or on broadcast), and
it applies the partner filter, the radio on/off state and GET_INFO. **It's a
Python model of the firmware, not the firmware itself**, so it can't catch
bugs in the `.ino` file.

## test_app_integration.py (web API + mesh on one database)

Needs Flask (`pip install -r application/user_app/requirements.txt`); skipped without it.
Two app instances (alice, bob), each with its own Flask API, app database and
mesh node on the simulated radio. Data goes in and comes out **only through the
HTTP API the web interface uses**.

| Test | Proves |
|---|---|
| `test_message_posted_in_one_app_appears_in_the_other_and_is_delivered` | `POST /api/messages` on alice, then the mesh, then `GET /api/messages` on bob shows it. Bob's app marks it delivered, the tombstone comes back over the mesh, alice's copy turns `is_dead`, and `on_delivered` fires. |
| `test_locale_comment_and_position_reach_the_peer` | A locale comment (`POST /api/locales`) and a position (`PUT /api/location`) from alice show up in bob's `/api/locales` and `/api/contacts` |

Sabotaging the mesh (`AppStore.has` always True, so nothing is ever requested)
makes both tests fail.

## Not covered by any test

- The actual firmware (needs hardware)
- Clock drift between devices
- More than 3 nodes, or several devices competing for one peer
- Protocol version mismatch
- The app writing to its database while a session is running
- `accept_kinds` filtering
