"""How run numbers are assigned (per date), and which range a data file or image belongs to. Qt-free."""
import os
import re

NAME = re.compile(r"precal_(?:raw|prd|result)_kor_run_(\d{8})_(\d+)")
PLAN_SINCE = "20261107"
SCAN_BLOCK = 50   # General Scan runs per block (RotationManager.SCAN_BLOCK)
PLAN = [   # (first, last, use, detail)
    (0, 499, "General Scan", "10 blocks of 50 (000-450)"),
    (500, 599, "HV Scan", "1 run per HV offset"),
    (600, 699, "Dark (laser off)", "self or external trigger"),
    (700, 799, "Manual (reserved)", "no default button"),
    (800, 899, "Manual laser / Stability", "laser on"),
    (900, 999, "Test / dummy", "not for analysis"),
]
NOTE = f"Per date. Before {PLAN_SINCE[:4]}-{PLAN_SINCE[4:6]}-{PLAN_SINCE[6:]}: dark runs used 700-799."


def run_of(path):
    """(date, run number) from a data file or image name, or None."""
    m = NAME.search(path or "")
    return (m.group(1), int(m.group(2))) if m else None


SUMMARY_SCAN = re.compile(r"^(\d{8})_(\d{3,4})-(\d{3,4})_scan\.root$")
SUMMARY_ROUTINE = re.compile(r"^(\d{8})_(\d{2})(\d{2})_(.+)\.root$")


def summary_of(path):
    """routine_summary.py output name -> {"kind": "scan"|"routine", "date", ...}, or None."""
    name = os.path.basename(path or "")
    m = SUMMARY_SCAN.match(name)
    if m:
        return {"kind": "scan", "date": m.group(1), "run0": int(m.group(2)), "run1": int(m.group(3))}
    m = SUMMARY_ROUTINE.match(name)
    if m:
        return {"kind": "routine", "date": m.group(1), "time": f"{m.group(2)}:{m.group(3)}", "name": m.group(4)}
    return None


def summary_text(path):
    """'Scan 2026-11-07  runs 000-045' / 'Routine TEST-4K  2026-11-08 00:10', '' for other files."""
    s = summary_of(path)
    if not s:
        return ""
    d = f"{s['date'][:4]}-{s['date'][4:6]}-{s['date'][6:]}"
    if s["kind"] == "scan":
        return f"Scan {d}  runs {s['run0']:03d}-{s['run1']:03d}"
    return f"Routine {s['name']}  {d} {s['time']}"


def row_of(date, run):
    """Index into PLAN of the range a run belongs to (None for dates before the plan)."""
    if date < PLAN_SINCE:
        return None
    return next((i for i, (a, b, _u, _d) in enumerate(PLAN) if a <= run <= b), None)
