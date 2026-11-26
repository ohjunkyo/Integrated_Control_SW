#!/usr/bin/env python3
"""Guards the one duplicated piece of geometry we cannot deduplicate.

managers/angle_convert.py must agree with ADC_test/angle_convert.h, the C++
single source of truth used by every analysis macro. Python cannot include the
header, so this test PARSES it -- both the PosMapAngle() pin table and the
GetXYRotForDirection() folding -- and checks all 8 cable directions.

If this fails, the DAQ would aim the stage at one rotation while analysis looked
for files at another. That is exactly how ch0 silently mis-selected files after
Device 2 was rewired B->H (2026-08-12).

Run:  python3 test_angle_convert_matches_header.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from managers.angle_convert import get_xy_rot_for_direction, pos_map_angle

HEADER = "/home/precalkor/ADC/ADC_test/angle_convert.h"
DIRECTIONS = "ABCDEFGH"


def parse_pos_map(text):
    """Pull PosMapAngle()'s `case 'X': return N;` pairs out of the header."""
    body = text.split("inline int PosMapAngle", 1)[1].split("}", 1)[0]
    pairs = re.findall(r"case\s+'([A-H])'\s*:\s*return\s+(-?\d+)\s*;", body)
    return {d: int(v) for d, v in pairs}


def header_xy_rot(pin_deg):
    """GetXYRotForDirection() transcribed from the header, applied to a pin angle."""
    xm = (((pin_deg - 90) % 180) + 180) % 180 - 90
    ym = (((pin_deg - 180) % 180) + 180) % 180 - 90
    return (xm + 180 if xm < 0 else xm), (ym + 180 if ym < 0 else ym)


def main():
    if not os.path.exists(HEADER):
        print(f"SKIP: header not found at {HEADER}")
        return 0

    with open(HEADER) as f:
        text = f.read()

    pin_map = parse_pos_map(text)
    if len(pin_map) != 8:
        print(f"FAIL: parsed {len(pin_map)} pin entries from the header, expected 8")
        return 1

    failures = []
    for d in DIRECTIONS:
        if pin_map[d] != pos_map_angle(d):
            failures.append(f"  {d}: pin angle header={pin_map[d]} python={pos_map_angle(d)}")
            continue
        want = header_xy_rot(pin_map[d])
        got = get_xy_rot_for_direction(d)
        if want != got:
            failures.append(f"  {d}: x/y rot header={want} python={got}")

    if failures:
        print("FAIL: managers/angle_convert.py disagrees with angle_convert.h")
        print("\n".join(failures))
        return 1

    print(f"OK: all {len(DIRECTIONS)} cable directions match angle_convert.h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
