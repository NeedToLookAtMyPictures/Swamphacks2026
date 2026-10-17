"""Hardware check: is the bridge firmware alive, and can two boards hear each other?

Run this on BOTH computers at the same time:

    python examples/diagnose.py              (the one ESP32 plugged in)
    python examples/diagnose.py --port /dev/ttyACM0

Step 1 checks the USB link to the board (GET_INFO -> INFO reply).
Step 2 broadcasts a raw test frame every second and prints everything the radio
receives, bypassing the trailmesh protocol entirely. If each side prints the
other's pings, the radio path works and any remaining problem is in the protocol.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh.espnow_serial import CMD_GET_INFO, EspNowSerialLink, LinkLost  # noqa: E402
from trailmesh.transport import TransportError  # noqa: E402
from trailmesh.wire import Frame  # noqa: E402

PING_MAGIC = b"\x00\x00\x00\x00DIAG"  # sender id 0 never decodes as a real trailmesh device


def fmt_mac(mac):
    return ":".join(f"{b:02x}" for b in mac) if mac else "?"


def refresh_status(link, timeout=2.0):
    link.status = None
    link.request_status()
    deadline = time.time() + timeout
    while link.status is None and time.time() < deadline:
        time.sleep(0.05)
    return link.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="auto", help="auto, COM3, /dev/ttyACM0, termux-usb, ...")
    ap.add_argument("--seconds", type=float, default=30)
    args = ap.parse_args()

    logs = []
    try:
        link = EspNowSerialLink(args.port, log=lambda *a: logs.append(" ".join(map(str, a))))
    except TransportError as e:
        print(f"FAIL: can't open the board: {e}")
        return 1
    try:
        print(f"[1/2] Asking the board on {args.port} for its MAC...")
        deadline = time.time() + 5
        while link.mac is None and time.time() < deadline:
            time.sleep(0.5)
            if link.mac is None:
                link._write(bytes([CMD_GET_INFO]))  # GET_INFO again, in case the board was still booting
        for line in logs:
            print("      firmware log:", line)
        printed = len(logs)
        if link.mac is None:
            print("      FAIL: no reply. The bridge firmware is not running, or this is the wrong port.")
            print("      - Press the board's Reset button and run this again.")
            print("      - Re-flash firmware/espnow_bridge (Feather S3: board 'Adafruit Feather ESP32-S3',")
            print("        Tools -> USB CDC On Boot: Enabled).")
            return 1
        print(f"      OK: firmware is responding, board MAC {fmt_mac(link.mac)}")
        st = refresh_status(link)
        if st is None:
            print("      WARNING: board sent no status block; it is running an older bridge firmware. Re-flash it.")
        elif not st["radio_on"]:
            print(f"      FAIL: radio did not start (last error {st['last_err']:#x}); see the firmware log lines above.")
            return 1
        else:
            print(f"      radio on, channel {st['channel']}")

        print(f"[2/2] Broadcasting a test ping every second for {args.seconds:.0f} s; listening for the other board...")
        heard = set()
        n = 0
        next_ping = 0.0
        end = time.time() + args.seconds
        while time.time() < end:
            now = time.time()
            if now >= next_ping:
                link.send(PING_MAGIC + f" {fmt_mac(link.mac)} #{n}".encode())
                n += 1
                next_ping = now + 1
            for rx in link.poll():
                src = fmt_mac(rx.src)
                heard.add(src)
                if rx.payload.startswith(PING_MAGIC):
                    what = "diag ping" + rx.payload[len(PING_MAGIC):].decode(errors="replace")
                else:
                    f = Frame.decode(rx.payload)
                    what = f"trailmesh {f.type.name} from device {f.sender}" if f else f"unknown {len(rx.payload)} B"
                print(f"      <- {src} rssi {rx.rssi} dBm: {what}")
            for line in logs[printed:]:
                print("      firmware log:", line)
            printed = len(logs)
            time.sleep(0.01)

        st = refresh_status(link)
        print()
        if st:
            print(f"Board counters: sent ok {st['tx_ok']}, send errors {st['tx_err']}, received {st['rx']}, "
                  f"filtered {st['rx_filtered']}, dropped {st['rx_dropped']}, last error {st['last_err']:#x}")
            if st["tx_err"]:
                print("  Send errors: see the 'send failed' firmware log lines for the ESP-IDF error name.")
        if heard:
            print(f"OK: heard {len(heard)} other device(s): {', '.join(sorted(heard))}. The radio path works.")
        else:
            print(f"FAIL: sent {n} pings, heard nothing. If the other side also heard nothing, check:")
            print("  - both boards run this same firmware build (same CHANNEL constant)")
            print("  - the antenna: some boards need an external antenna attached")
            print("  - distance: start with the boards about 1 m apart")
            print("If the OTHER side heard us but we heard nothing, our board's receive path is the problem.")
        return 0 if heard else 2
    finally:
        link.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LinkLost as e:
        sys.exit(f"FAIL: {e}")
