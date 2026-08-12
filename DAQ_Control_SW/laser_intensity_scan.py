#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wavelength-by-wavelength Pulse Current sweep to find the optimal drive current
at the boresight (center) angle. For each wavelength, steps Pulse Current
through a range, takes a real 50000-event acquisition at each step (LD fired
for real -- this does NOT reuse old data), runs the existing offline analysis
chain, and reports which current level satisfies:

    QE (PHC, "relativeQE" branch)      in [QE_MIN, QE_MAX] %
    Charge  (SPE mean, "spe_mean")     in [CHARGE_MIN, CHARGE_MAX] pC
    2 p.e. contribution                <= TWO_PE_MAX  (fraction of triggered events)
    Peak-to-Valley                     as large as possible (informational, no hard cutoff given)
    ALL THREE PMTs simultaneously within QE/Charge windows

Requires the main Integrated_Control_SW GUI (main.py) to be CLOSED first --
this script opens the laser USB device and drives the DAQ directly, and only
one process can hold either at a time.

Stage must already be at the boresight/center position (tilt=0, rot=0 on both
mounts) before running -- this script does not move the rotation stage.

Usage:
    python3 laser_intensity_scan.py                      # full scan, all 3 wavelengths
    python3 laser_intensity_scan.py --wavelengths 450     # just one
    python3 laser_intensity_scan.py --dry-run             # print the plan, fire nothing
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, "/home/precalkor/Integrated_Control_SW/Laser_Control_SW/app")
from laser_driver import TamadenshiLaser  # noqa: E402

import hid  # noqa: E402


# =============================================================================
# Configuration -- fill in / adjust before running
# =============================================================================

# Which wavelengths to scan and, for each, the fixed Bias current (mA) to hold
# while Pulse Current is swept. Bias is NOT part of the sweep -- only Pulse is.
# Set these to whatever bias each board is normally run at; the combined
# bias+pulse must stay under the 200 mA hardware limit (enforced below too).
WAVELENGTHS_MA_BIAS = {
    "375nm": 0.0,
    "450nm": 0.0,
    "473nm": 0.0,
}

PULSE_CURRENTS_MA = [150, 155, 160, 165, 170, 175, 180]

# USB path per wavelength (see list_devices() below to find these -- these
# boards report no serial number, so the physical USB path is the only stable
# identifier; re-check after any re-cabling). Fill in before running.
WAVELENGTH_USB_PATH = {
    # Read from the live app's saved mapping (~/.daq_control_config.json,
    # key "laser_port_mapping"), which matched main.py's own hardcoded
    # default at the time this was filled in. Re-run --list-devices and
    # cross-check against that file if the boards get re-cabled.
    "375nm": b"1-3.4.4:1.0",
    "405nm": b"1-3.4.1:1.0",
    "450nm": b"1-3.4.2:1.0",
    "473nm": b"1-3.4.3:1.0",
}

EVENTS_PER_RUN = 50000  # matches config3.h's Events, kept in sync below

# Bias and pulse share one physical drive path on this board -- the manual's
# 200 mA ceiling applies to their SUM. The main app's laser_driver.py doesn't
# carry this as a class constant (that check lives in laser_manager.py's
# apply_laser_currents_multi() instead, GUI-coupled), so it's duplicated here.
LD_TOTAL_CURRENT_LIMIT_MA = 200.0

# Acceptance windows
QE_MIN, QE_MAX = 1.0, 2.0            # % , relativeQE branch ("QE (PHC)")
CHARGE_MIN, CHARGE_MAX = 1.6, 1.9    # pC, spe_mean branch ("Charge")
TWO_PE_MAX = 0.02                     # fraction of triggered (>=1 p.e.) events that are 2+ p.e.

CONFIG3_H = "/home/precalkor/Integrated_Control_SW/DAQ_Control_SW/config3.h"
SCRIPT_V7 = "/home/precalkor/ADC/ADC_test/script_v7.sh"
FLAG_DIR = "/tmp/daq_flags"
PROCESSED_DIR = "/home/precalkor/ADC/ADC_test/Data/production/"
RESULT_DIR = "/home/precalkor/ADC/ADC_test/Data/FinalResult/"
RAW_DIR = "/home/precalkor/ADC/ADC_test/Data/RAW/Laser/"

# ch0/1/2 <-> SN1/SN2/SN3 as read from config3.h -- keep this file's PMT
# labels in sync with whatever's physically mounted.
CHANNEL_LABELS = {0: "SN1", 1: "SN2", 2: "SN3"}

OUT_CSV = os.path.expanduser(
    f"~/laser_intensity_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
)


# =============================================================================
# Helpers
# =============================================================================

def list_devices():
    return hid.enumerate(TamadenshiLaser.VENDOR_ID, TamadenshiLaser.PRODUCT_ID)


def die(msg):
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)


def set_config3_field(field: str, value: str):
    """In-place sed-style edit of a `const ... <field> = "..."` or `= N;` line
    in config3.h. This file is read by script_v7.sh via its own parse_config()
    (a grep+sed one-liner), so editing it here is exactly how the GUI itself
    changes wavelength/current/etc. before a run."""
    with open(CONFIG3_H, "r") as f:
        lines = f.readlines()
    pattern = re.compile(rf'(const\s+\S+\s+{re.escape(field)}\s*=\s*)(".*?"|[0-9]+)(\s*;)')
    changed = False
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if m:
            is_str = m.group(2).startswith('"')
            new_val = f'"{value}"' if is_str else str(value)
            lines[i] = pattern.sub(rf'\g<1>{new_val}\g<3>', line)
            changed = True
            break
    if not changed:
        die(f"config3.h: field '{field}' not found -- check the exact const name.")
    with open(CONFIG3_H, "w") as f:
        f.writelines(lines)


def next_daq_flag_snapshot():
    """Run numbers are auto-incremented by script_v7.sh from existing raw
    files, not chosen by us -- so to know which RUN_INT a run got, watch
    /tmp/daq_flags/daq_<N>.flag appear (script_v7.sh touches it right before
    calling execute_DAQ_v2) and read N back out of the filename."""
    return set(glob.glob(os.path.join(FLAG_DIR, "daq_*.flag")))


def wait_for_new_run(before_snapshot, timeout_s=120):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        after = set(glob.glob(os.path.join(FLAG_DIR, "daq_*.flag")))
        new = after - before_snapshot
        if new:
            m = re.search(r"daq_(\d+)\.flag", list(new)[0])
            if m:
                return int(m.group(1))
        time.sleep(0.5)
    die("Timed out waiting for the DAQ flag to appear -- did script_v7.sh start?")


def wait_for_done(run_int, timeout_s=1800):
    """Waits for done_<run>.flag -- set by script_v7.sh once
    DAQ -> prod_ntp_v7.C -> read_ntp_v7.C -> Draw_Contour_v3.C have all
    finished for this run."""
    done_flag = os.path.join(FLAG_DIR, f"done_{run_int}.flag")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if os.path.exists(done_flag):
            return True
        time.sleep(2)
    print(f"[WARN] run {run_int}: analysis chain did not finish within "
          f"{timeout_s}s. Proceeding anyway -- results may be incomplete.")
    return False


def find_result_file(run_int):
    matches = glob.glob(os.path.join(RESULT_DIR, f"*_{run_int:03d}.root")) + \
        glob.glob(os.path.join(RESULT_DIR, f"*_{run_int:04d}.root"))
    return matches[0] if matches else None


def read_result_branches(result_path):
    """Reads spe_mean, relativeQE, peak_to_valley, poisson_mu from each
    tree_ch<N> in the result file. Uses ROOT via a short-lived subprocess
    (rather than importing PyROOT into this process) so a ROOT crash on a bad
    fit can't take the whole scan down."""
    macro = f"""
    TFile f("{result_path}");
    for (int ch = 0; ch < 8; ch++) {{
        TTree* t = (TTree*)f.Get(Form("tree_ch%d", ch));
        if (!t) continue;
        double spe=-1, qe=-1, pv=-1, mu=-1;
        t->SetBranchAddress("spe_mean", &spe);
        t->SetBranchAddress("relativeQE", &qe);
        t->SetBranchAddress("peak_to_valley", &pv);
        t->SetBranchAddress("poisson_mu", &mu);
        t->GetEntry(0);
        printf("ROW ch=%d spe=%.6f qe=%.6f pv=%.6f mu=%.6f\\n", ch, spe, qe, pv, mu);
    }}
    """
    proc = subprocess.run(
        ["root", "-l", "-b", "-q", f"-e", macro],
        capture_output=True, text=True, timeout=120,
    )
    rows = {}
    for line in proc.stdout.splitlines():
        if not line.startswith("ROW "):
            continue
        parts = dict(kv.split("=") for kv in line[4:].split())
        ch = int(parts["ch"])
        rows[ch] = {
            "spe_mean_pC": float(parts["spe"]),
            "relativeQE_pct": float(parts["qe"]),
            "peak_to_valley": float(parts["pv"]),
            "poisson_mu": float(parts["mu"]),
        }
    return rows


def two_pe_fraction(mu: float) -> float:
    """Fraction of TRIGGERED (>=1 p.e.) events that are actually 2+ p.e.,
    from Poisson statistics alone: P(2)/(1-P(0)), mu = poisson_mu (already a
    fitted branch -- no new charge-spectrum fit needed). This is the standard
    multi-p.e.-contamination estimate for an SPE calibration light level."""
    import math
    if mu <= 0:
        return 0.0
    p0 = math.exp(-mu)
    p2 = math.exp(-mu) * mu**2 / 2.0
    denom = 1.0 - p0
    return (p2 / denom) if denom > 0 else 0.0


def evaluate(rows):
    """Checks the 3-PMT criteria. Returns (pass: bool, per_channel_notes: list[str])."""
    notes = []
    all_pass = True
    for ch, label in CHANNEL_LABELS.items():
        r = rows.get(ch)
        if r is None:
            notes.append(f"{label}(ch{ch}): NO DATA")
            all_pass = False
            continue
        qe_ok = QE_MIN <= r["relativeQE_pct"] <= QE_MAX
        charge_ok = CHARGE_MIN <= r["spe_mean_pC"] <= CHARGE_MAX
        pe2 = two_pe_fraction(r["poisson_mu"])
        pe2_ok = pe2 <= TWO_PE_MAX
        ok = qe_ok and charge_ok and pe2_ok
        all_pass = all_pass and ok
        notes.append(
            f"{label}(ch{ch}): QE={r['relativeQE_pct']:.2f}%{'OK' if qe_ok else 'X'} "
            f"Charge={r['spe_mean_pC']:.3f}pC{'OK' if charge_ok else 'X'} "
            f"2PE={pe2*100:.2f}%{'OK' if pe2_ok else 'X'} "
            f"P/V={r['peak_to_valley']:.2f}"
        )
    return all_pass, notes


# =============================================================================
# Main scan
# =============================================================================

def scan_wavelength(wl: str, dry_run: bool):
    bias = WAVELENGTHS_MA_BIAS[wl]
    usb_path = WAVELENGTH_USB_PATH.get(wl)
    if usb_path is None and not dry_run:
        die(f"WAVELENGTH_USB_PATH['{wl}'] is not set. Run this file's "
            f"list_devices() helper once, note the path for {wl}'s board, "
            f"and fill it in at the top of this script.")

    print(f"\n{'='*70}\n {wl}  (bias fixed at {bias:.1f} mA, "
          f"pulse {PULSE_CURRENTS_MA[0]}-{PULSE_CURRENTS_MA[-1]} mA)\n{'='*70}")

    laser = None
    if not dry_run:
        laser = TamadenshiLaser()
        ok, msg = laser.connect(usb_path)
        if not ok:
            die(f"{wl}: connect failed -- {msg}")
        laser.set_tec_on(True)
        time.sleep(2.0)  # let TEC start settling before firing

    results = []
    wl_num = wl.replace("nm", "")

    try:
        for pulse in PULSE_CURRENTS_MA:
            total = bias + pulse
            if total > LD_TOTAL_CURRENT_LIMIT_MA:
                print(f"  [SKIP] pulse={pulse} -> bias+pulse={total:.0f}mA "
                      f"exceeds {LD_TOTAL_CURRENT_LIMIT_MA:.0f}mA limit")
                continue

            print(f"\n  --- pulse={pulse} mA (bias={bias} mA, total={total} mA) ---")

            if dry_run:
                print(f"  [DRY RUN] would set currents, fire {EVENTS_PER_RUN} "
                      f"events, run prod/read chain, and evaluate.")
                continue

            # No combined set_currents() on the main app's driver -- both
            # setters called separately; the 200 mA sum check above already
            # gated this point before either write happens.
            if not laser.set_bias_current(bias) or not laser.set_pulse_current(pulse):
                print(f"  [ERROR] failed to write currents -- skipping this point.")
                continue
            laser.set_ld_on(True)
            time.sleep(1.0)  # let the LD output settle before acquiring

            set_config3_field("Wavelength", wl_num)
            set_config3_field("Laser", str(int(round(total))))
            set_config3_field("Events", str(EVENTS_PER_RUN))
            set_config3_field("NOTE", f"IntensityScan_{wl}_{pulse}mA")

            before = next_daq_flag_snapshot()
            subprocess.Popen(
                ["bash", SCRIPT_V7, "laser", CONFIG3_H, "0", "0", "0", "0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            run_int = wait_for_new_run(before)
            print(f"  run {run_int} acquiring ({EVENTS_PER_RUN} events)...")
            wait_for_done(run_int)

            laser.set_ld_on(False)  # dark between points -- don't leave it firing while we analyze

            result_path = find_result_file(run_int)
            if result_path is None:
                print(f"  [WARN] run {run_int}: no result file found -- skipping.")
                continue

            rows = read_result_branches(result_path)
            passed, notes = evaluate(rows)
            for n in notes:
                print(f"    {n}")
            print(f"    => {'PASS' if passed else 'fail'}")

            row = {"wavelength": wl, "pulse_mA": pulse, "bias_mA": bias,
                   "run": run_int, "pass": passed}
            for ch, label in CHANNEL_LABELS.items():
                r = rows.get(ch, {})
                row[f"{label}_QE_pct"] = r.get("relativeQE_pct")
                row[f"{label}_Charge_pC"] = r.get("spe_mean_pC")
                row[f"{label}_PV"] = r.get("peak_to_valley")
                row[f"{label}_2PE_pct"] = (
                    two_pe_fraction(r["poisson_mu"]) * 100 if "poisson_mu" in r else None
                )
            results.append(row)

    finally:
        if laser is not None:
            laser.set_ld_on(False)
            laser.set_bias_current(0.0)
            laser.set_pulse_current(0.0)
            laser.disconnect()

    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wavelengths", nargs="+", default=list(WAVELENGTHS_MA_BIAS.keys()),
                    help="Subset of wavelengths to scan (default: all configured).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan without firing the laser or acquiring.")
    ap.add_argument("--list-devices", action="store_true",
                    help="List attached Tamadenshi boards and exit (use this to "
                         "fill in WAVELENGTH_USB_PATH).")
    ap.add_argument("--pulse-currents", nargs="+", type=int, default=None,
                    help="Override PULSE_CURRENTS_MA for this run only, e.g. "
                         "'--pulse-currents 165' for a single-point smoke test "
                         "before committing to the full sweep.")
    args = ap.parse_args()

    if args.pulse_currents:
        global PULSE_CURRENTS_MA
        PULSE_CURRENTS_MA = args.pulse_currents

    if args.list_devices:
        for i, d in enumerate(list_devices()):
            print(f"[{i}] path={d['path'].decode(errors='replace')}  "
                  f"serial={d.get('serial_number') or '(none)'}  "
                  f"product={d.get('product_string') or '(none)'}")
        return

    for wl in args.wavelengths:
        if wl not in WAVELENGTHS_MA_BIAS:
            die(f"Unknown wavelength '{wl}'. Configured: {list(WAVELENGTHS_MA_BIAS.keys())}")

    all_results = []
    for wl in args.wavelengths:
        all_results.extend(scan_wavelength(wl, args.dry_run))

    if args.dry_run or not all_results:
        return

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_results[0].keys()))
        writer.writeheader()
        writer.writerows(all_results)
    print(f"\n[INFO] Full results: {OUT_CSV}")

    print(f"\n{'='*70}\n Optimal pulse current per wavelength (lowest passing current)\n{'='*70}")
    for wl in args.wavelengths:
        passing = [r for r in all_results if r["wavelength"] == wl and r["pass"]]
        if passing:
            best = min(passing, key=lambda r: r["pulse_mA"])
            print(f"  {wl}: {best['pulse_mA']} mA  (run {best['run']})")
        else:
            print(f"  {wl}: NO PASSING POINT in {PULSE_CURRENTS_MA} mA -- "
                  f"widen the range or check criteria")


if __name__ == "__main__":
    main()
