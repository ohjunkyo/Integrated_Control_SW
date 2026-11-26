#!/usr/bin/env python3
"""Offline checks of the guard's laser step (UPS_GUARD_DRY=1, fake locks and acks; no hardware)."""
import os
import sys
import tempfile
import threading

os.environ["UPS_GUARD_DRY"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ups_guard as g  # noqa: E402
import power_fail  # noqa: E402

tmp = tempfile.mkdtemp()
power_fail.DIR = tmp
power_fail.REQUEST = os.path.join(tmp, "power_fail_request.json")
g.LOG = os.path.join(tmp, "guard.log")
g.OUTBOX = os.path.join(tmp, "outbox")
g.APP_ACK_S = 1
g.time.sleep = lambda s: None


def scenario(name, held, acks, expect_app, expect_stop):
    """held: lock names held; acks: {who: lasers list} written before the wait."""
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    g.running = lambda lock: lock in held
    g.instance_lock.holder = lambda lock: {"pid": 999999} if lock in held else None
    req = power_fail.post("test")
    for who, lasers in acks.items():
        power_fail.ack(who, req, "note", lasers=lasers)
    out = g.stop_lasers(req)
    via_app = out.startswith("lasers off (") and "direct" not in out
    stopped = out.split("stopped ")[1].split(";")[0].split(", ") if "stopped " in out else []
    ok = via_app == expect_app and sorted(stopped) == sorted(expect_stop)
    print(f"{'OK ' if ok else 'BAD'} {name:44s} -> {out}")
    return ok


cases = [
    ("no app open", set(), {}, False, []),
    ("v2 only, v2 switched lasers off", {"daq_v2"}, {"v2": ["405nm"]}, True, []),
    ("v2 only, v2 reports none", {"daq_v2"}, {"v2": []}, False, ["daq_v2"]),
    ("v2 only, v2 silent", {"daq_v2"}, {}, False, ["daq_v2"]),
    ("Tk only (holds lasers), Tk acks", {"daq"}, {"tk": ["375nm", "405nm"]}, True, []),
    ("Tk only, old Tk without handler (silent)", {"daq"}, {}, False, ["daq"]),
    ("Tk+v2, Tk holds lasers, both ack", {"daq", "daq_v2"}, {"v2": [], "tk": ["405nm"]}, True, []),
    ("Tk+v2, v2 acks none, Tk silent", {"daq", "daq_v2"}, {"v2": []}, False, ["daq", "daq_v2"]),
    ("old laser_gui holds a board, no DAQ app", {"laser_gui"}, {}, False, ["laser_gui"]),
]
results = [scenario(*c) for c in cases]

req = power_fail.post("roundtrip")
power_fail.ack("v2", req, "n", lasers=["473nm"])
assert power_fail.acked("v2", req) and power_fail.ack_info("v2", req)["lasers"] == ["473nm"]
assert not power_fail.acked("tk", req)
print("power_fail ack/ack_info roundtrip: OK")
sys.exit(0 if all(results) else 1)
