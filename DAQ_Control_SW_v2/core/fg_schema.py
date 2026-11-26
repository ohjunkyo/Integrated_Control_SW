"""DT1081B section functions: which inputs/outputs each uses, its parameters, results and run controls.

Built from the device's own web UI (widget_*.js / modal_*.js). The page draws its forms from this table,
and to_params() turns the form values back into the exact configure_function shape the web UI sends.
"""

# function id -> (display name, callback), in the device's selector order
FUNCTIONS = {
    "fanout": ("Fan Out", "fanout"), "wire": ("Wire", "wire"), "and": ("And", "and"), "or": ("Or", "or"),
    "or_veto": ("Or + Veto", "or_v"), "veto": ("Veto", "veto"), "majority": ("Majority", "maj"),
    "majority_veto": ("Majority + Veto", "maj_veto"), "lut": ("Lookup Table", "lut"),
    "coincidence_gate": ("Coincidence Gate", "coinc"), "scaler": ("Scaler", "scaler"), "counter": ("Counter", "count"),
    "counter_timer": ("Counter Timer", "count_timer"), "chronom": ("Chronometer", "cm"), "rate_meter": ("Ratemeter", "rm"),
    "rate_meter_advanced": ("Ratemeter Advanced", "rma"), "time_tag": ("Time Tagging", "time_tag"),
    "tof": ("Time Of Flight", "tof"), "tot": ("Time Over Threshold", "tot"), "pulse_generator": ("Pulse Generator", "p_gen"),
    "digital_generator": ("Digital Generator", "dig_gen"), "pattern_generator": ("Pattern Generator", "pat_gen"),
}

# functions that give Inputs 3 and 6 their own input standard (standard_sub)
SPECIAL_IO = {"or_veto", "veto", "majority_veto", "scaler", "counter", "counter_timer", "coincidence_gate", "rate_meter",
              "rate_meter_advanced", "chronom", "time_tag", "tof", "tot"}

# role of each physical connector per function: ({IN n: role}, {OUT n: role})
ROLES = {
    "fanout": ({1: "signal"}, {1: "IN 1", 2: "IN 1", 3: "IN 1", 4: "IN 1"}),
    "wire": ({1: "OUT 1", 2: "OUT 2", 4: "OUT 3", 5: "OUT 4"}, {1: "IN 1", 2: "IN 2", 3: "IN 4", 4: "IN 5"}),
    "and": ({i: "AND" for i in range(1, 7)}, {i: "AND" for i in range(1, 5)}),
    "or": ({i: "OR" for i in range(1, 7)}, {i: "OR" for i in range(1, 5)}),
    "or_veto": ({1: "OR", 2: "OR", 4: "OR", 5: "OR", 6: "veto"}, {i: "OR" for i in range(1, 5)}),
    "veto": ({1: "OUT 1", 2: "OUT 2", 4: "OUT 3", 5: "OUT 4", 6: "veto"}, {1: "IN 1", 2: "IN 2", 3: "IN 4", 4: "IN 5"}),
    "majority": ({i: "MAJ" for i in range(1, 7)}, {i: "MAJ" for i in range(1, 5)}),
    "majority_veto": ({1: "MAJ", 2: "MAJ", 4: "MAJ", 5: "MAJ", 6: "veto"}, {i: "MAJ" for i in range(1, 5)}),
    "lut": ({i: "LUT" for i in range(1, 7)}, {i: "LUT" for i in range(1, 5)}),
    "coincidence_gate": ({1: "coinc", 2: "coinc", 4: "coinc", 5: "coinc", 6: "gate"}, {1: "IN 1", 2: "IN 2", 3: "IN 4", 4: "IN 5"}),
    "scaler": ({1: "count", 2: "count", 3: "reset", 4: "count", 5: "count", 6: "gate"}, {1: "IN 1/N", 2: "IN 2/N", 3: "IN 4/N", 4: "IN 5/N"}),
    "counter": ({1: "count", 2: "count", 3: "reset", 4: "count", 5: "count", 6: "gate"}, {}),
    "counter_timer": ({1: "count", 3: "reset", 4: "count", 6: "gate"}, {1: "end 1", 2: "run 1", 3: "end 2", 4: "run 2"}),
    "chronom": ({1: "start", 2: "stop", 3: "reset", 4: "start", 5: "stop", 6: "gate"}, {1: "busy 1", 2: "clock 1", 3: "busy 2", 4: "clock 2"}),
    "rate_meter": ({1: "rate", 2: "rate", 3: "reset", 4: "rate", 5: "rate", 6: "gate"}, {}),
    "rate_meter_advanced": ({1: "rate", 2: "rate", 3: "reset", 4: "rate", 5: "rate", 6: "gate"}, {1: "alarm", 2: "alarm", 3: "alarm", 4: "alarm"}),
    "time_tag": ({1: "tag", 2: "tag", 4: "tag", 5: "tag", 6: "T0"}, {1: "FIFO full"}),
    "tof": ({1: "stop", 2: "stop", 3: "veto", 4: "stop", 5: "stop", 6: "T0"}, {1: "T0", 2: "last win", 3: "win end"}),
    "tot": ({1: "meas", 2: "meas", 3: "veto", 4: "meas", 5: "meas", 6: "reset"}, {i: "pulse" for i in range(1, 5)}),
    "pulse_generator": ({}, {i: "pulse" for i in range(1, 5)}),
    "digital_generator": ({}, {i: "level" for i in range(1, 5)}),
    "pattern_generator": ({}, {i: f"bit {i}" for i in range(1, 5)}),
}

# enable toggles: (label, index in the read-back list, lemo id written)
_IN1245 = [("IN 1", 0, 0), ("IN 2", 1, 1), ("IN 4", 2, 3), ("IN 5", 3, 4)]
_IN1245_COMPACT = [("IN 1", 0, 0), ("IN 2", 1, 1), ("IN 4", 2, 2), ("IN 5", 3, 3)]
_IN1_6 = [(f"IN {i + 1}", i, i) for i in range(6)]
_OUT1_4 = [(f"OUT {i + 1}", i, i) for i in range(4)]
_IN14 = [("IN 1", 0, 0), ("IN 4", 1, 1)]
_RATE_LABELS = ["IN 1", "IN 2", "IN 4", "IN 5"]

BYPASS = ["OFF", "A", "B", "C", "D"]   # bypass_section 0 = off, 1..4 = section A..D

# fields: (key, label, kind, extra); kind int -> (lo, hi, step), enum -> ([labels], [values]), bool -> None
SPECS = {
    "fanout": {"enables": _OUT1_4},
    "wire": {"enables": _IN1245_COMPACT},
    "and": {"enables": _IN1_6, "bypass": True},
    "or": {"enables": _IN1_6, "bypass": True},
    "or_veto": {"enables": _IN1245, "bypass": True},
    "veto": {"enables": _IN1245_COMPACT},
    "majority": {"enables": _IN1_6},
    "majority_veto": {"enables": _IN1245},
    "lut": {"enables": _IN1_6, "lut": True, "run": True},
    "coincidence_gate": {
        "enables": _IN1245, "coinc_flag": True,
        "fields": [("gate", "Gate (IN 6)", "bool", None), ("close_on_coincidence", "Close on coincidence", "bool", None),
                   ("delay", "Delay (ns)", "int", (0, 100000, 10)), ("width", "Width (ns)", "int", (20, 100000, 10)),
                   ("trigger", "Trigger", "enum", (["FIRST", "IN 1", "IN 2", "IN 4", "IN 5"], [0, 1, 2, 3, 4]))],
        "results": ("counters", ["Total"] + _RATE_LABELS), "reset": [("Reset", 0)]},
    "scaler": {
        "enables": _IN1245_COMPACT,
        "fields": [("gate", "Gate (IN 6)", "bool", None), ("scale", "Scale", "int", (2, 100000000, 2))],
        "results": ("counters", ["OUT 1", "OUT 2", "OUT 3", "OUT 4"]), "reset": [(f"Reset {i + 1}", i) for i in range(4)]},
    "counter": {
        "enables": _IN1245_COMPACT, "fields": [("gate", "Gate (IN 6)", "bool", None)],
        "results": ("counters", _RATE_LABELS), "reset": [(f"Reset {n}", i) for i, n in enumerate(_RATE_LABELS)]},
    "counter_timer": {
        "enables": _IN14,
        "fields": [("gate", "Gate (IN 6)", "bool", None), ("auto_reset", "Auto reset", "bool", None),
                   ("mode", "Mode", "enum", (["FREE", "COUNT DOWN", "TARGET", "WINDOW"], [0, 1, 2, 3])),
                   ("source", "Source", "enum", (["INPUT", "TIME"], [0, 1])),
                   ("time", "Time base", "enum", (["20 ns", "1 us", "1 ms", "1 s"], [0, 1, 2, 3])),
                   ("target", "Target", "int", (0, 999999999999, 1)),
                   ("gate_width", "Gate width (ns)", "int", (100, 30000000000, 1))],
        "results": ("counter_timer", ["IN 1", "IN 4"]), "reset": [("Reset IN 1", 0), ("Reset IN 4", 1)]},
    "chronom": {
        "enables": _IN14,
        "fields": [("gate", "Gate (IN 6)", "bool", None), ("mode", "Mode", "enum", (["Gate", "Start/Stop"], [0, 1])),
                   ("frequency", "Output frequency (Hz)", "int", (1, 100000000, 1)),
                   ("reset_stop", "Reset on stop", "bool", None), ("reset_gate", "Reset on gate", "bool", None)],
        "results": ("chronom", ["Channel 1", "Channel 2"]), "reset": [("Reset 1", 0), ("Reset 2", 1)]},
    "rate_meter": {
        "enables": _IN1245_COMPACT, "fields": [("gate", "Gate (IN 6)", "bool", None)],
        "results": ("rates", _RATE_LABELS)},
    "rate_meter_advanced": {
        "enables": _IN1245_COMPACT, "thresholds": True,
        "fields": [("gate", "Gate (IN 6)", "bool", None), ("alarm", "Alarm", "bool", None),
                   ("filter", "Filter", "enum", (["off", "very slow", "slow", "medium", "fast", "very fast"], [0, 1, 2, 3, 4, 5])),
                   ("int_time", "Integration time", "enum",
                    (["1 ms", "100 ms", "500 ms", "1 s", "5 s", "10 s", "30 s", "1 min", "10 min", "1 h"], list(range(10))))],
        "results": ("rates_alarm", _RATE_LABELS), "reset": [(f"Reset {n}", i) for i, n in enumerate(_RATE_LABELS)]},
    "time_tag": {"enables": [("IN 1", 0, 0), ("IN 2", 1, 1), ("IN 4", 2, 3), ("IN 5", 3, 4), ("IN 6", 4, 5)], "timetag": True},
    "tof": {
        "enables": [("IN 1", 0, 0), ("IN 2", 1, 1), ("IN 4", 3, 3), ("IN 5", 4, 4)], "fixed_on": [2, 5], "windows": 2048,
        "fields": [("t0_mode", "T0 source", "enum", (["External", "Internal"], [0, 1])),
                   ("t0_value", "T0 frequency (Hz)", "int", (0, 1000000000, 1)),
                   ("t0_reset", "T0 enabled during window", "bool", None)],
        "results": ("spectrum", _RATE_LABELS), "run": True},
    "tot": {
        "enables": [("IN 1", 0, 0), ("IN 2", 1, 1), ("IN 4", 3, 3), ("IN 5", 4, 4)], "fixed_on": [2, 5], "windows": 1024,
        "results": ("spectrum", _RATE_LABELS), "run": True},
    "pulse_generator": {
        "enables": _OUT1_4,
        "fields": [("period", "Period (ns)", "int", (30, 1000000000, 1)), ("width", "Width (ns)", "int", (20, 100000, 1)),
                   ("frequency_type", "Frequency", "enum", (["Fixed", "Poisson"], [0, 1]))]},
    "digital_generator": {"enables": [(f"OUT {i + 1} HIGH", i, i) for i in range(4)]},
    "pattern_generator": {"enables": _OUT1_4, "pattern": True, "run": True,
                          "fields": [("frequency", "Frequency (Hz)", "int", (1, 10000000, 1))]},
}

RUNNING_STATE = {"lut", "tof", "tot", "pattern_generator"}   # functions that answer get_running_state
RUN_BUTTONS = {"lut": [("Start", 1), ("Pause", 0)], "pattern_generator": [("Start", 1), ("Pause", 0)],
               "tof": [("Start", 1), ("Pause", 0), ("Reset", 2)], "tot": [("Start", 1), ("Pause", 0), ("Reset", 2)]}


def display(fn):
    return FUNCTIONS.get(fn, (fn, fn))[0]


def callback(fn):
    return FUNCTIONS.get(fn, (fn, fn))[1]


def read_enables(fn, cfg):
    """-> ([enable], [coincidence]) per toggle, in SPECS order."""
    spec = SPECS.get(fn, {})
    arr = cfg.get("lemo_in_enables" if fn == "lut" else "lemo_enables") or []
    en, co = [], []
    for _lab, idx, _lemo in spec.get("enables", []):
        e = arr[idx] if idx < len(arr) else {}
        en.append(bool(e.get("enable", False)))
        co.append(bool(e.get("coincidence", False)))
    return en, co


def _split31(v):
    v = int(v)
    return v & 0x7FFFFFFF, v >> 31


def to_params(fn, section, enables, coinc=None, values=None, extra=None):
    """Form values -> configure_function params in the shape the device web UI sends for `fn`."""
    spec = SPECS.get(fn, {})
    values = dict(values or {})
    p = {"section": section}
    if spec.get("enables") and fn != "lut":
        le = []
        for k, (_lab, _idx, lemo) in enumerate(spec["enables"]):
            d = {"lemo": lemo, "enable": bool(enables[k])}
            if spec.get("coinc_flag"):
                d["coincidence"] = bool((coinc or [False] * len(enables))[k])
            le.append(d)
        le += [{"lemo": lemo, "enable": True} for lemo in spec.get("fixed_on", [])]
        p["lemo_enables"] = sorted(le, key=lambda d: d["lemo"])
    if spec.get("bypass"):
        sec = int(values.pop("bypass_section", 0))
        p["bypass_enable"], p["bypass_section"] = sec != 0, sec
    if fn == "time_tag":
        p["bypass_enable"], p["bypass_section"] = False, 0
    if spec.get("thresholds"):
        p["thresholds"] = [{"lemo": i, "threshold": int(t)} for i, t in enumerate(values.pop("thresholds"))]
    if fn == "counter_timer":
        p["gate_width1"], p["gate_width2"] = _split31(values.pop("gate_width"))
        p["target1"], p["target2"] = _split31(values.pop("target"))
    p.update(values)
    if extra:
        p.update(extra)
    return p


def summary(fn, cfg):
    """One line for the overview card."""
    if not isinstance(cfg, dict) or "error" in cfg:
        return ""
    if fn == "pulse_generator" and "period" in cfg:
        p = cfg["period"]
        rate = 1e9 / p if p else 0
        r = f"{rate / 1e6:g} MHz" if rate >= 1e6 else f"{rate / 1e3:g} kHz" if rate >= 1e3 else f"{rate:g} Hz"
        return f"{r}, {cfg['width']} ns, {'Poisson' if cfg.get('frequency_type') else 'fixed'}"
    parts = []
    for key, label, kind, extra in SPECS.get(fn, {}).get("fields", []):
        if key in cfg and kind in ("int", "enum"):
            v = cfg[key]
            if kind == "enum" and v in extra[1]:
                v = extra[0][extra[1].index(v)]
            parts.append(f"{label.split(' (')[0]} {v}")
    return ", ".join(parts[:3])
