"""Ask the HV Monitor to set a CAEN N1470 channel voltage (request file, see hv_request_file.py)
and wait until VMon has settled in the HV monitoring DB. Python API only, no widgets."""
import sqlite3
import time
from datetime import datetime

from core import applog, env, next_daq  # env puts the repo root on sys.path
import hv_request_file as rf  # noqa: E402
import instance_lock  # noqa: E402

ACK_TIMEOUT_S = 15        # the HV Monitor polls the file every 2 s
DONE_TIMEOUT_S = 90       # crate reply after accept
SETTLE_S = 30             # VMon within TOL_V for this long
TOL_V = 2.0
DB_FRESH_S = 30           # a DB row older than this does not count
GAIN_DELAY_S = 180        # PMT gain settle after the voltage has settled
POLL_S = 2.0


class HVRequestError(RuntimeError):
    pass


def post(ch, vset, who="v2", reason=""):
    """-> request id. Raises HVRequestError when the HV Monitor is not running or a request is open."""
    if not instance_lock.is_running("hv"):
        raise HVRequestError("HV Monitor is not running")
    cur = rf.pending()
    if cur and rf.age(cur) < rf.MAX_AGE_S + DONE_TIMEOUT_S:
        raise HVRequestError(f"another HV request is still open ({cur['id']}, {cur['state']})")
    req = rf.post(ch, vset, who, reason)
    applog.log(f"[INFO] HV request {req['id']} posted: Ch{req['ch']} VSet {req['vset']:.1f} V"
               f"{' (' + reason + ')' if reason else ''}")
    return req["id"]


def status(req_id):
    """The request file as a dict (state posted/accepted/refused/done/error/cancelled/unknown)."""
    return rf.status(req_id)


def _result(st, ok, note=None):
    out = dict(st)
    out["ok"] = ok
    if note is not None:
        out["note"] = note
    return out


def _wait_state(req_id, until, timeout, cancel):
    end = time.time() + timeout
    while True:
        st = rf.status(req_id)
        if st["state"] in until or st["state"] == "unknown":
            return st
        if cancel and cancel():
            return dict(st, state="cancelled", note="cancelled by the caller")
        if time.time() >= end:
            return dict(st, state="timeout")
        time.sleep(POLL_S)


def _latest_vmon(ch, db_path):
    """(row time, VMon) of the newest HV DB row, or (None, None)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        r = con.execute(f"SELECT timestamp, Ch{ch}_V FROM monitoring_data ORDER BY timestamp DESC LIMIT 1").fetchone()
        con.close()
        if r and r[0] is not None and r[1] is not None:
            return datetime.fromisoformat(r[0]).timestamp(), float(r[1])
    except Exception:
        pass
    return None, None


def wait_settled(req_id, timeout=900, settle_s=SETTLE_S, tol_v=TOL_V, gain_delay_s=GAIN_DELAY_S,
                 skip_gain_delay=False, cancel=None, progress=None, db_path=None):
    """Block until the HV Monitor has executed the request, VMon has stayed within tol_v of the
    target for settle_s (HV monitoring DB), then the gain-settle delay. -> dict(ok, state, note, vmon, ...).
    cancel() -> True aborts the wait; progress(phase, text) reports each step."""
    db_path = db_path or env.HV_DB
    say = progress or (lambda phase, text: None)
    say("ack", "waiting for the HV Monitor")
    st = _wait_state(req_id, ("accepted",) + rf.FINAL, ACK_TIMEOUT_S, cancel)
    if st["state"] in ("timeout", "cancelled"):
        note = st.get("note") or f"HV Monitor did not pick up the request within {ACK_TIMEOUT_S} s"
        if rf.update(st, "cancelled", note, expect=("posted",)) is not None:
            applog.log(f"[WARN] HV request {req_id} cancelled: {note}")
            return _result(st, False, note)
        st = rf.status(req_id)   # taken meanwhile: carry on
    if st["state"] == "accepted":
        say("done", "VSet queued at the crate")
        st = _wait_state(req_id, rf.FINAL, DONE_TIMEOUT_S, cancel)
    if st["state"] != "done":
        note = st.get("note") or st["state"]
        applog.log(f"[WARN] HV request {req_id} failed: {st['state']} -- {note}")
        return _result(st, False, note)
    ch, vset = st["ch"], float(st["vset"])
    say("settle", f"waiting for Ch{ch} VMon within {tol_v:g} V of {vset:.1f} V for {settle_s:.0f} s")
    end = time.time() + timeout
    since = None
    vmon = st.get("vmon")
    while True:
        if cancel and cancel():
            return _result(st, False, "cancelled by the caller")
        if time.time() >= end:
            applog.log(f"[WARN] HV request {req_id}: Ch{ch} VMon did not settle within {timeout:.0f} s")
            return _result(st, False, f"VMon did not settle within {timeout:.0f} s (last {vmon})")
        t, v = _latest_vmon(ch, db_path)
        if t is not None and time.time() - t <= DB_FRESH_S:
            vmon = v
            if abs(v - vset) <= tol_v:
                since = since or t
                next_daq.wait_until(since + settle_s + (0 if skip_gain_delay else max(gain_delay_s, 0)))
                if t - since >= settle_s:
                    break
            else:
                since = None
                next_daq.clear()
        time.sleep(POLL_S)
    applog.log(f"[INFO] HV request {req_id} settled: Ch{ch} VMon {vmon:.1f} V")
    st = _result(st, True, f"settled at {vmon:.1f} V")
    st["vmon"] = vmon
    if skip_gain_delay or gain_delay_s <= 0:
        return st
    say("gain", f"gain settle delay {gain_delay_s:.0f} s")
    end = time.time() + gain_delay_s
    while time.time() < end:
        if cancel and cancel():
            return _result(st, False, "cancelled during the gain settle delay")
        time.sleep(min(POLL_S, max(end - time.time(), 0)))
    return st
