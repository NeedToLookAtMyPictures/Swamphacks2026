# reliable.py: ACKs and retransmission

**Purpose.** The packet loss recovery from issue #2: *"The receiver confirms
each packet it gets, and the sender resends any packet that isn't confirmed
within a timeout."* It also hands frames to the session in order, so the
session can rely on "the whole INV arrives before the WANT."

There are two classes, one per direction. Each session owns one of each.

## ReliableSender

State: `next_seq` (counter) and `_unacked`, a map of `seq -> [frame, last_sent_time, tries]`.

| Method | What it does |
|---|---|
| `can_send()` | True if fewer than `window` (default 16) frames are unacked. This is the flow control. |
| `send(frame, now)` | Assigns the next seq, records it as unacked, returns the frame (the caller transmits it) |
| `on_ack(payload)` | Removes every seq below `next_expected`, plus any seq marked in the SACK bitmap |
| `due(now)` | Returns frames unacked for at least `rto` (0.25 s) and marks them re-sent. Raises `RetriesExhausted` if any frame was already sent more than `max_retries` (12) times. |
| `idle` | True when nothing is unacked |
| `unacked_types()` | Set of frame types still unacked (used for the DONE special case in [session.md](session.md)) |

Worst case before a session gives up because of retries: about 13 sends × 0.25 s ≈ 3.25 s.
The session's 4 s idle timeout usually triggers first if the peer is truly gone.

## ReliableReceiver

State: `expected` (next seq to deliver) and `_buffer`, out-of-order frames
waiting for a gap to fill.

`on_frame(frame)` returns a list of frames now deliverable in order:

```
if expected <= frame.seq < expected + 64:  buffer it (duplicates ignored)
while expected is in buffer: pop it into the output and increment expected
return output
```

- A frame with `seq < expected` (already delivered, so this is a duplicate)
  is ignored.
- A frame 64 or more ahead is dropped. The sender only has 16 in flight, so
  this only happens with a misbehaving peer.
- **The caller still sends an ACK for duplicates**, which is how a sender
  whose earlier ACK was lost stops retransmitting.

`ack_payload()` builds the ACK:

```
next_expected = expected            # "I have everything below this"
sack bit i    = 1 if (expected + 1 + i) is buffered, for i in 0..31
```

`expected` itself is never buffered (it would have been delivered already), so
the bitmap starts at `expected + 1`.

## Example with loss

The sender sends seqs 0–4, and seq 1 is lost.

| Receiver gets | Delivered | ACK sent |
|---|---|---|
| 0 | [0] | next=1, sack=0 |
| 2 | [] | next=1, sack=0b1 (2 held) |
| 3 | [] | next=1, sack=0b11 |
| 4 | [] | next=1, sack=0b111 |

The sender drops 0, 2, 3 and 4 from `_unacked`. After `rto`, it retransmits only seq 1.

| 1 (retransmit) | [1, 2, 3, 4] | next=5, sack=0 |

## How this compares to TCP

| | TCP | Here |
|---|---|---|
| Cumulative ACK | yes | yes |
| Selective ACK | optional | always (32-bit bitmap) |
| Retransmit timeout | adapts to measured round-trip time | **fixed `rto`** |
| Congestion control | yes | **no**, just a fixed window of 16 |
| Delayed / combined ACKs | yes | **no**: one ACK per reliable frame |

## Assumptions and gaps

- **The fixed `rto` (0.25 s) suits ESP-NOW over USB serial on a laptop.** LoRa
  frames take 100 ms to 1 s+ of airtime, so LoRa needs an `rto` of several
  seconds and a much smaller window.
- **One ACK per frame doubles the frame count.** That's fine on ESP-NOW, but
  wasteful on LoRa. Acknowledging every N frames, or after a short delay,
  would fix it.
- **Retransmits come in bursts.** If many frames go overdue at the same
  moment, `due()` returns all of them at once.
