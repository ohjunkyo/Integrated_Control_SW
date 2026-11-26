"""Power-failure shutdown request shared by the UPS guard, DAQ Control v2 and the HV monitor."""
import json
import os
import time
from datetime import datetime

DIR = os.path.expanduser("~/.cache/precal")
REQUEST = os.path.join(DIR, "power_fail_request.json")
STATE = os.path.join(DIR, "ups_state.json")          # latest UPS reading written by the guard
MAX_AGE_S = 3600


def _write(path, data):
    os.makedirs(DIR, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def post(reason):
    req = {"id": int(time.time()), "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "reason": reason}
    _write(REQUEST, req)
    return req


def pending():
    """The open request, or None (none, unreadable or older than an hour)."""
    req = _read(REQUEST)
    if not req or time.time() - req.get("id", 0) > MAX_AGE_S:
        return None
    return req


def ack(who, req, note="", lasers=None):
    """lasers: wavelengths this app actually switched off (None = not a laser owner)."""
    _write(f"{REQUEST}.{who}.ack", {"id": req["id"], "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note,
                                    "lasers": lasers})


def ack_info(who, req):
    """The acknowledgement for this request as a dict, or None."""
    a = _read(f"{REQUEST}.{who}.ack")
    return a if a and req and a.get("id") == req.get("id") else None


def acked(who, req):
    return ack_info(who, req) is not None


def clear():
    try:
        os.remove(REQUEST)
    except OSError:
        pass


def write_state(state):
    _write(STATE, state)


def read_state():
    return _read(STATE)

