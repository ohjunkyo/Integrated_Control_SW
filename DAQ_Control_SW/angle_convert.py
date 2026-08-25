"""Stage angle <-> Hamamatsu incidence angle conversion.

A line-for-line port of ADC_test/angle_convert.h, which the analysis chain
(prod_ntp_v7, Draw_Uniformity_Norm_v7, Draw_Contour_v3) uses. The live scan
view needs the same numbers the offline report will later show, so this must
stay in lockstep with the header -- if the polynomial or the sign rules there
change, change them here too.

Why a port rather than calling ROOT: the live view converts on every UI
refresh, and spawning a ROOT process per point (~1 s each) would make the
plot lag several points behind the scan.
"""


def pos_map_angle(direction: str) -> int:
    """Cable direction letter -> its azimuth on the stage [deg]."""
    return {
        'E': 0,   'F': 45,  'G': 90,  'H': 135,
        'A': 180, 'B': 225, 'C': 270, 'D': 315,
    }.get((direction or 'A').upper(), 180)


def get_xy_rot_for_direction(direction: str):
    """Rotation-stage angles that align the PMT's X / Y axis with the scan axis."""
    pm = pos_map_angle(direction)
    xm = ((pm - 90) % 180 + 180) % 180 - 90     # canonical (-90, 90]
    ym = ((pm - 180) % 180 + 180) % 180 - 90
    x_rot = xm + 180 if xm < 0 else xm
    y_rot = ym + 180 if ym < 0 else ym
    return x_rot, y_rot


def convert_kr_to_hamamatsu(kr: float) -> float:
    """2nd-order calibration polynomial: stage angle -> PMT incidence angle."""
    return -0.0049 * kr * kr + 1.7515 * kr - 0.0402


def get_hamamatsu_angle(direction: str, tilt_val: float, rot_val: float):
    """Signed Hamamatsu incidence angle + which scan axis this point belongs to.

    Returns (hamamatsu_angle, axis_label) where axis_label is "X"/"Y"/"?".
    """
    x_rot, y_rot = get_xy_rot_for_direction(direction)
    rot = int(round(rot_val))
    is_x = (rot == x_rot)
    is_y = (rot == y_rot)
    axis = "X" if is_x else ("Y" if is_y else "?")

    # Full cable azimuth, so opposite cables (A/E, B/F, ...) keep distinct
    # signs even though x_rot/y_rot collapse them mod 180.
    delta = ((180 - pos_map_angle(direction)) % 360 + 360) % 360
    xflip = ((delta + x_rot) % 360 == 180)
    yflip = ((delta + y_rot) % 360 == 270)

    sgn = -1.0 if tilt_val < 0 else 1.0
    if is_x:
        sgn *= -1.0 if xflip else 1.0
    elif is_y:
        sgn *= 1.0 if yflip else -1.0
    return sgn * convert_kr_to_hamamatsu(abs(tilt_val)), axis
