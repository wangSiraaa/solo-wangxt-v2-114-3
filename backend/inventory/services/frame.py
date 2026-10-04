"""
Sampling-frame revision service.

Invariants enforced here (and double-enforced in deploy/postgis.sql):

* Every boundary / area / CRS statement becomes an immutable
  PlotFrameRevision moving draft -> reviewed -> published. The ORIGINAL
  boundary, area cross-check and the human publication reason are retained.
* A revision is validated, never silently accepted: a new boundary that
  EXCLUDES existing stem positions, whose polygon area disagrees with the
  declared area beyond tolerance, or which OVERLAPS a same-stratum neighbour
  opens FrameIssue pending items and forces the revision into `blocked`.
* Historical TreeMeasurement rows are never migrated, deleted or rewritten:
  an excluded stem becomes a pending item attached to the revision. Estimates
  bind explicitly to one published SamplingFrameVersion and per-hectare
  expansion of older confirmed estimates therefore never changes.
* Frame versions are append-only snapshots; publishing emits exactly one new
  SamplingFrameVersion atomically (a conditional status claim means two
  racing publish requests cannot produce two published versions).

Geometry note: rings are projected-metre polygons (same CRS contract as the
rest of the station). Polygon intersection area is computed by fan
triangulation + exact Sutherland-Hodgman triangle clipping, which is exact
for the convex plot polygons used by the station; overlap DETECTION for
arbitrary rings uses segment-intersection / point containment tests.
"""
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from inventory.models import (
    FrameIssue,
    ISSUE_AREA_MISMATCH,
    ISSUE_OPEN,
    ISSUE_OVERLAP,
    ISSUE_RESOLVED,
    ISSUE_TREE_EXCLUDED,
    Plot,
    PlotFrameRevision,
    REVISION_BLOCKED,
    REVISION_DRAFT,
    REVISION_PUBLISHED,
    REVISION_REVIEWED,
    SamplingFrameVersion,
    FRAME_SOURCE_BASELINE,
    FRAME_SOURCE_REVISION,
    TreeMeasurement,
)
from inventory.services.units import point_in_ring, ring_area_ha


class FrameRevisionError(Exception):
    """Domain-level rejection (maps to HTTP 400)."""


class FrameRevisionConflict(Exception):
    """State conflict (maps to HTTP 409), e.g. open revision / race."""


# ------------------------------------------------------------- ring geometry
def clean_ring(ring):
    """Validate a JSON ring and return it with the ring closed explicitly."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        raise FrameRevisionError(
            "boundary needs at least 4 [x, y] vertices (ring closed)")
    pts = []
    for v in ring:
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise FrameRevisionError("each boundary vertex must be [x_m, y_m]")
        try:
            pts.append([float(v[0]), float(v[1])])
        except (TypeError, ValueError):
            raise FrameRevisionError(f"boundary vertex not numeric: {v!r}")
    if pts[0] != pts[-1]:
        pts.append([pts[0][0], pts[0][1]])
    if len(pts) < 4:
        raise FrameRevisionError("boundary needs at least 3 distinct vertices")
    if ring_area_ha(pts) <= 0:
        raise FrameRevisionError(
            "boundary polygon area is zero or negative (degenerate/self-crossing)")
    return pts


def _segments_cross(p1, p2, p3, p4):
    """True when segments p1p2 and p3p4 cross in their interiors."""
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _clip_polygon(subject, clip):
    """
    Sutherland-Hodgman clip of any simple subject polygon to a CONVEX clip
    polygon. Returns the intersection vertices ([] when disjoint). Boundary
    contact only (shared edge/point) collapses to zero area.
    """
    def side(p, a, b):
        return ((b[0] - a[0]) * (p[1] - a[1])
                - (b[1] - a[1]) * (p[0] - a[0]))

    def inter(p1, p2, a, b):
        dc = (b[0] - a[0], b[1] - a[1])
        dp = (p2[0] - p1[0], p2[1] - p1[1])
        t = (dc[0] * (a[1] - p1[1]) - dc[1] * (a[0] - p1[0])) \
            / (dc[0] * dp[1] - dc[1] * dp[0])
        return [p1[0] + t * dp[0], p1[1] + t * dp[1]]

    out = [list(p) for p in subject]
    n = len(clip)
    for i in range(n):
        a, b = clip[i], clip[(i + 1) % n]
        if not out:
            return []
        inp, out = out, []
        s = inp[-1]
        ds = side(s, a, b) >= 0.0
        for e in inp:
            de = side(e, a, b) >= 0.0
            if de:
                if not ds:
                    out.append(inter(s, e, a, b))
                out.append(e)
            elif ds:
                out.append(inter(s, e, a, b))
            s, ds = e, de
    return out


def rings_overlap(a, b, eps=1e-9):
    """
    Positive-area overlap test for two closed rings.

    Plots in the station are convex (rectangles); clipping polygon A by
    convex polygon B yields the exact intersection, which also covers full
    containment. Merely sharing an edge or a corner yields zero area and is
    NOT an overlap (adjacent plots are legitimate). Segment crossing covers
    the concave-edge case as a fallback.
    """
    ea = list(zip(a[:-1], a[1:]))
    eb = list(zip(b[:-1], b[1:]))
    for (p1, p2) in ea:
        for (p3, p4) in eb:
            if _segments_cross(p1, p2, p3, p4):
                return True
    inter_poly = _clip_polygon(a[:-1], b[:-1])
    return _polygon_area(inter_poly) > eps


def _triangles(ring):
    """Fan triangulation from vertex 0 (exact for convex/star-shaped rings)."""
    pts = ring[:-1]
    return [(pts[0], pts[i], pts[i + 1])
            for i in range(1, len(pts) - 1)]


def _clip_triangle(subject, clip):
    return _clip_polygon(list(subject), list(clip))


def _polygon_area(poly):
    if not poly or len(poly) < 3:
        return 0.0
    # recentre before summing cross products (UTM-metre cancellation)
    ox, oy = poly[0]
    s = 0.0
    ring = poly + [poly[0]]
    for (x1, y1), (x2, y2) in zip(ring[:-1], ring[1:]):
        s += (x1 - ox) * (y2 - oy) - (x2 - ox) * (y1 - oy)
    return abs(s) * 0.5


def rings_intersection_area_ha(a, b):
    """
    Intersection area in hectares. Exact for convex rings (direct polygon
    clipping). The demo plots are convex; for concave rings the fan-
    triangulation fallback is a conservative helper and rings_overlap()
    remains the authoritative positive-area detection.
    """
    inter_poly = _clip_polygon(a[:-1], b[:-1])
    area = _polygon_area(inter_poly)
    if area > 0.0:
        return area / 10000.0
    # concave fallback
    if not rings_overlap(a, b):
        return 0.0
    area = 0.0
    for ta in _triangles(a):
        for tb in _triangles(b):
            pieces = _clip_triangle(list(ta), list(tb))
            if len(pieces) >= 3:
                area += _polygon_area(pieces)
    return area / 10000.0


# -------------------------------------------------------------- checksum etc
def content_checksum(boundary, declared_area_ha, crs_epsg):
    payload = json.dumps(
        {"boundary": boundary, "declared_area_ha": declared_area_ha,
         "crs_epsg": int(crs_epsg)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _issue_fingerprint(kind, payload):
    blob = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------- frame versions
def frame_plot_entry(plot, boundary, declared_area_ha, area_polygon_ha,
                     crs_epsg, revision_id=None):
    return {
        "code": plot.code,
        "stratum": plot.stratum_id,
        "stratum_code": plot.stratum.code,
        "x_m": plot.x_m,
        "y_m": plot.y_m,
        "boundary": boundary,
        "declared_area_ha": declared_area_ha,
        "area_polygon_ha": area_polygon_ha,
        "crs_epsg": int(crs_epsg),
        "revision_id": revision_id,
    }


def ensure_baseline_frame(crs_epsg, note=""):
    """
    Return the baseline SamplingFrameVersion (v1), creating it from the
    current Plot rows if needed. The baseline is the frame historical
    measurements were collected under.
    """
    existing = SamplingFrameVersion.objects.filter(version=1).first()
    if existing is not None:
        return existing
    payload = {}
    for p in Plot.objects.select_related("stratum").order_by("code"):
        payload[p.code] = frame_plot_entry(
            p, p.boundary, p.declared_area_ha, p.area_polygon_ha, crs_epsg)
    with transaction.atomic():
        try:
            frame = SamplingFrameVersion.objects.create(
                version=1, source=FRAME_SOURCE_BASELINE,
                plot_payload=payload,
                note=note or "Baseline sampling frame (original survey).")
        except Exception:
            # Concurrent creation (unique version) — the winner is the frame.
            frame = SamplingFrameVersion.objects.get(version=1)
    return frame


def latest_frame():
    return SamplingFrameVersion.objects.order_by("-version").first()


def get_frame(version):
    if version in (None, "latest"):
        frame = latest_frame()
    else:
        frame = SamplingFrameVersion.objects.filter(version=version).first()
    return frame


def effective_plot_state(plot, frame, default_crs_epsg=None):
    """
    Boundary/area of a plot AS OF a frame version.

    Falls back to the live Plot row when the plot did not yet exist when the
    (older) frame snapshot was emitted — a brand-new plot has never been
    revised, so its own original boundary is authoritative.
    """
    if frame is not None:
        entry = frame.plot_payload.get(plot.code)
        if entry is not None:
            return entry
    crs = default_crs_epsg
    if crs is None:
        from django.conf import settings
        crs = settings.SURVEY_CRS_EPSG
    return frame_plot_entry(
        plot, plot.boundary, plot.declared_area_ha, plot.area_polygon_ha, crs)


# ---------------------------------------------------------------- validation
def excluded_stems(plot, new_ring):
    """
    Existing measurements of this plot whose stem falls OUTSIDE the proposed
    ring. These are historical observations bound to the boundary in force
    when collected: they are LISTED as pending items, never moved/deleted.
    """
    out = []
    qs = (
        TreeMeasurement.objects
        .filter(tree__plot=plot)
        .select_related("tree", "campaign")
        .order_by("campaign__measured_on", "id")
    )
    for m in qs:
        if not point_in_ring(m.x_m, m.y_m, new_ring):
            out.append({
                "measurement_id": m.id,
                "tree_id": m.tree_id,
                "field_number": m.field_number_seen,
                "campaign": m.campaign.code,
                "x_m": m.x_m,
                "y_m": m.y_m,
                "status": m.status,
            })
    return out


def same_stratum_overlaps(plot, new_ring, frame):
    """Positive-area overlap with the current boundary of a SAME-stratum plot."""
    overlaps = []
    for other in Plot.objects.select_related("stratum").filter(
            stratum=plot.stratum).exclude(pk=plot.pk).order_by("code"):
        state = effective_plot_state(other, frame)
        other_ring = clean_ring(state["boundary"])
        if rings_overlap(new_ring, other_ring):
            area_ha = rings_intersection_area_ha(new_ring, other_ring)
            overlaps.append({
                "plot": other.code,
                "overlap_area_ha": round(area_ha, 6),
                "other_boundary_revision_id": state.get("revision_id"),
            })
    return overlaps


def area_check_result(declared_area_ha, polygon_area_ha, tolerance):
    if declared_area_ha <= 0:
        raise FrameRevisionError("declared_area_ha must be positive")
    declared_area_ha = float(declared_area_ha)
    polygon_area_ha = float(polygon_area_ha)
    rel = float(abs(polygon_area_ha - declared_area_ha) / declared_area_ha)
    within = bool(rel <= tolerance)
    return {
        "declared_area_ha": declared_area_ha,
        "polygon_area_ha": polygon_area_ha,
        "relative_error": round(rel, 6),
        "tolerance": float(tolerance),
        "within_tolerance": within,
        "detail": (
            f"polygon {polygon_area_ha:.4f} ha vs declared "
            f"{declared_area_ha:.4f} ha: {rel:.3%} (tolerance "
            f"{tolerance:.2%})"),
    }


def _open_issue_specs(revision, boundary, declared_area_ha, polygon_area_ha,
                      crs_epsg, tolerance, frame):
    """Compute the FULL set of open issues for a proposed geometry."""
    specs = []
    check = area_check_result(declared_area_ha, polygon_area_ha, tolerance)
    if not check["within_tolerance"]:
        payload = {k: check[k] for k in
                   ("declared_area_ha", "polygon_area_ha",
                    "relative_error", "tolerance")}
        specs.append((ISSUE_AREA_MISMATCH,
                      "declared/polygon area mismatch: " + check["detail"],
                      payload))
    stems = excluded_stems(revision.plot, boundary)
    if stems:
        specs.append((ISSUE_TREE_EXCLUDED,
                      f"new boundary excludes {len(stems)} existing stem "
                      "position(s); historical measurements are never moved — "
                      "resolve the boundary instead",
                      {"excluded": stems}))
    overlaps = same_stratum_overlaps(revision.plot, boundary, frame)
    if overlaps:
        specs.append((ISSUE_OVERLAP,
                      "boundary overlaps same-stratum plot(s): "
                      + ", ".join(f"{o['plot']} ({o['overlap_area_ha']} ha)"
                                  for o in overlaps),
                      {"overlaps": overlaps}))
    return specs, check


def _apply_validation(revision, boundary, declared_area_ha, polygon_area_ha,
                      crs_epsg, tolerance, frame):
    """
    Recompute issues against the current database, reconcile the issue rows
    (resolve conditions that disappeared, add new ones), and set status:
      any open issue -> blocked ; none -> draft (review must be re-done).
    """
    specs, check = _open_issue_specs(
        revision, boundary, declared_area_ha, polygon_area_ha, crs_epsg,
        tolerance, frame)
    wanted = {}
    for kind, detail, payload in specs:
        fp = _issue_fingerprint(kind, payload)
        wanted[fp] = (kind, detail, payload)

    now = timezone.now()
    for issue in revision.issues.all():
        if issue.status == ISSUE_OPEN and issue.fingerprint not in wanted:
            issue.status = ISSUE_RESOLVED
            issue.resolution_note = (
                "condition no longer present on re-validation "
                f"({now.isoformat()})")
            issue.resolved_at = now
            issue.save()

    for fp, (kind, detail, payload) in wanted.items():
        existing = revision.issues.filter(fingerprint=fp).first()
        if existing is None:
            FrameIssue.objects.create(
                revision=revision, kind=kind, detail=detail,
                payload=payload, fingerprint=fp)
        elif existing.status == ISSUE_RESOLVED:
            existing.status = ISSUE_OPEN
            existing.resolution_note = ""
            existing.resolved_at = None
            existing.save()

    revision.area_check = check
    revision.area_polygon_ha = polygon_area_ha
    revision.boundary = boundary
    revision.declared_area_ha = declared_area_ha
    revision.crs_epsg = int(crs_epsg)
    revision.content_checksum = content_checksum(
        boundary, declared_area_ha, crs_epsg)
    revision.status = REVISION_BLOCKED if wanted else REVISION_DRAFT
    revision.reviewed_at = None
    revision.save()
    return revision


# ------------------------------------------------------------- lifecycle API
@transaction.atomic
def create_revision(plot, boundary, declared_area_ha, crs_epsg,
                    tolerance, crs_note="", reason=""):
    """
    Propose a frame revision. Idempotent: re-uploading the SAME geometry
    (same boundary + declared area + CRS checksum) returns the existing
    revision with created=False instead of making a new row.
    """
    boundary = clean_ring(boundary)
    if crs_epsg is None:
        raise FrameRevisionError("crs_epsg is mandatory with every revision")
    frame = ensure_baseline_frame(crs_epsg)

    latest = PlotFrameRevision.objects.filter(plot=plot).order_by(
        "-revision_no").first()
    checksum = content_checksum(boundary, declared_area_ha, crs_epsg)
    if latest is not None and latest.status != REVISION_PUBLISHED:
        if latest.content_checksum == checksum:
            return latest, False
        raise FrameRevisionConflict(
            f"plot {plot.code} already has an open revision "
            f"#{latest.revision_no} [{latest.status}]; re-validate or publish "
            "that one instead of opening a competing proposal.")

    if latest is not None and latest.status == REVISION_PUBLISHED:
        original_boundary = latest.boundary
        original_declared = latest.declared_area_ha
        original_polygon = latest.area_polygon_ha
        original_crs = latest.crs_epsg
        supersedes = latest
        revision_no = latest.revision_no + 1
        if checksum == latest.content_checksum:
            # identical to what is already published: still idempotent
            return latest, False
    else:
        original_boundary = plot.boundary
        original_declared = plot.declared_area_ha
        original_polygon = plot.area_polygon_ha
        original_crs = crs_epsg  # the original survey shares the station CRS
        supersedes = None
        revision_no = 1

    polygon_area_ha = float(ring_area_ha(boundary))
    revision = PlotFrameRevision.objects.create(
        plot=plot, revision_no=revision_no, status=REVISION_DRAFT,
        original_boundary=original_boundary,
        original_declared_area_ha=original_declared,
        original_area_polygon_ha=original_polygon,
        original_crs_epsg=int(original_crs),
        boundary=boundary, declared_area_ha=declared_area_ha,
        area_polygon_ha=polygon_area_ha, crs_epsg=int(crs_epsg),
        crs_note=crs_note, area_tolerance=tolerance,
        area_check={}, content_checksum=checksum, reason=reason,
        supersedes=supersedes,
    )
    _apply_validation(revision, boundary, declared_area_ha, polygon_area_ha,
                      crs_epsg, tolerance, frame)
    return revision, True


@transaction.atomic
def revalidate_revision(revision, tolerance, updates=None):
    """
    Re-run every check against the CURRENT database. When `updates` carries a
    new boundary/area/CRS, replace the proposed geometry (allowed while the
    revision is not published; original_* columns never change). Open issues
    that disappeared are resolved; new ones are opened; status becomes
    blocked (issues) or draft (clean). Nothing is published by this call.
    """
    if revision.status == REVISION_PUBLISHED:
        raise FrameRevisionConflict(
            "published revisions are immutable; propose a new revision")
    updates = updates or {}
    boundary = clean_ring(updates.get("boundary", revision.boundary))
    declared = float(updates.get("declared_area_ha",
                                 revision.declared_area_ha))
    crs = int(updates.get("crs_epsg", revision.crs_epsg))
    if "crs_note" in updates:
        revision.crs_note = updates["crs_note"] or ""
        revision.save(update_fields=["crs_note"])
    polygon_area_ha = float(ring_area_ha(boundary))
    frame = latest_frame() or ensure_baseline_frame(crs)
    _apply_validation(revision, boundary, declared, polygon_area_ha, crs,
                      tolerance, frame)
    return revision


@transaction.atomic
def review_revision(revision, reason=""):
    """draft(clean) -> reviewed. Blocked revisions cannot be reviewed."""
    revision = PlotFrameRevision.objects.select_for_update().get(
        pk=revision.pk)
    open_issues = revision.issues.filter(status=ISSUE_OPEN).count()
    if open_issues:
        raise FrameRevisionConflict(
            f"{open_issues} open pending item(s); revision stays blocked "
            "until re-validation is clean.")
    if revision.status == REVISION_PUBLISHED:
        return revision
    if revision.status == REVISION_BLOCKED:
        raise FrameRevisionConflict(
            "revision is blocked by pending items; cannot be reviewed.")
    if reason:
        revision.reason = reason
    revision.status = REVISION_REVIEWED
    revision.reviewed_at = timezone.now()
    revision.save()
    return revision


def publish_revision(revision, reason="", tolerance=None):
    """
    reviewed -> published, atomically emitting ONE SamplingFrameVersion.

    NOT wrapped in one outer transaction: the blocked-state commit (phase 1)
    must survive raising the conflict, while the frame emission (phase 2)
    commits atomically. Each phase opens its own transaction block.

    Two racing publish requests cannot both succeed: the loser either fails
    the locked reviewed-status re-check (PostgreSQL row lock) or the unique
    frame-version insert (sqlite backstop), and gets a conflict.

    Final validation is re-run first. If it opens pending items, the revision
    is durably moved to `blocked` (issues persisted) in its OWN committed
    transaction, and NO frame version is emitted — callers can never be left
    with a half-published boundary or estimates against a phantom frame.
    """
    tol = tolerance if tolerance is not None else revision.area_tolerance

    # --- phase 1: locked claim + read-only final validation ---------------
    with transaction.atomic():
        claimed = (
            PlotFrameRevision.objects
            .filter(pk=revision.pk, status=REVISION_REVIEWED)
            .select_for_update()
        )
        if not claimed.exists():
            fresh = PlotFrameRevision.objects.get(pk=revision.pk)
            if fresh.status == REVISION_PUBLISHED:
                raise FrameRevisionConflict("revision already published")
            raise FrameRevisionConflict(
                f"revision is {fresh.status}, not reviewed; cannot publish")
        locked = claimed.first()
        frame = latest_frame()
        specs, _check = _open_issue_specs(
            locked, locked.boundary, locked.declared_area_ha,
            locked.area_polygon_ha, locked.crs_epsg, tol, frame)

    if specs:
        # Persist the blocked state + pending items, emit nothing.
        with transaction.atomic():
            revalidate_revision(locked, tol)
        raise FrameRevisionConflict(
            "final validation opened pending items; publication blocked and "
            "no frame version was emitted.")

    final_reason = (reason or locked.reason).strip()
    if not final_reason:
        raise FrameRevisionError(
            "a publication reason is required and is retained with the frame")

    # --- phase 2: conditional claim + atomic frame emission ----------------
    with transaction.atomic():
        claimed = (
            PlotFrameRevision.objects
            .filter(pk=revision.pk, status=REVISION_REVIEWED)
            .select_for_update()
        )
        if not claimed.exists():
            raise FrameRevisionConflict(
                "lost the publication race: the revision is no longer "
                "reviewed; only one published version can be emitted")
        locked = claimed.first()
        frame = latest_frame()
        new_version = frame.version + 1
        payload = {code: dict(entry)
                   for code, entry in frame.plot_payload.items()}
        # Self-contained snapshot: carry over plots added AFTER the previous
        # frame was emitted (never revised, so their original boundary is
        # authoritative) without mutating the older, immutable frame row.
        from django.conf import settings as dj_settings
        known = set(payload)
        for plot in Plot.objects.select_related("stratum").all():
            if plot.code not in known:
                payload[plot.code] = frame_plot_entry(
                    plot, plot.boundary, plot.declared_area_ha,
                    plot.area_polygon_ha, dj_settings.SURVEY_CRS_EPSG)
        payload[locked.plot.code] = frame_plot_entry(
            locked.plot, locked.boundary, locked.declared_area_ha,
            locked.area_polygon_ha, locked.crs_epsg, revision_id=locked.id)
        from django.db import IntegrityError, OperationalError
        try:
            # Nested savepoint: on PostgreSQL a unique(version) violation
            # aborts only the savepoint, so the conflict maps cleanly to 409.
            with transaction.atomic():
                new_frame = SamplingFrameVersion.objects.create(
                    version=new_version, source=FRAME_SOURCE_REVISION,
                    source_revision=locked, plot_payload=payload,
                    note=f"{locked.plot.code}: {final_reason}")
        except (IntegrityError, OperationalError):
            # PostgreSQL reports the raced unique(version) insert as
            # IntegrityError; a serialised backend (sqlite shared-cache)
            # reports the concurrent writer as OperationalError. Either way
            # another publish is in progress / won: no frame is emitted here.
            raise FrameRevisionConflict(
                "concurrent publication detected: a frame version was "
                "(or is being) emitted by another publish request; only one "
                "published revision is allowed per publish race.")

        locked.reason = final_reason
        locked.status = REVISION_PUBLISHED
        locked.published_at = timezone.now()
        locked.emitted_frame = new_frame
        locked.save()
        return locked, new_frame


# --------------------------------------------------------- compare / impact
def revision_comparison(revision):
    """Geometry/area old-vs-new summary for the compare API."""
    old_ring = clean_ring(revision.original_boundary)
    new_ring = clean_ring(revision.boundary)
    old_area = revision.original_area_polygon_ha
    new_area = revision.area_polygon_ha
    stems = []
    qs = (
        TreeMeasurement.objects
        .filter(tree__plot=revision.plot)
        .select_related("campaign")
        .order_by("campaign__measured_on", "id")
    )
    for m in qs:
        in_old = point_in_ring(m.x_m, m.y_m, old_ring)
        in_new = point_in_ring(m.x_m, m.y_m, new_ring)
        if in_old and not in_new:
            effect = "excluded"
        elif not in_old and in_new:
            effect = "newly_included"
        else:
            effect = "retained"
        stems.append({
            "measurement_id": m.id, "tree_id": m.tree_id,
            "field_number": m.field_number_seen,
            "campaign": m.campaign.code, "x_m": m.x_m, "y_m": m.y_m,
            "status": m.status, "in_original": in_old,
            "in_revised": in_new, "effect": effect,
        })
    return {
        "plot": revision.plot.code,
        "revision_no": revision.revision_no,
        "status": revision.status,
        "original": {
            "boundary": old_ring,
            "declared_area_ha": revision.original_declared_area_ha,
            "polygon_area_ha": old_area,
            "crs_epsg": revision.original_crs_epsg,
        },
        "revised": {
            "boundary": new_ring,
            "declared_area_ha": revision.declared_area_ha,
            "polygon_area_ha": new_area,
            "crs_epsg": revision.crs_epsg,
            "crs_note": revision.crs_note,
        },
        "area_check": revision.area_check,
        "area_delta_ha": round(new_area - old_area, 6),
        "per_hectare_factor": (
            round(old_area / new_area, 6) if new_area else None),
        "excluded_stems": [s for s in stems if s["effect"] == "excluded"],
        "newly_included_stems": [
            s for s in stems if s["effect"] == "newly_included"],
        "retained_stem_count": sum(1 for s in stems if s["effect"] == "retained"),
        "stems": stems,
        "overlap_area_ha_with_original": rings_intersection_area_ha(
            old_ring, new_ring),
    }


def revision_impact(revision):
    """
    Impact query: affected individuals + the estimate versions that the
    revision would (and would NOT) change. Confirmed estimates stay on their
    own frozen frame; only NEW drafts against the new frame use the new area.
    """
    from inventory.models import EstimateVersion

    comp = revision_comparison(revision)
    emitted = revision.emitted_frame
    qs = EstimateVersion.objects.filter(t1_campaign__isnull=False)
    bound_versions = [
        {
            "estimate_id": v.id,
            "label": v.label,
            "status": v.status,
            "frame_version": v.frame.version if v.frame else None,
            "affected_by_this_revision": bool(
                emitted is not None and v.frame_id == emitted.id),
        }
        for v in qs.select_related("frame").order_by("-created_at")
    ]
    return {
        "plot": revision.plot.code,
        "revision_no": revision.revision_no,
        "status": revision.status,
        "emitted_frame_version": emitted.version if emitted else None,
        "affected_measurements": comp["excluded_stems"],
        "newly_included_measurements": comp["newly_included_stems"],
        "per_hectare_expansion": {
            "original_area_ha": comp["original"]["polygon_area_ha"],
            "revised_area_ha": comp["revised"]["polygon_area_ha"],
            "kg_per_ha_multiplier_new_vs_old": comp["per_hectare_factor"],
            "note": "only NEW draft estimates bound to the emitted frame use "
                    "the revised area; older confirmed estimates are frozen "
                    "on their own frame and never change.",
        },
        "estimate_versions": bound_versions,
        "open_issues": [
            {"kind": i.kind, "detail": i.detail, "payload": i.payload}
            for i in revision.issues.filter(status=ISSUE_OPEN)],
    }
