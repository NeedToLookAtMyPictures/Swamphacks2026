"""Transfer a large record between two boards on the SAME computer and measure it.

    python examples/bulk_test.py --port-a /dev/ttyACM0 --port-b /dev/ttyACM1 --size 51200

Both nodes run in this one process, so no second terminal is needed.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailmesh import Config, Kind, Node, Record, RecordStore  # noqa: E402
from trailmesh.espnow_serial import EspNowSerialLink  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-a", required=True)
    ap.add_argument("--port-b", required=True)
    ap.add_argument("--size", type=int, default=51200, help="image size in bytes")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()

    la = EspNowSerialLink(args.port_a, log=None)
    lb = EspNowSerialLink(args.port_b, log=None)
    time.sleep(1)  # let both radios come up
    ends = []
    a = Node(1, la, RecordStore(), Config(always_on=True), on_session_end=ends.append)
    b = Node(2, lb, RecordStore(), Config(always_on=True), on_session_end=ends.append)
    img = Record(Kind.IMAGE, 1, int(time.time()), os.urandom(args.size))
    a.post(img)
    print(f"node 1 has a {args.size} B image; waiting for node 2 to receive it...")

    t0 = time.time()
    got = None
    while time.time() - t0 < args.timeout and len(ends) < 2:
        a.poll()
        b.poll()
        if got is None and b.store.has(img.id):
            got = time.time() - t0
        time.sleep(0.001)

    intact = b.store.has(img.id) and b.store.get(img.id).body == img.body
    if got:
        print(f"delivered intact={intact} in {got:.2f} s ({args.size / got / 1024:.1f} KB/s, discovery included)")
    else:
        print(f"NOT delivered within {args.timeout:.0f} s")
    print("sessions:", ", ".join(f"node {s.peer_id}'s peer: {'ok' if s.ok else 'FAILED'}" for s in ends) or "none")

    for lk in (la, lb):
        lk.request_status()
    time.sleep(0.5)
    for name, lk in (("board A", la), ("board B", lb)):
        st = lk.status or {}
        print(f"{name}: sent {st.get('tx_ok')}, send errors {st.get('tx_err')}, received {st.get('rx')}, "
              f"dropped {st.get('rx_dropped')}")
    la.close()
    lb.close()
    return 0 if intact else 1


if __name__ == "__main__":
    sys.exit(main())
