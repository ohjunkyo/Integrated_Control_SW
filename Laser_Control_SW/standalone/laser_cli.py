#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Command-line front end for the Tamadenshi LD board (standalone edition).

No GUI, no config files, no absolute paths -- everything it needs is in this
directory. Run `./laser_cli.py list` first to see which boards are attached,
then address a specific one with `--index N` (or `--serial ...`) if more than
one is plugged in.

Examples:
    ./laser_cli.py list
    ./laser_cli.py status
    ./laser_cli.py monitor --interval 2
    ./laser_cli.py set --bias 20 --pulse 145
    ./laser_cli.py set --temp 25 --tec on
    ./laser_cli.py set --trigger ext
    ./laser_cli.py on          # LD ON  (asks for confirmation)
    ./laser_cli.py off
    ./laser_cli.py pulse-width            # read
    ./laser_cli.py pulse-width --set 680  # write (asks for confirmation)
"""

import argparse
import sys
import time

try:
    from laser_driver import TamadenshiLaser, list_devices, DATA_LOG_DIR
except ImportError:
    print("Error: laser_driver.py not found next to this script.")
    sys.exit(1)


# --- device selection ----------------------------------------------------

def pick_device(args):
    """Resolve --path/--index/--serial into a single device, or exit with a
    helpful message. Boards share one VID/PID and report NO serial number, so
    the physical USB path is the only stable handle when several are attached
    -- prefer --path in scripts, since index order follows enumeration and can
    change across reboots."""
    devs = list_devices()
    if not devs:
        print("❌ No Tamadenshi LD board found.")
        print("   - Is the USB cable connected?")
        print("   - Are the udev rules installed? (see install.sh)")
        sys.exit(1)

    if args.path:
        want = args.path.encode()
        matches = [d for d in devs if d['path'] == want]
        if not matches:
            print(f"❌ No board at path '{args.path}'. "
                  "Run 'list' to see what is attached.")
            sys.exit(1)
        return matches[0]

    if args.serial:
        matches = [d for d in devs
                   if (d.get('serial_number') or '') == args.serial]
        if not matches:
            print(f"❌ No board with serial '{args.serial}'. "
                  "Run 'list' to see what is attached.")
            sys.exit(1)
        return matches[0]

    if args.index is not None:
        if not (0 <= args.index < len(devs)):
            print(f"❌ --index {args.index} out of range "
                  f"(found {len(devs)} board(s)). Run 'list'.")
            sys.exit(1)
        return devs[args.index]

    if len(devs) > 1:
        print(f"❌ {len(devs)} boards attached -- specify which one with "
              "--index N or --serial SN. Run 'list' to see them.")
        sys.exit(1)

    return devs[0]


def open_device(args) -> TamadenshiLaser:
    dev = pick_device(args)
    laser = TamadenshiLaser(name=dev.get('serial_number') or "LD")
    ok, msg = laser.connect(dev['path'])
    if not ok:
        sys.exit(1)
    return laser


def confirm(prompt: str, assume_yes: bool) -> bool:
    """Gate anything that changes what the hardware actually emits."""
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# --- commands ------------------------------------------------------------

def cmd_list(args):
    devs = list_devices()
    if not devs:
        print("No Tamadenshi LD board found.")
        return 1
    print(f"{len(devs)} board(s) found:\n")
    for i, d in enumerate(devs):
        print(f"  [{i}] serial : {d.get('serial_number') or '(none)'}")
        print(f"      product: {d.get('product_string') or '(none)'}")
        print(f"      path   : {d['path'].decode(errors='replace')}")
        print()
    return 0


def format_status(st: dict) -> str:
    pd = (f"{st['pd_current'] * 1000.0:.4f} mA (raw {st['pd_raw']})"
          if st.get('pd_valid') else
          f"n/a -- photodiode reads 0 (raw {st.get('pd_raw', 0)})")
    pw = st.get('pulse_width_ps')
    lines = [
        f"  LD             : {'ON' if st.get('ld_on') else 'off'}",
        f"  TEC            : {'ON' if st.get('tec_on') else 'off'}",
        f"  LD temperature : {st.get('ld_temp', 0.0):.2f} °C",
        f"  Bias  (setpt)  : {st.get('bias', 0.0):.2f} mA",
        f"  Pulse (setpt)  : {st.get('pulse', 0.0):.2f} mA",
        f"  Pulse width    : {str(pw) + ' ps' if pw else '(not read yet)'}",
        f"  Photodiode     : {pd}",
        f"  Alarm/Interlock: {st.get('alarm')} / {st.get('interlock')}"
        f"   (info byte 0x{st.get('info_byte', 0):02X})",
    ]
    return "\n".join(lines)


def cmd_status(args):
    laser = open_device(args)
    try:
        # Pull the pulse width once so the status line is complete; it lives in
        # EEPROM and is not part of the periodic status report.
        laser.get_pulse_width_ps()
        if not laser.update_status(log_csv=False):
            print("❌ Failed to read status.")
            return 1
        print()
        print(format_status(laser.status))
        print()
        return 0
    finally:
        laser.disconnect()


def cmd_monitor(args):
    laser = open_device(args)
    try:
        laser.get_pulse_width_ps()
        print(f"Polling every {args.interval}s. CSV log dir: {DATA_LOG_DIR}")
        print("(rows are written only while the LD is on) -- Ctrl-C to stop\n")
        print(f"{'time':<10} {'temp[C]':>8} {'bias[mA]':>9} "
              f"{'pulse[mA]':>10} {'PD[mA]':>10}  LD  TEC")
        while True:
            if laser.update_status():
                st = laser.status
                pdtxt = (f"{st['pd_current'] * 1000.0:10.4f}"
                         if st.get('pd_valid') else f"{'n/a':>10}")
                print(f"{time.strftime('%H:%M:%S'):<10} "
                      f"{st.get('ld_temp', 0):8.2f} "
                      f"{st.get('bias', 0):9.2f} "
                      f"{st.get('pulse', 0):10.2f} "
                      f"{pdtxt}  "
                      f"{'ON ' if st.get('ld_on') else 'off'} "
                      f"{'ON' if st.get('tec_on') else 'off'}")
            else:
                print(f"{time.strftime('%H:%M:%S'):<10} read failed")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        laser.disconnect()


def cmd_set(args):
    if not any([args.bias is not None, args.pulse is not None,
                args.temp is not None, args.tec, args.trigger]):
        print("Nothing to do -- pass at least one of "
              "--bias/--pulse/--temp/--tec/--trigger.")
        return 1

    laser = open_device(args)
    try:
        rc = 0
        # Currents first, and always through set_currents() so the COMBINED
        # 200 mA ceiling is enforced. Writing one field at a time cannot see
        # the sum and would let 150+150 mA through.
        if args.bias is not None or args.pulse is not None:
            if not laser.update_status(log_csv=False):
                print("❌ Cannot read current setpoints; refusing to write.")
                return 1
            bias = args.bias if args.bias is not None else laser.status['bias']
            pulse = args.pulse if args.pulse is not None else laser.status['pulse']
            if not confirm(f"Set bias {bias:.1f} mA + pulse {pulse:.1f} mA "
                           f"(total {bias + pulse:.1f} mA)?", args.yes):
                print("Aborted.")
                return 1
            ok, msg = laser.set_currents(bias, pulse)
            print(("✅ " if ok else "❌ ") + msg)
            rc |= 0 if ok else 1

        if args.temp is not None:
            if laser.set_temp(args.temp):
                print(f"✅ TEC target set to {args.temp:.1f} °C.")
            else:
                print("❌ Failed to set temperature.")
                rc = 1

        if args.tec:
            state = args.tec == "on"
            if laser.set_tec_on(state):
                print(f"✅ TEC {'ON' if state else 'OFF'}.")
            else:
                print("❌ Failed to switch TEC.")
                rc = 1

        if args.trigger:
            modes = {"pg1": (True, False, False),
                     "pg2": (False, True, False),
                     "ext": (False, False, True),
                     "off": (False, False, False)}
            pg1, pg2, ext = modes[args.trigger]
            if laser.set_trigger_mode(pg1, pg2, ext):
                print(f"✅ Trigger source: {args.trigger}.")
            else:
                print("❌ Failed to set trigger.")
                rc = 1
        return rc
    finally:
        laser.disconnect()


def cmd_on(args):
    laser = open_device(args)
    try:
        if not laser.update_status(log_csv=False):
            print("❌ Cannot read status; refusing to fire.")
            return 1
        st = laser.status
        print(f"\nAbout to turn the LASER ON with:")
        print(f"  bias {st['bias']:.1f} mA, pulse {st['pulse']:.1f} mA, "
              f"temp {st['ld_temp']:.1f} °C")
        if not confirm("Emit laser light now?", args.yes):
            print("Aborted.")
            return 1
        if laser.set_ld_on(True):
            print("✅ LD ON.")
            return 0
        print("❌ Failed to turn LD on.")
        return 1
    finally:
        laser.disconnect()


def cmd_off(args):
    laser = open_device(args)
    try:
        if laser.set_ld_on(False):
            print("✅ LD OFF.")
            return 0
        print("❌ Failed to turn LD off.")
        return 1
    finally:
        laser.disconnect()


def cmd_pulse_width(args):
    laser = open_device(args)
    try:
        if args.set is None:
            pw = laser.get_pulse_width_ps(args.address)
            if pw is None:
                print("❌ Pulse-width read failed (retry -- the board "
                      "sometimes returns a stale response).")
                return 1
            print(f"Pulse width @ slot {args.address}: {pw} ps")
            return 0

        current = laser.get_pulse_width_ps(args.address)
        print(f"Current pulse width @ slot {args.address}: "
              f"{current if current is not None else '(unreadable)'} ps")
        # Writing slot 0 changes the live optical output immediately AND
        # persists to EEPROM, so this is not a dry run.
        if not confirm(f"Write {args.set} ps to slot {args.address}? "
                       "(changes what the laser emits)", args.yes):
            print("Aborted.")
            return 1
        try:
            ok = laser.set_pulse_width_ps(args.set, args.address)
        except ValueError as e:
            print(f"❌ {e}")
            return 1
        if ok:
            print(f"✅ Pulse width set to {args.set} ps.")
            return 0
        print("❌ Write failed.")
        return 1
    finally:
        laser.disconnect()


# --- argument parsing ----------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Tamadenshi LD board control (standalone CLI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--path",
                   help="Select a board by USB path (see 'list'). Most stable "
                        "identifier -- prefer this in scripts.")
    p.add_argument("--index", type=int,
                   help="Which board, when several are attached (see 'list'). "
                        "Enumeration order, may change across reboots.")
    p.add_argument("--serial",
                   help="Select a board by serial number. Note: these boards "
                        "report no serial, so this rarely works.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip confirmation prompts (for scripts).")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Enumerate attached boards.").set_defaults(
        func=cmd_list)
    sub.add_parser("status", help="Print a one-shot status snapshot.").set_defaults(
        func=cmd_status)

    m = sub.add_parser("monitor",
                       help="Poll status continuously and log to CSV.")
    m.add_argument("--interval", type=float, default=1.0,
                   help="Seconds between polls (default 1).")
    m.set_defaults(func=cmd_monitor)

    s = sub.add_parser("set", help="Change bias/pulse/temperature/trigger.")
    s.add_argument("--bias", type=float, help="Bias current in mA.")
    s.add_argument("--pulse", type=float, help="Pulse current in mA.")
    s.add_argument("--temp", type=float, help="TEC target temperature in °C.")
    s.add_argument("--tec", choices=["on", "off"], help="Switch the TEC.")
    s.add_argument("--trigger", choices=["pg1", "pg2", "ext", "off"],
                   help="Trigger source.")
    s.set_defaults(func=cmd_set)

    sub.add_parser("on", help="Turn the laser diode ON.").set_defaults(func=cmd_on)
    sub.add_parser("off", help="Turn the laser diode OFF.").set_defaults(func=cmd_off)

    w = sub.add_parser("pulse-width", help="Read or write the pulse width.")
    w.add_argument("--set", type=int, metavar="PS",
                   help="Write this width in ps (100-10230). Omit to read.")
    w.add_argument("--address", type=int, default=0,
                   help="EEPROM slot 0-9; 0 is the live one (default).")
    w.set_defaults(func=cmd_pulse_width)

    return p


def main():
    args = build_parser().parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
