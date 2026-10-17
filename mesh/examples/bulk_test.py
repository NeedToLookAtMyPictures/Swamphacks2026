"""Transfer a large message between two boards on the SAME computer and measure it.

    python examples/bulk_test.py --port-a /dev/ttyACM0 --port-b /dev/ttyACM1 --size 51200

Simulate hiker B walking out of range mid-transfer, then coming back:

    python examples/bulk_test.py --port-a ... --port-b ... --size 300000 --cut-after 3 --cut-for 8

"Out of range" is done by switching board B's radio off; the USB link stays up,
exactly like a real hiker whose radio can no longer hear the other one.
(Unplugging a board is a different failure: the host loses its radio. The link
survives that too and reconnects, but it doesn't model walking away.)

Both nodes run in this one process, on throwaway app databases (never data/user.db).
"""
import argparse
import os
import random
import string
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "application", "user_app"))

from database import BROADCAST, Database  # noqa: E402  (the app's database)

from trailmesh import AppStore  # noqa: E402
from trailmesh.radio import BOARDS, RADIOS, build_node, open_board  # noqa: E402


def app_db(directory, name, device_id):
    """A throwaway app database, so the test never touches the real data/user.db."""
    db = Database(os.path.join(directory, f"{name}.db"))
    db.init_device(name, device_id)
    return db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-a", required=True)
    ap.add_argument("--port-b", required=True)
    ap.add_argument("--board", choices=BOARDS, default="espnow",
                    help="espnow: firmware/espnow_bridge; heltec: firmware/heltec_bridge (LoRa + ESP-NOW)")
    ap.add_argument("--radio", choices=RADIOS, default="auto", help="heltec only: auto, lora or espnow")
    ap.add_argument("--sf", type=int, default=7, help="LoRa spreading factor 7-12 (all boards must match)")
    ap.add_argument("--size", type=int, default=51200, help="message length in characters")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--cut-after", type=float, default=None, help="seconds into the run to take B out of range")
    ap.add_argument("--cut-for", type=float, default=8.0, help="seconds B stays out of range")
    args = ap.parse_args()

    log = lambda *m: print(f"{time.time() - t0:6.2f}s", *m)  # noqa: E731
    t0 = time.time()
    la = open_board(args.board, args.port_a, sf=args.sf, log=log)
    lb = open_board(args.board, args.port_b, sf=args.sf, log=log)
    time.sleep(1)  # let both boards come up

    def on_end(me):
        return lambda s: log(f"{me}'s session with node {s.peer_id:08x}: {'ok' if s.ok else 'FAILED'}, "
                             f"received {s.records_received} record(s)")

    tmp = tempfile.mkdtemp(prefix="bulk_test-")
    alice, bob = app_db(tmp, "alice", "00000001"), app_db(tmp, "bob", "00000002")
    a = build_node(la, AppStore(alice), radio=args.radio, config=dict(always_on=True), on_session_end=on_end("alice"))
    b = build_node(lb, AppStore(bob), radio=args.radio, config=dict(always_on=True, retry_backoff=2.0),
                   on_session_end=on_end("bob"))
    urgent = alice.add_locale("spring-1203", "Spring at mile 1203", "dry")
    big = alice.add_message(BROADCAST, "".join(random.choices(string.ascii_letters, k=args.size)))
    t0 = time.time()
    log(f"alice has an urgent spring report and a {args.size}-character message for everyone")

    def bob_has(table, rec):
        return bob.get(table, rec["origin_id"], rec["seq"]) is not None

    got = {}
    cut_state = "pending" if args.cut_after is not None else "none"
    while time.time() - t0 < args.timeout:
        now = time.time() - t0
        if cut_state == "pending" and now >= args.cut_after:
            log(">>> B walks out of range (radio off)")
            b.link.set_radio(False)
            cut_state = "cut"
        elif cut_state == "cut" and now >= args.cut_after + args.cut_for:
            log(">>> B is back in range (radio on)")
            b.link.set_radio(True)
            cut_state = "restored"
        a.poll()
        b.poll()
        for name, table, rec in (("urgent spring report", "locales", urgent), ("big message", "messages", big)):
            if name not in got and bob_has(table, rec):
                got[name] = now
                log(f"bob has the {name}")
        if len(got) == 2 and a.session is None and b.session is None:
            break
        time.sleep(0.001)

    row = bob.get("messages", big["origin_id"], big["seq"])
    intact = row is not None and row["content"] == big["content"]
    print()
    if "big message" in got:
        t = got["big message"]
        print(f"big message delivered intact={intact} at {t:.2f} s ({args.size / t / 1024:.1f} KB/s overall)")
    else:
        print(f"big message NOT delivered within {args.timeout:.0f} s")
    if args.cut_after is not None and "urgent spring report" in got:
        before = got["urgent spring report"] < args.cut_after
        print(f"urgent report arrived {'BEFORE' if before else 'after'} the cut, at {got['urgent spring report']:.2f} s")

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
