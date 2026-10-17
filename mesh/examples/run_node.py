"""Run one trailmesh node against a real ESP32 running firmware/espnow_bridge.

    pip install -r requirements.txt
    python examples/run_node.py --port /dev/ttyUSB0 --id 1 --post "spring at mile 1203 is dry"
    python examples/run_node.py --port /dev/ttyUSB1 --id 2

Use --demo for always-on radio (instant discovery) instead of the 20 s duty cycle.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import Config, Node, Record, RecordStore  # noqa: E402
from trailmesh.espnow_serial import EspNowSerialLink  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--id", type=int, required=True, help="device id (nonzero, unique)")
    ap.add_argument("--db", default=None, help="sqlite file (default: in-memory)")
    ap.add_argument("--post", action="append", default=[], help="locale update text to author at startup")
    ap.add_argument("--demo", action="store_true", help="radio always on")
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
    node = Node(args.id, link, store, Config(always_on=args.demo), on_record=on_record, on_session_end=on_end)
    print(f"node {args.id} running with {len(store.all())} records; ctrl-c to stop")
    try:
        node.run_forever()
    except KeyboardInterrupt:
        link.close()


if __name__ == "__main__":
    main()
