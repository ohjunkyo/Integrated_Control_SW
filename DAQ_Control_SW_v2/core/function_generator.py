"""CAEN DT1081B over LAN: the WebSocket JSON API its own web UI uses (ws://<host>:8080/).

Reads, every section/I-O/channel/file/clock/logic-analyser write, and the time-tag stream. Every write is
appended to LOG/FG/fg_<date>.csv. Qt-free; call from worker threads. Firmware upload and network
settings are deliberately not exposed (a failure there takes the trigger source off the network).
"""
import csv
import json
import os
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime

from core import env
from core import fg_schema as S

DEFAULT_HOST = "192.168.10.208"
DEFAULT_PORT = 8080
TIMEOUT_S = 5
LOG_DIR = os.path.join(env.ADC_DIR, "LOG", "FG")
WIDTH_MIN_NS = 20
PERIOD_MIN_NS = 30
NAME_RE = r"^[a-zA-Z0-9\-+_]{1,19}$"
FILE_KINDS = ("config", "lut", "pattern", "width")

_lock = threading.Lock()


def host():
    s = env.settings()
    return s.get("fg_host", DEFAULT_HOST), int(s.get("fg_port", DEFAULT_PORT))


def _ws(timeout=TIMEOUT_S):
    import websocket
    h, p = host()
    return websocket.create_connection(f"ws://{h}:{p}/", timeout=timeout)


class _Client:
    def __init__(self, ws):
        self.ws = ws

    def ask(self, command, params=None, callback="main", top=None, check=True):
        msg = {"command": command, "callback": callback, "SessionKey": ""}
        if params is not None:
            msg["params"] = params
        if top:
            msg.update(top)
        self.ws.send(json.dumps(msg))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("command") == command:   # skip unsolicited pushes (time-tag data)
                break
        if check and not r.get("Result"):
            raise RuntimeError(f"{command}: {r.get('Response') or 'refused by device'}")
        return r


def call(command, params=None, callback="main", top=None, check=True):
    with _lock, closing(_ws()) as ws:
        return _Client(ws).ask(command, params, callback, top, check)


def _many(requests):
    """[(command, params, callback, top)] on one connection -> [reply or Exception]."""
    out = []
    with _lock, closing(_ws()) as ws:
        c = _Client(ws)
        for cmd, params, cb, top in requests:
            try:
                out.append(c.ask(cmd, params, cb, top))
            except Exception as e:   # one unanswered item must not hide the rest
                out.append(e)
    return out


def _data(r, default=None):
    return default if isinstance(r, Exception) else r.get("data", default)


# ---- reads -------------------------------------------------------------------
def read_device():
    r = _many([("get_model", None, "main", None), ("get_version", None, "main", None), ("get_DAC_version", None, "main", None),
               ("get_clk_status", None, "main", None), ("get_alarm_status", None, "main", None),
               ("get_license_status", None, "main", None), ("get_all_sections_function", None, "main", None)])
    clk = r[3]
    return {"model": _data(r[0]), "version": _data(r[1], {}), "dac": _data(r[2], 0),
            "clock": None if isinstance(clk, Exception) else str(clk.get("data")),
            "clk_out": None if isinstance(clk, Exception) else clk.get("clk_out_enable"),
            "alarm": str(_data(r[4], "0")), "license": _data(r[5]),
            "functions": {s["section"]: s["function_name"] for s in (_data(r[6], []) or [])}}


def read_section(section, fn):
    """Function config, I/O standards, channel use, results and run state of one section."""
    spec = S.SPECS.get(fn, {})
    req = [("get_function_config", {"section": section}, S.callback(fn), None),
           ("get_input_config", {"section": section}, "main", None),
           ("get_output_config", {"section": section}, "main", None),
           ("get_ch_status", {"section": section}, "input", None)]
    want_res = bool(spec.get("results"))
    if want_res:
        req.append(("get_function_results", {"section": section}, S.callback(fn), None))
    if fn in S.RUNNING_STATE:
        req.append(("get_running_state", {"section": section}, S.callback(fn), None))
    r = _many(req)
    out = {"section": section, "function": fn, "config": _data(r[0], {}) or {},
           "input": _data(r[1], {}) or {}, "output": _data(r[2], {}) or {},
           "ch_status": [c.get("status", 0) for c in (_data(r[3], {}) or {}).get("ch_status", [])]}
    i = 4
    if want_res:
        out["results"] = _data(r[i], {}) or {}
        i += 1
    if fn in S.RUNNING_STATE:
        out["running"] = (_data(r[i], {}) or {}).get("running")
    if isinstance(r[0], Exception):
        out["config"] = {"error": str(r[0])}
    return out


def read_results(section, fn):
    return _data(call("get_function_results", {"section": section}, S.callback(fn)), {}) or {}


def read_channels(section):
    req = [("get_input_channel_config", {"section": section, "channel": c}, "in_ch", None) for c in range(6)]
    req += [("get_output_channel_config", {"section": section, "channel": c}, "out_ch", None) for c in range(4)]
    r = _many(req)
    return [_data(x, {}) or {} for x in r[:6]], [_data(x, {}) or {} for x in r[6:]]


def list_files(kind):
    d = _data(call("get_config_file", callback="settings", top={"function": kind}), "") or ""
    return [n for n in str(d).split(";") if n]


def download(name, kind):
    return _data(call("download_config", {"file_name": name, "function": kind}, "settings"))


def check_clock():
    return _data(call("check_clk", callback="settings"))


def eth_config():
    return _data(call("get_eth_config", callback="settings"), {})


def screenshot():
    """BMP bytes of the front-panel screen."""
    with _lock, closing(_ws()) as ws:   # the device does not answer screen_shot; it only writes the file
        ws.send(json.dumps({"command": "screen_shot", "callback": "settings", "SessionKey": ""}))
        time.sleep(0.4)
    h, _p = host()
    with urllib.request.urlopen(f"http://{h}/screen_shot.bmp?t={time.time():.0f}", timeout=TIMEOUT_S) as f:
        bmp = f.read()
    return bmp


def la_trigger():
    return _data(call("get_la_trigger_config"), {}) or {}


def la_capture(timeout_s=10.0):
    """Arm the logic analyser and wait for one capture -> {time, inputs[24][2048], outputs[16][2048]} or None."""
    with _lock, closing(_ws(timeout=timeout_s + 5)) as ws:
        c = _Client(ws)
        c.ask("la_arm")
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            r = c.ask("la_getdata", check=False)
            if r.get("Result"):
                return r.get("data")
            time.sleep(0.3)
    return None


# ---- writes (all logged) -----------------------------------------------------
TRIGGER_SECTION = 0   # section A: pulse generator that triggers the DAQ and the lasers


def trigger_hz():
    """DAQ trigger rate from section A's pulse generator, or None when it is not a fixed pulse generator."""
    fn = read_device()["functions"].get(TRIGGER_SECTION)
    if fn != "pulse_generator":
        return None
    cfg = read_section(TRIGGER_SECTION, fn)["config"]
    period = float(cfg.get("period") or 0)
    if period <= 0 or cfg.get("frequency_type"):
        return None
    return 1e9 / period


def logged_trigger_hz():
    """Last pulse-generator period written to section A in the change log, as Hz, or None."""
    import glob
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "fg_*.csv")), reverse=True):
        try:
            with open(path, newline="") as f:
                rows = [r for r in csv.DictReader(f)
                        if r.get("section") == "ABCD"[TRIGGER_SECTION] and r.get("action") == "configure pulse_generator"]
        except OSError:
            continue
        for r in reversed(rows):
            try:
                cfg = json.loads(r["after"])
                if not cfg.get("frequency_type") and float(cfg["period"]) > 0:
                    return 1e9 / float(cfg["period"])
            except (ValueError, KeyError, TypeError):
                continue
    return None


def _log(who, section, action, before, after):
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.now()
    path = os.path.join(LOG_DIR, f"fg_{now:%Y-%m-%d}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "who", "section", "action", "before", "after"])
        sec = "ABCD"[section] if isinstance(section, int) and 0 <= section < 4 else (section or "")
        w.writerow([now.isoformat(timespec="seconds"), who, sec, action, _short(before), _short(after)])


def _short(v):
    if v is None or v == "":
        return ""
    if isinstance(v, (dict, list)):
        v = dict(v) if isinstance(v, dict) else v
        if isinstance(v, dict):
            v.pop("section", None)
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def write(who, section, action, command, params=None, callback="main", top=None, before=None, readback=None):
    """Send one state-changing command, log it, and return the reply (readback() result if given)."""
    r = call(command, params, callback, top)
    after = params if params is not None else (top or {})
    if readback:
        try:
            after = readback()
        except Exception:
            pass
    _log(who, section, action, before, after)
    return after if readback else r


def select_function(section, fn, who):
    before = read_device()["functions"].get(section)
    write(who, section, "select function", "select_section_function", {"section": section, "function": fn},
          before=S.display(before) if before else "", readback=lambda: S.display(read_device()["functions"].get(section)))


def configure_function(section, fn, params, who):
    cb = S.callback(fn)
    before = _data(call("get_function_config", {"section": section}, cb), {})
    return write(who, section, f"configure {fn}", "configure_function", params, cb, before=before,
                 readback=lambda: _data(call("get_function_config", {"section": section}, cb), {}))


def configure_io(section, inp, out, who):
    b = _many([("get_input_config", {"section": section}, "main", None), ("get_output_config", {"section": section}, "main", None)])
    before = {"in": _data(b[0], {}), "out": _data(b[1], {})}
    call("configure_input", dict(inp, section=section), "inpout")
    call("configure_output", dict(out, section=section), "inpout")
    a = _many([("get_input_config", {"section": section}, "main", None), ("get_output_config", {"section": section}, "main", None)])
    after = {"in": _data(a[0], {}), "out": _data(a[1], {})}
    _log(who, section, "configure I/O", before, after)
    return after


def configure_channel(section, kind, channel, values, who):
    """kind 'in' (0..5: status, enable_gd, gate, delay, invert) or 'out' (0..3: status, enable_mono, mono_value, invert)."""
    get_cmd, set_cmd, cb = (("get_input_channel_config", "configure_input_channel", "in_ch") if kind == "in"
                            else ("get_output_channel_config", "configure_output_channel", "out_ch"))
    q = {"section": section, "channel": channel}
    before = _data(call(get_cmd, q, cb), {})
    return write(who, section, f"{kind.upper()} {channel + 1} settings", set_cmd, dict(values, **q), cb, before=before,
                 readback=lambda: _data(call(get_cmd, q, cb), {}))


def reset_channel(section, fn, channel, label, who):
    return write(who, section, label, "reset_channel", {"section": section, "channel": channel}, S.callback(fn))


def device_action(command, label, who, params=None, top=None, callback="settings"):
    return write(who, None, label, command, params, callback, top)


def set_la_trigger(cfg, who):
    cfg = dict(cfg, auto=0)
    before = la_trigger()
    return write(who, None, "logic analyser trigger", "configure_la_trigger", {"file_content": json.dumps(cfg)},
                 before=before, readback=la_trigger)


def upload_config(name, text, who):
    json.loads(text)   # the device refuses non-JSON
    return write(who, None, f"upload config {name}", "upload_config",
                 {"file_name": name, "file_content": text, "function": "config"}, "settings")


# ---- compatibility with the first tab version ----------------------------------
def read_all():
    d = read_device()
    secs = []
    for sec in range(4):
        fn = d["functions"].get(sec, "")
        try:
            cfg = _data(call("get_function_config", {"section": sec}, S.callback(fn)), {})
        except Exception as e:
            cfg = {"error": str(e)}
        secs.append({"section": sec, "function_name": fn, "config": cfg})
    return {"model": d["model"], "sections": secs, "clock": {"data": d["clock"], "clk_out_enable": d["clk_out"]}}


def pulse_generator():
    fn = read_device()["functions"].get(0)
    return _data(call("get_function_config", {"section": 0}, "p_gen"), {}) if fn == "pulse_generator" else None


def validate(period_ns, width_ns):
    if width_ns < WIDTH_MIN_NS:
        return f"Width must be >= {WIDTH_MIN_NS} ns."
    if period_ns < width_ns + 10:
        return "Period must be at least width + 10 ns."
    if period_ns > 1_000_000_000:
        return "Period must be <= 1e9 ns."
    return None


def set_pulse_generator(period_ns, width_ns, enables, who="operator", frequency_type=None, section=0):
    why = validate(period_ns, width_ns)
    if why:
        raise ValueError(why)
    cur = _data(call("get_function_config", {"section": section}, "p_gen"), {})
    ft = cur.get("frequency_type", 0) if frequency_type is None else int(frequency_type)
    p = S.to_params("pulse_generator", section, enables, values={"frequency_type": ft, "width": int(width_ns), "period": int(period_ns)})
    return configure_function(section, "pulse_generator", p, who)


# ---- time-tag stream -----------------------------------------------------------
class TimeTagStream:
    """Keeps its own socket open while running; events arrive as unsolicited send_data messages."""

    def __init__(self, section, on_events, on_error):
        self.section, self.on_events, self.on_error = section, on_events, on_error
        self._stop = threading.Event()
        self._t = None

    def start(self, who):
        self._stop.clear()
        self._t = threading.Thread(target=self._run, args=(who,), daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()

    def _run(self, who):
        try:
            with closing(_ws(timeout=1.0)) as ws:
                c = _Client(ws)
                c.ask("start_tt_data", {"section": self.section}, "timetag")
                time.sleep(0.2)
                c.ask("reset_channel", {"section": self.section, "channel": 1}, "time_tag")
                _log(who, self.section, "time tag start", "", "")
                while not self._stop.is_set():
                    try:
                        m = json.loads(ws.recv())
                    except Exception:
                        continue   # recv timeout: check the stop flag
                    if m.get("command") == "send_data" and m.get("timetag_data") is not None:
                        self.on_events(m["timetag_data"])
                c.ask("stop_tt_data", {"section": self.section}, "timetag", check=False)
                time.sleep(0.1)
                c.ask("reset_channel", {"section": self.section, "channel": 0}, "time_tag", check=False)
                _log(who, self.section, "time tag stop", "", "")
        except Exception as e:
            self.on_error(str(e))


# ---- change log ----------------------------------------------------------------
def history(limit=200):
    """Last `limit` rows across all fg_*.csv files as [time, who, section, action, before, after], newest last."""
    rows = []
    if not os.path.isdir(LOG_DIR):
        return rows
    for name in sorted(os.listdir(LOG_DIR)):
        if name.startswith("fg_") and name.endswith(".csv"):
            with open(os.path.join(LOG_DIR, name), newline="") as f:
                for row in list(csv.reader(f))[1:]:
                    if len(row) == 4:   # first-version rows: time, who, before, after (Section A)
                        row = [row[0], row[1], "A", "pulse generator", row[2], row[3]]
                    rows.append((row + [""] * 6)[:6])
    return rows[-limit:]
