# Tests

Run from `mesh/`:

```bash
python3 -W ignore -m unittest discover -s tests -v
```

`-W ignore` hides harmless "unclosed database" warnings, because the tests don't
close their in-memory stores.

## test_exchange.py (simulated radio)

All of these use `SimMedium` (see [sim.md](sim.md)). Most run in demo mode
(`always_on=True`), so discovery is instant.

| Test | Setup | Proves | Does not prove |
|---|---|---|---|
| `test_two_nodes_converge` | A has a locale update and a message, B has a pin | Both end up with the same 3 records. Data flows in both directions in one session. | |
| `test_large_record_is_chunked_over_lossy_link` | 20 KB message, **30% loss** | Chunking, reassembly, ACK/retransmit recovery; the bytes arrive identical | Real-world speed |
| `test_gossip_carries_data_across_hops` | A–B in range, then only B–C | C gets A's record even though A and C never met. **This is the core gossip claim.** | |
| `test_cutoff_skips_records_both_got_from_server` | One record with server_time before the cutoff, one after (both *created* before it) | The pre-cutoff one is skipped. The one **created** before the cutoff but **uploaded** after it is still sent. (See [store.md](store.md).) | |
| `test_newer_locale_update_supersedes_older` | B has "flowing", A has a newer "dry" for the same `spring:water` key | Both devices end up with only "dry" | Keeping history (not implemented) |
| `test_walk_out_of_range_mid_transfer_then_resume` | A has an urgent locale update and a 200 KB message; the range is cut once the update arrives | The update arrives first (priority order). The session fails cleanly. After range returns, the message completes in a new session. | |
| `test_duty_cycled_nodes_meet_in_shared_window` | **Real schedule** (20 s period, 2 s window) | Synced windows find each other within one period | Clock drift (the simulator's clock is perfect) |
| `test_identical_sets_skip_exchange` | Both hold the same record | Equal digests mean no session at all | |
| `test_frames_fit_lora_sized_mtu` | 100-byte MTU, 30 messages | Every frame fits a smaller MTU (SimLink asserts it); the sync still converges | LoRa timing |

**Checking the tests can fail:** reversing the priority sort in
`session.py` makes `test_walk_out_of_range_mid_transfer_then_resume` fail, as
expected.

## test_serial_link.py (real serial code, fake ESP32)

Skipped if `pyserial` isn't installed.

| Test | Proves |
|---|---|
| `test_slip_roundtrip_with_escapes` | SLIP encode/decode handles `0xC0` and `0xDB` inside messages |
| `test_two_nodes_over_serial` | Two `EspNowSerialLink`s on Linux pseudo-terminals, each talking to a fake ESP32 written in Python, sync a locale update and a 5 KB locale update. This exercises the real serial framing, reader thread, command encoding and GET_INFO. |

The fake ESP32 (`FakeEsp32Pair`) copies the firmware's documented behaviour:
SEND delivers to the other board when the MAC matches (or on broadcast), and
it applies the partner filter, the radio on/off state and GET_INFO. **It's a
Python model of the firmware, not the firmware itself**, so it can't catch
bugs in the `.ino` file.

## Not covered by any test

- The actual firmware (needs hardware)
- Clock drift between devices
- More than 3 nodes, or several devices competing for one peer
- Protocol version mismatch
- Malformed or malicious frames (see the known gaps in [session.md](session.md))
- Persistence across restarts (`RecordStore("file.db")`)
- `accept_kinds` filtering
