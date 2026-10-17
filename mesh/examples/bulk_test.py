"""Transfer a large message between two boards on the SAME computer and measure it.

    python examples/bulk_test.py --port-a /dev/ttyACM0 --port-b /dev/ttyACM1 --size 51200

Simulate hiker B walking out of range mid-transfer, then coming back:

    python examples/bulk_test.py --port-a ... --port-b ... --size 300000 --cut-after 3 --cut-for 8

"Out of range" is done by switching board B's radio off; the USB link stays up,
exactly like a real hiker whose radio can no longer hear the other one.
(Unplugging a board is a different failure: the host loses its radio. The link
survives that too and reconnects, but it doesn't model walking away.)

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
    ap.add_argument("--size", type=int, default=51200, help="message size in bytes")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--cut-after", type=float, default=None, help="seconds into the run to take B out of range")
    ap.add_argument("--cut-for", type=float, default=8.0, help="seconds B stays out of range")
    args = ap.parse_args()

    log = lambda *m: print(f"{time.time() - t0:6.2f}s", *m)  # noqa: E731
    t0 = time.time()
    la = EspNowSerialLink(args.port_a, log=log)
    lb = EspNowSerialLink(args.port_b, log=log)
    time.sleep(1)  # let both radios come up

    def on_end(me):
        return lambda s: log(f"node {me}'s session with node {s.peer_id}: {'ok' if s.ok else 'FAILED'}, "
                             f"received {s.records_received} record(s)")

    a = Node(1, la, RecordStore(), Config(always_on=True), on_session_end=on_end(1))
    b = Node(2, lb, RecordStore(), Config(always_on=True, retry_backoff=2.0), on_session_end=on_end(2))
    urgent = Record.locale_update(1, int(time.time()), "spring-1203", "water", "dry")
    big = Record(Kind.MESSAGE, 1, int(time.time()), os.urandom(args.size))
    a.post(urgent)
    a.post(big)
    t0 = time.time()
    log(f"node 1 has an urgent water report and a {args.size} B message")

    got = {}
    cut_state = "pending" if args.cut_after is not None else "none"
    while time.time() - t0 < args.timeout:
        now = time.time() - t0
        if cut_state == "pending" and now >= args.cut_after:
            log(">>> B walks out of range (radio off)")
            lb.set_radio(False)
            cut_state = "cut"
        elif cut_state == "cut" and now >= args.cut_after + args.cut_for:
            log(">>> B is back in range (radio on)")
            lb.set_radio(True)
            cut_state = "restored"
        a.poll()
        b.poll()
        for name, rec in (("urgent water report", urgent), ("big message", big)):
            if name not in got and b.store.has(rec.id):
                got[name] = now
                log(f"node 2 has the {name}")
        if len(got) == 2 and a.session is None and b.session is None:
            break
        time.sleep(0.001)

    intact = b.store.has(big.id) and b.store.get(big.id).body == big.body
    print()
    if "big message" in got:
        t = got["big message"]
        print(f"big message delivered intact={intact} at {t:.2f} s ({args.size / t / 1024:.1f} KB/s overall)")
    else:
        print(f"big message NOT delivered within {args.timeout:.0f} s")
    if args.cut_after is not None and "urgent water report" in got:
        before = got["urgent water report"] < args.cut_after
        print(f"urgent report arrived {'BEFORE' if before else 'after'} the cut, at {got['urgent water report']:.2f} s")

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
