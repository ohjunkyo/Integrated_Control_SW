"""Cable direction -> rotation-stage angle mapping.

Python mirror of ADC_test/angle_convert.h, which is the single source of truth
for this geometry and is shared by every analysis macro. Python cannot include
the C++ header, so this module restates the SAME formulas -- and
tests/test_angle_convert_matches_header.py parses the header and asserts the
two agree for all 8 cable directions, so drift fails a test instead of silently
mis-aiming the stage or mis-selecting files during analysis.

Do not hardcode a cable table anywhere else; import from here.
"""

# Standard pin position (deg) around the PMT for each cable direction A~H.
# Mirrors PosMapAngle() in angle_convert.h.
_POS_MAP = {'E': 0, 'F': 45, 'G': 90, 'H': 135,
            'A': 180, 'B': 225, 'C': 270, 'D': 315}

# The rotation stage only spans this range; targets outside it are folded by
# 180 deg, which points the same scan axis at the mirrored cathode region.
# angle_convert.h's GetXYRotForDirection folds mod 180 for exactly this reason.
ROT_MIN, ROT_MAX = 0, 135


def pos_map_angle(direction):
    """Pin position (deg) for a cable direction letter. Unknown -> 'A' (180)."""
    return _POS_MAP.get(str(direction).upper(), 180)


def rot_with_offset(direction, offset):
    """Stage rotation for `direction` at axis `offset` (0 = X scan, 90 = Y).

    Folded into [ROT_MIN, ROT_MAX] when the raw target is unreachable, matching
    GetXYRotForDirection(). The TILT sweep is NOT mirrored to compensate --
    analysis recovers that sign itself from the full cable azimuth (see
    GetHamamatsuAngle's xflip/yflip), and the raw stage always sweeps -55 -> +55.
    """
    rot = (pos_map_angle(direction) - 180 + offset) % 360
    if not (ROT_MIN <= rot <= ROT_MAX):
        folded = (rot - 180) % 360
        if ROT_MIN <= folded <= ROT_MAX:
            rot = folded
    return rot


def get_xy_rot_for_direction(direction):
    """(x_rot, y_rot) for a cable direction -- the C++ GetXYRotForDirection."""
    return rot_with_offset(direction, 0), rot_with_offset(direction, 90)
