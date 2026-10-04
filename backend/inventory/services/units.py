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
    """Shoelace area of a projected ring [m] -> hectares."""
    import numpy as np

    pts = np.asarray(ring, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 3:
        raise ValidationError("boundary needs at least 3 [x, y] vertices.")
    area_m2 = 0.5 * abs(
        np.dot(pts[:, 0], np.roll(pts[:, 1], -1))
        - np.dot(pts[:, 1], np.roll(pts[:, 0], -1))
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


def _segments_intersect(p1, p2, q1, q2):
    """True if closed segments p1p2 and q1q2 intersect (proper or touching)."""
    def orient(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0]))

    def on_segment(a, b, c):
        return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
                and min(a[1], b[1]) - 1e-9 <= c[1]
                <= max(a[1], b[1]) + 1e-9)

    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    if o1 == 0 and on_segment(p1, p2, q1):
        return True
    if o2 == 0 and on_segment(p1, p2, q2):
        return True
    if o3 == 0 and on_segment(q1, q2, p1):
        return True
    if o4 == 0 and on_segment(q1, q2, p2):
        return True
    return ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0))


def rings_overlap(ring_a, ring_b):
    """
    Simple-polygon overlap test for two closed rings (projected metres):
    edge crossing, or either ring containing a vertex of the other (covers
    total containment). No geometry libraries required (sqlite dev mode).
    """
    a = [tuple(p) for p in ring_a[:-1]] if ring_a[0] == ring_a[-1] \
        else [tuple(p) for p in ring_a]
    b = [tuple(p) for p in ring_b[:-1]] if ring_b[0] == ring_b[-1] \
        else [tuple(p) for p in ring_b]
    if any(point_in_ring(x, y, ring_b) for x, y in a):
        return True
    if any(point_in_ring(x, y, ring_a) for x, y in b):
        return True
    for i in range(len(a)):
        for j in range(len(b)):
            if _segments_intersect(a[i], a[(i + 1) % len(a)],
                                   b[j], b[(j + 1) % len(b)]):
                return True
    return False


def _polygon_clip(subject, clipping):
    """
    Sutherland-Hodgman clipping of one closed ring by another. Exact for
    convex clipping windows (the demo plots are rectangles); used only to
    report an indicative overlap area, never for the boolean overlap call.
    """
    def clip_edge(poly, a, b):
        if not poly:
            return []
        out = []
        ax, ay = a
        bx, by = b
        s = poly[-1]
        s_in = (bx - ax) * (s[1] - ay) - (by - ay) * (s[0] - ax) >= -1e-9
        for e in poly:
            e_in = (bx - ax) * (e[1] - ay) - (by - ay) * (e[0] - ax) >= -1e-9
            if e_in != s_in:
                dx, dy = e[0] - s[0], e[1] - s[1]
                denom = (bx - ax) * dy - (by - ay) * dx
                t = ((bx - ax) * (s[1] - ay) - (by - ay) * (s[0] - ax)) \
                    / (denom or 1e-15)
                out.append((s[0] + t * dx, s[1] + t * dy))
            if e_in:
                out.append(e)
            s, s_in = e, e_in
        return out

    poly = [tuple(p) for p in subject]
    for i in range(len(clipping)):
        poly = clip_edge(poly, clipping[i], clipping[(i + 1) % len(clipping)])
    return poly


def ring_intersection_area_ha(ring_a, ring_b):
    """Indicative overlap area [ha] via convex clipping (0 if disjoint)."""
    import numpy as np

    a = ring_a[:-1] if ring_a[0] == ring_a[-1] else ring_a
    b = ring_b[:-1] if ring_b[0] == ring_b[-1] else ring_b
    clipped = _polygon_clip([tuple(p) for p in a], [tuple(p) for p in b])
    if len(clipped) < 3:
        return 0.0
    pts = np.asarray(clipped, dtype=float)
    area_m2 = 0.5 * abs(
        np.dot(pts[:, 0], np.roll(pts[:, 1], -1))
        - np.dot(pts[:, 1], np.roll(pts[:, 0], -1))
    )
    return area_m2 / 10000.0
