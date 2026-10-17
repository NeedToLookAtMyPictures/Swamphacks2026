"""Run one trailmesh node against a real ESP32 running firmware/espnow_bridge, or a Heltec
LoRa 32 V3 running firmware/heltec_bridge (--board heltec).

On a Heltec board, --radio picks the radios:
    --radio auto     LoRa; the exchange moves to ESP-NOW when the peer is within 60 m (default)
    --radio lora     LoRa only, at any distance (test LoRa with the boards side by side)
    --radio espnow   ESP-NOW only

The node syncs the user app's database (application/user_app/data/user.db by
default), so run it next to the app. Set the device up once in the app first
(POST /api/device), or pass --name to create it here.

    pip install -r requirements.txt
    python examples/run_node.py --name alice --demo
    python examples/run_node.py --port /dev/ttyACM1 --db other.db --name bob --demo

--port defaults to "auto": the one ESP32 plugged in. With several boards, name
one: COM3 on Windows, /dev/serial/by-id/... on Linux (ttyACM numbers can swap).

Android (Termux): run the same command. It reruns itself through termux-usb to
get access to the board (accept the dialog). If the board is unplugged, the node
stops and has to be started again; see trailmesh/termux_usb.py.

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

from trailmesh import AppStore, Record  # noqa: E402
from trailmesh.espnow_serial import LinkLost  # noqa: E402
from trailmesh.radio import BOARDS, RADIOS, build_node, open_board  # noqa: E402
from trailmesh.transport import TransportError  # noqa: E402
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
    ap.add_argument("--port", default="auto",
                    help="the ESP32: auto (default), COM3, /dev/ttyACM0, or on Android termux-usb or /dev/bus/usb/...")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="the app's database file")
    ap.add_argument("--name", help="create the device with this name if it isn't set up yet")
    ap.add_argument("--demo", action="store_true", help="radio always on")
    ap.add_argument("--board", choices=BOARDS, default="espnow",
                    help="espnow: firmware/espnow_bridge; heltec: firmware/heltec_bridge (LoRa + ESP-NOW)")
    ap.add_argument("--radio", choices=RADIOS, default="auto",
                    help="heltec only. auto: LoRa, switching to ESP-NOW within 60 m; lora / espnow: that radio only")
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
        print(f"session with {s.peer_id} over {'ESP-NOW' if s.fast or node.link is board else 'LoRa'} "
              f"{'ok' if s.ok else 'FAILED'}: got {s.records_received}, sent {s.records_sent}")

    try:
        board = open_board(args.board, args.port, sf=args.sf)
    except TransportError as e:
        sys.exit(f"can't open the board: {e}")
    node = build_node(board, store, radio=args.radio, config=dict(always_on=args.demo), on_record=on_record,
                      on_delivered=on_delivered, on_session_end=on_end)
    if args.verbose:
        trace(node.link)
        if node.fast_link is not None:
            trace(node.fast_link)
    print(f"node {db.get_device()['name']} (mesh id {node.id:08x}) running, radio mode {args.radio}, "
          f"digest {store.digest().hex()}; ctrl-c to stop")
    node.poll()  # switches the discovery radio on
    time.sleep(1)
    board.request_status()  # check the radio actually came up
    time.sleep(0.5)
    print_board_status(board)
    try:
        node.run_forever()
    except KeyboardInterrupt:
        pass
    except LinkLost as e:
        sys.exit(str(e))
    finally:
        board.close()


def print_board_status(board):
    st, lora = board.status, getattr(board, "lora_status", None)
    if board.mac is None:
        print("board: NO REPLY FROM FIRMWARE (wrong port or not flashed?)")
    elif st is None:
        print(f"board {board.mac.hex(':')}: old firmware without status; re-flash it")
    elif lora is not None:
        print(f"board {board.mac.hex(':')}: LoRa {'on' if lora['radio_on'] else 'off'} "
              f"({lora['freq_hz'] / 1e6:.3f} MHz, {lora['bw_hz'] // 1000} kHz, SF{lora['sf']}, "
              f"last error {lora['last_err']}), ESP-NOW {'on' if st['radio_on'] else 'off'}")
    elif not st["radio_on"]:
        print(f"board {board.mac.hex(':')}: RADIO FAILED TO START (error {st['last_err']:#x}); see [esp32] log above")
    else:
        print(f"board {board.mac.hex(':')}: radio on, channel {st['channel']}")


if __name__ == "__main__":
    main()
