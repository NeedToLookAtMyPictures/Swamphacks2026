"""Run one trailmesh node against a real ESP32 running firmware/espnow_bridge.

The node syncs the user app's database (application/user_app/data/user.db by
default), so run it next to the app. Set the device up once in the app first
(POST /api/device), or pass --name to create it here.

    pip install -r requirements.txt
    python examples/run_node.py --port COM3 --name alice --demo
    python examples/run_node.py --port /dev/ttyUSB1 --db other.db --name bob --demo

Use --demo for always-on radio (instant discovery) instead of the 20 s duty cycle.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "application", "user_app"))

from database import DEFAULT_DB_PATH, Database  # noqa: E402  (the app's database)

from trailmesh import AppStore, Config, Node, Record  # noqa: E402
from trailmesh.espnow_serial import EspNowSerialLink  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="serial port of the ESP32, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="the app's database file")
    ap.add_argument("--name", help="create the device with this name if it isn't set up yet")
    ap.add_argument("--demo", action="store_true", help="radio always on")
    args = ap.parse_args()

    db = Database(args.db)
    if db.get_device() is None:
        if not args.name:
            sys.exit("this database has no device yet: pass --name, or set it up in the app")
        db.init_device(args.name)
    store = AppStore(db)

    def on_record(r: Record):
        print(f"<- {r.kind.name}: {r.data}")

    def on_delivered(origin_id, seq):
        print(f"<- message {origin_id}/{seq} was delivered")

    def on_end(s):
        print(f"session with {s.peer_id} {'ok' if s.ok else 'FAILED'}: "
              f"got {s.records_received}, sent {s.records_sent}")

    link = EspNowSerialLink(args.port)
    node = Node(link, store, Config(always_on=args.demo), on_record=on_record, on_delivered=on_delivered,
                on_session_end=on_end)
    print(f"node {db.get_device()['name']} (mesh id {node.id:08x}) running; ctrl-c to stop")
    try:
        node.run_forever()
    except KeyboardInterrupt:
        link.close()


if __name__ == "__main__":
    main()
