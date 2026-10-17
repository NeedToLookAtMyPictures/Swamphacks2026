"""Run one trailmesh node against a real ESP32 running firmware/espnow_bridge (or lora_bridge: --link lora).

The node syncs the user app's database (application/user_app/data/user.db by
default), so run it next to the app. Set the device up once in the app first
(POST /api/device), or pass --name to create it here.

    pip install -r requirements.txt
    python examples/run_node.py --port /dev/ttyACM0 --name alice --demo
    python examples/run_node.py --port /dev/ttyACM1 --db other.db --name bob --demo
    (Windows: --port COM3. Prefer /dev/serial/by-id/... on Linux: ttyACM numbers can swap.)

Use --demo for always-on radio (instant discovery) instead of the 20 s duty cycle.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "application", "user_app"))

from database import DEFAULT_DB_PATH, Database  # noqa: E402  (the app's database)

from trailmesh import AppStore, Node, Record  # noqa: E402
from trailmesh.radio import LINKS, config_for, open_link  # noqa: E402
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
    ap.add_argument("--port", required=True, help="serial port of the ESP32, e.g. COM3 or /dev/ttyUSB0")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="the app's database file")
    ap.add_argument("--name", help="create the device with this name if it isn't set up yet")
    ap.add_argument("--demo", action="store_true", help="radio always on")
    ap.add_argument("--link", choices=LINKS, default="espnow", help="espnow_bridge or lora_bridge firmware")
    ap.add_argument("--sf", type=int, default=7, help="LoRa spreading factor 7-12 (all boards must match)")
    ap.add_argument("--verbose", action="store_true", help="print every frame sent and received")
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

    link = open_link(args.link, args.port, sf=args.sf)
    if args.verbose:
        trace(link)
    node = Node(link, store, config_for(link, always_on=args.demo), on_record=on_record, on_delivered=on_delivered,
                on_session_end=on_end)
    print(f"node {db.get_device()['name']} (mesh id {node.id:08x}) running, digest {store.digest().hex()}; "
          "ctrl-c to stop")
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
