"""Unit conversion & validation for field measurements.

Canonical storage is dbh [cm] and height [m]. The raw value and its declared
unit are kept alongside so unit mistakes are auditable. Wrong/missing units
raise ValidationError at ingest — a bare number is never accepted.
"""
from django.core.exceptions import ValidationError

from inventory.models import DBH_TO_CM, DBH_UNITS, HEIGHT_UNITS

DBH_RANGE_CM = (1.0, 200.0)
HEIGHT_RANGE_M = (0.3, 120.0)


def convert_dbh_to_cm(raw, unit):
    if unit not in DBH_UNITS:
        raise ValidationError(
            f"dbh_unit must be one of {DBH_UNITS}, got {unit!r}. Every dbh "
            "entry must declare its unit explicitly."
        )
    if raw is None:
        raise ValidationError("dbh_raw is required with a unit (use status "
                              "'alive_not_measured' for missing dbh).")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"dbh_raw not numeric: {raw!r}")
    cm = value * DBH_TO_CM[unit]
    lo, hi = DBH_RANGE_CM
    if not (lo <= cm <= hi):
        raise ValidationError(
            f"dbh {value} {unit} -> {cm:.2f} cm outside plausible range "
            f"[{lo}, {hi}] cm (likely wrong unit, e.g. mm entered as cm)."
        )
    return cm


def convert_height_to_m(raw, unit):
    if unit not in HEIGHT_UNITS:
        raise ValidationError(
            f"height_unit must be one of {HEIGHT_UNITS}, got {unit!r}."
        )
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"height_raw not numeric: {raw!r}")
    m = value
    lo, hi = HEIGHT_RANGE_M
    if not (lo <= m <= hi):
        raise ValidationError(
            f"height {value} {unit} outside plausible range [{lo}, {hi}] m "
            "(perhaps recorded in cm instead of m)."
        )
    return m


def ring_area_ha(ring):
    """Shoelace area of a projected ring [m] -> hectares.

    Coordinates are shifted to the first vertex before summing: UTM metres
    (~5e5 x, ~4e6 y) otherwise cause catastrophic float cancellation in the
    cross products (the two ~1e13 sums differ by only ~1e4), which can
    corrupt the area by orders of magnitude for otherwise-valid polygons.
    """
    import numpy as np

    pts = np.asarray(ring, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 3:
        raise ValidationError("boundary needs at least 3 [x, y] vertices.")
    shifted = pts - pts[0]
    area_m2 = 0.5 * abs(
        np.dot(shifted[:, 0], np.roll(shifted[:, 1], -1))
        - np.dot(shifted[:, 1], np.roll(shifted[:, 0], -1))
    )
    return area_m2 / 10000.0


def point_in_ring(x, y, ring):
    """Ray-casting point-in-polygon for a single projected ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside
