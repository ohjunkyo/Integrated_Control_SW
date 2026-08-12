#!/usr/bin/env python3
"""One-off / rerunnable backfill: reconstruct LOG/ScanHistory/scanmap_<date>.json
for scan dates that predate the Scan Matrix point-card feature (2026-07-12),
by reading each RAW file's RunInfo tree directly (no dependency on the app's
own runtime state). Existing entries in a scanmap file are never overwritten
-- only missing (axis, tilt, wavelength) keys are added -- so this is safe to
rerun any time, including while today's scan is live-writing its own file.

Usage: python3 backfill_scanmap.py [YYYYMMDD ...]
  With no arguments, scans every date found under Data/RAW/*/precal_raw_kor_run_*.root.
"""
import os
import re
import sys
import glob
import json
from datetime import datetime

import uproot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Live RAW files, plus the external-HDD backup (older runs get moved there --
# e.g. 2026-07-08/09 no longer exist under Data/RAW at all).
RAW_ROOTS = [
    "/home/precalkor/ADC/ADC_test/Data/RAW",
    "/home/precalkor/external_HDD_1_4T/Data_Backup/RAW",
]
OUT_DIR = os.path.join(BASE_DIR, "LOG", "ScanHistory")

# Same cable -> stage-angle mapping as RotationManager._get_rot_for_cable /
# angle_convert.h's PosMapAngle, used here to classify each RAW file's
# rotation angle as the X-axis or Y-axis scan for its cable direction.
CABLE_MAP = {'E': 0, 'F': 45, 'G': 90, 'H': 135, 'A': 180, 'B': 225, 'C': 270, 'D': 315}


def _rot_for_axes(direction):
    d = CABLE_MAP.get(direction.upper(), 180)
    x_rot = (d - 180) % 360
    y_rot = (x_rot + 90) % 360
    return x_rot, y_rot


def _classify_axis(direction, rot_val):
    if rot_val is None or direction is None:
        return None
    x_rot, y_rot = _rot_for_axes(direction)
    if abs(rot_val - x_rot) < 1:
        return "X"
    if abs(rot_val - y_rot) < 1:
        return "Y"
    return None


def _read_run_info(fp):
    with uproot.open(fp) as rf:
        if "RunInfo" not in rf:
            return None
        ri = rf["RunInfo"]

        def get1(name):
            arr = ri[name].array(library="np")
            return arr[0] if len(arr) else None

        return {
            "dir2": get1("Direction2"), "dir3": get1("Direction3"),
            "rot2": get1("RawRotateAngle2"), "tilt2": get1("RawTiltAngle2"),
            "rot3": get1("RawRotateAngle3"), "tilt3": get1("RawTiltAngle3"),
            "wl": get1("Wavelength"),
        }


def backfill_date(date_tag, files):
    out_path = os.path.join(OUT_DIR, f"scanmap_{date_tag}.json")
    data = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    added = 0
    for fp in files:
        try:
            info = _read_run_info(fp)
        except Exception:
            continue
        if not info:
            continue

        axis = _classify_axis(info["dir2"], info["rot2"]) or _classify_axis(info["dir3"], info["rot3"])
        if not axis:
            continue

        try:
            tilt = int(round(float(info["tilt2"])))
            wl = f"{int(info['wl'])}nm"
        except (TypeError, ValueError):
            continue

        key = f"{axis}_{tilt}_{wl}"
        if key in data:
            continue  # never overwrite an existing (live-recorded or already backfilled) entry

        mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
        data[key] = {
            "file": fp, "axis": axis, "tilt": tilt, "wl": wl,
            "rot2": float(info["rot2"]) if info["rot2"] is not None else None,
            "rot3": float(info["rot3"]) if info["rot3"] is not None else None,
            "time": mtime, "status": "OK", "backfilled": True,
        }
        added += 1

    if added:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return added


def main():
    wanted_dates = set(sys.argv[1:])
    all_files = []
    for root in RAW_ROOTS:
        all_files.extend(glob.glob(os.path.join(root, "*", "precal_raw_kor_run_*.root")))
    all_files.sort()

    # Same (axis, tilt, wl) key can exist in both roots (e.g. a file backed up
    # after also being re-run) -- de-dupe by key later via the "key in data"
    # check, so just make sure the live Data/RAW copy is tried first per date
    # (RAW_ROOTS order above already puts it first).
    by_date = {}
    for fp in all_files:
        m = re.search(r"run_(\d{8})_\d+\.root$", os.path.basename(fp))
        if not m:
            continue
        date_tag = m.group(1)
        if wanted_dates and date_tag not in wanted_dates:
            continue
        by_date.setdefault(date_tag, []).append(fp)

    total = 0
    for date_tag in sorted(by_date):
        n = backfill_date(date_tag, by_date[date_tag])
        total += n
        print(f"{date_tag}: +{n} points ({len(by_date[date_tag])} raw files scanned)")
    print(f"Total points added: {total}")


if __name__ == "__main__":
    main()
