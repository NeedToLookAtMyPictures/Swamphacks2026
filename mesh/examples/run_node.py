"""Run one trailmesh node against a real ESP32 running firmware/espnow_bridge.

    pip install -r requirements.txt
    python examples/run_node.py --port /dev/ttyACM0 --id 1 --post "spring at mile 1203 is dry"
    python examples/run_node.py --port /dev/ttyACM1 --id 2

Use --demo for always-on radio (instant discovery) instead of the 20 s duty cycle.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import Config, Node, Record, RecordStore  # noqa: E402
from trailmesh.espnow_serial import EspNowSerialLink  # noqa: E402
from trailmesh.wire import Frame  # noqa: E402


def trace(link):
    """Wrap a link so every frame sent and received is printed (for --verbose)."""
    def describe(payload):
        f = Frame.decode(payload)
        if f is None:
            return f"unparsed {len(payload)} B"
        return f"{f.type.name} dev={f.sender} sess={f.session} seq={f.seq} {len(f.payload)} B"

    send, poll, set_partner = link.send, link.poll, link.set_partner

    def traced_send(payload, dest=None):
        print(f"  tx -> {dest.hex(':') if dest else 'broadcast'}: {describe(payload)}")
        send(payload, dest)

    def traced_poll():
        frames = poll()
        for rx in frames:
            print(f"  rx <- {rx.src.hex(':') if rx.src else '?'} ({rx.rssi} dBm): {describe(rx.payload)}")
        return frames

    def traced_partner(device_id):
        print(f"  partner filter -> {device_id}")
        set_partner(device_id)

    link.send, link.poll, link.set_partner = traced_send, traced_poll, traced_partner
    return link


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--id", type=int, required=True, help="device id (nonzero, unique)")
    ap.add_argument("--db", default=None, help="sqlite file (default: in-memory)")
    ap.add_argument("--post", action="append", default=[], help="locale update text to author at startup")
    ap.add_argument("--demo", action="store_true", help="radio always on")
    ap.add_argument("--verbose", action="store_true", help="print every frame sent and received")
    args = ap.parse_args()

    store = RecordStore(args.db or ":memory:")
    for text in args.post:
        store.put(Record.locale_update(args.id, int(time.time()), "spring-1203", "water", text))

    def on_record(r: Record):
        print(f"<- new {r.kind.name} from device {r.origin}: {r.text()!r}")

    def on_end(s):
        print(f"session with {s.peer_id} {'ok' if s.ok else 'FAILED'}: "
              f"got {s.records_received}, sent {s.records_sent}")

    link = EspNowSerialLink(args.port)
    if args.verbose:
        trace(link)
    node = Node(args.id, link, store, Config(always_on=args.demo), on_record=on_record, on_session_end=on_end)
    print(f"node {args.id} running with {len(store.all())} records, digest {store.digest().hex()}; ctrl-c to stop")
    time.sleep(1)
    link.request_status()  # the radio was switched on at connect; check it actually came up
    time.sleep(0.5)
    st = link.status
    if link.mac is None:
        print("board: NO REPLY FROM FIRMWARE (wrong port or not flashed?)")
    elif st is None:
        print(f"board {link.mac.hex(':')}: old firmware without status; re-flash firmware/espnow_bridge")
    elif not st["radio_on"]:
        print(f"board {link.mac.hex(':')}: RADIO FAILED TO START (error {st['last_err']:#x}); see [esp32] log above")
    else:
        print(f"board {link.mac.hex(':')}: radio on, channel {st['channel']}")
    try:
        node.run_forever()
    except KeyboardInterrupt:
        link.close()


if __name__ == "__main__":
    main()
