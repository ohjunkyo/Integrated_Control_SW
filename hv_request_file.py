"""HV set-voltage request shared by DAQ Control v2 (writer) and the HV Monitor (executor):
one JSON file under ~/.cache/precal, one request at a time, the outcome written back in place.

States: posted (v2) -> accepted | refused (HV Monitor) -> done | error (crate reply); v2 may write
cancelled while the request is still posted."""
import json
import os
import time
from datetime import datetime

DIR = os.path.expanduser("~/.cache/precal")
PATH = os.path.join(DIR, "hv_request.json")
MAX_AGE_S = 120                       # a request not picked up within this is refused as stale
OPEN = ("posted", "accepted")
FINAL = ("refused", "done", "error", "cancelled")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = f"{PATH}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)


def read():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def age(req):
    return time.time() - req.get("posted_at", 0)


def pending():
    """The request still waiting on the HV Monitor (posted or accepted), or None."""
    req = read()
    return req if req and req.get("state") in OPEN else None


def post(ch, vset, who, reason):
    req = {"id": f"{time.strftime('%Y%m%d-%H%M%S')}{int(time.time() * 1000) % 1000:03d}-{os.getpid()}",
           "ts": _now(), "posted_at": time.time(),
           "who": who, "reason": reason, "ch": int(ch), "vset": float(vset),
           "state": "posted", "note": "", "vmon": None, "updated": _now()}
    _write(req)
    return req


def update(req, state, note="", vmon=None, expect=None):
    """Write an outcome into the request file; None (nothing written) when the file holds another
    request or, with expect, a state outside it."""
    cur = read()
    if not cur or cur.get("id") != req.get("id") or (expect and cur.get("state") not in expect):
        return None
    cur.update(state=state, note=note, updated=_now())
    if vmon is not None:
        cur["vmon"] = vmon
    _write(cur)
    return cur


def status(req_id):
    cur = read()
    if not cur or cur.get("id") != req_id:
        return {"id": req_id, "state": "unknown", "note": "request file replaced or missing"}
    return cur
