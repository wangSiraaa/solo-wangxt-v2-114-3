"""
Sampling-frame revision workflow.

A resurveyed plot boundary is a VERSIONED EDITION of the sampling frame, not
an in-place edit to ``Plot.boundary``. The lifecycle is::

    draft -> reviewed -> published

Guarantees enforced here
========================
* The ORIGINAL boundary, declared vs polygon area cross-check, CRS statement
  and publication reason are all retained on the revision row; published
  rows are immutable (model save() guard + PostGIS trigger).
* A new edition is validated BEFORE it can do anything: if it excludes an
  existing stem position, its declared area is beyond tolerance of the
  polygon, or it overlaps a same-stratum plot, an open FrameRevisionIssue is
  raised and publication is blocked. Issues are resolved only by a
  documented human decision — never recomputed away.
* Publication is atomic. Within the same transaction the current frame is
  advanced and every DRAFT estimate for that plot is re-expanded against the
  new edition (or the whole publication rolls back), so there is never a
  half-published boundary alongside stale/wrong estimates.
* Historical TreeMeasurement rows stay attributed (``collected_frame``) to
  the boundary in force when they were collected: nothing migrates,
  deletes or rewrites them.
* Identical geometry + area + CRS uploads are idempotent (same revision
  returned), and the DB unique constraint (one published per plot) makes
  two racing publish requests produce exactly ONE published edition.
"""
import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from inventory.models import (
    FRAME_DRAFT,
    FRAME_PUBLISHED,
    FRAME_REVIEWED,
    FRAME_SUPERSEDED,
    ISSUE_AREA_MISMATCH,
    ISSUE_CRS,
    ISSUE_EXCLUDED_TREE,
    ISSUE_OPEN,
    ISSUE_OVERLAP,
    ISSUE_RESOLVED,
    FrameRevisionIssue,
    Plot,
    PlotFrameRevision,
    TreeMeasurement,
)
from inventory.services.units import (
    point_in_ring,
    ring_area_ha,
    ring_intersection_area_ha,
    rings_overlap,
)

# ---------------------------------------------------------------- errors
class FrameWorkflowError(Exception):
    """Illegal lifecycle transition (draft/reviewed/published)."""


class FrameBlockedError(Exception):
    """Publication attempted while open blocking issues remain."""

    def __init__(self, issues):
        self.issues = issues
        super().__init__(
            "publication blocked by "
            f"{len(issues)} open issue(s): "
            + "; ".join(i.summary for i in issues))


# ------------------------------------------------------------- checksums
def canonical_ring(boundary):
    """Return the ring as a closed list of rounded [x, y] float pairs."""
    try:
        ring = [[float(x), float(y)] for x, y in boundary]
    except (TypeError, ValueError, IndexError):
        raise DjValidationError(
            "boundary must be a ring of [x_m, y_m] pairs in the stated CRS."
        )
    if len(ring) < 3:
        raise DjValidationError("boundary needs at least 3 [x, y] vertices.")
    # Close the ring in the canonical form so [[...], first] and an open
    # ring describing the same polygon hash identically.
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _sha(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def geometry_checksum(boundary):
    ring = canonical_ring(boundary)
    return _sha([[round(x, 6), round(y, 6)] for x, y in ring])


def content_checksum(boundary, declared_area_ha, crs_epsg):
    return _sha({
        "ring": geometry_checksum(boundary),
        "declared_area_ha": round(float(declared_area_ha), 9),
        "crs_epsg": int(crs_epsg),
    })


# ----------------------------------------------------------- baseline frame
def ensure_baseline_frame(plot):
    """
    The frame edition implicit in the historical Plot row, published as
    edition #1 so historical measurements and old estimates have an
    explicit frame to belong to. Idempotent (unique content checksum).
    """
    chk = content_checksum(plot.boundary, plot.declared_area_ha,
                           settings.SURVEY_CRS_EPSG)
    existing = PlotFrameRevision.objects.filter(
        plot=plot, content_checksum=chk).first()
    if existing:
        return existing, False
    ring = canonical_ring(plot.boundary)
    poly_ha = ring_area_ha(ring)
    now = timezone.now()
    with transaction.atomic():
        rev, created = PlotFrameRevision.objects.get_or_create(
            plot=plot, content_checksum=chk,
            defaults=dict(
                revision_no=_next_revision_no(plot),
                status=FRAME_PUBLISHED,
                declared_area_ha=plot.declared_area_ha,
                boundary=ring,
                polygon_area_ha=poly_ha,
                area_check=_area_check(plot.declared_area_ha, poly_ha,
                                       settings.PLOT_AREA_TOLERANCE),
                crs_epsg=settings.SURVEY_CRS_EPSG,
                crs_note="Baseline frame derived from the historical plot "
                         "polygon at the start of frame revisioning.",
                geometry_checksum=geometry_checksum(ring),
                reason="Historical sampling frame (pre-revision baseline).",
                validation_payload={"baseline": True},
                published_at=now,
                publication_reason="Baseline publication of the pre-existing "
                                   "surveyed frame.",
            ),
        )
    return rev, created


def ensure_all_baseline_frames():
    return [ensure_baseline_frame(p)[0]
            for p in Plot.objects.select_related("stratum")]


def _next_revision_no(plot):
    last = (PlotFrameRevision.objects.filter(plot=plot)
            .order_by("-revision_no").values_list("revision_no", flat=True)
            .first())
    return (last or 0) + 1


def latest_published_frame(plot):
    """Published edition currently in force (baseline auto-created)."""
    rev = (PlotFrameRevision.objects
           .filter(plot=plot, status=FRAME_PUBLISHED)
           .order_by("-revision_no").first())
    if rev is None:
        rev, _ = ensure_baseline_frame(plot)
    return rev


def effective_frame_map():
    """{plot_code: published revision} for every plot (baselines ensured)."""
    out = {}
    for plot in Plot.objects.select_related("stratum"):
        out[plot.code] = latest_published_frame(plot)
    return out


# ------------------------------------------------------------- validation
def _area_check(declared_ha, polygon_ha, tolerance):
    if not declared_ha or declared_ha <= 0:
        raise DjValidationError("declared_area_ha must be positive")
    declared_ha = float(declared_ha)
    polygon_ha = float(polygon_ha)
    rel = float(abs(polygon_ha - declared_ha) / declared_ha)
    return {
        "declared_area_ha": round(declared_ha, 6),
        "polygon_area_ha": round(polygon_ha, 6),
        "absolute_diff_ha": round(abs(polygon_ha - declared_ha), 6),
        "relative_diff": round(rel, 6),
        "tolerance": float(tolerance),
        "passed": bool(rel <= tolerance),
    }


def _existing_stem_rows(plot):
    """
    Every historical stem position attributed to the plot. A revision that
    drops ANY of them excludes a real historical observation — blocking.
    Returns one row per TreeMeasurement (tree, campaign, x, y, frame).
    """
    return list(
        TreeMeasurement.objects
        .filter(tree__plot=plot)
        .select_related("tree", "campaign")
        .order_by("campaign__measured_on", "tree__current_field_number")
    )


def _same_stratum_other_boundaries(plot):
    """Currently-effective boundaries of every other plot in the stratum."""
    others = (Plot.objects.filter(stratum_id=plot.stratum_id)
              .exclude(pk=plot.pk).select_related("stratum"))
    out = []
    for other in others:
        frame = latest_published_frame(other)
        out.append((other, frame))
    return out


def validate_revision(revision, tolerance=None, station_crs=None,
                      persist=True):
    """
    Run every check for the proposed edition.

    Returns a dict with ``passed`` plus structured results. With
    ``persist=True`` findings are upserted as FrameRevisionIssue rows;
    existing issues whose finding no longer holds are left untouched (a
    human already acknowledged them) and genuinely new findings are opened.
    """
    tolerance = settings.PLOT_AREA_TOLERANCE if tolerance is None else tolerance
    station_crs = (settings.SURVEY_CRS_EPSG if station_crs is None
                   else station_crs)

    ring = canonical_ring(revision.boundary)
    poly_ha = ring_area_ha(ring)
    revision.polygon_area_ha = poly_ha
    area = _area_check(revision.declared_area_ha, poly_ha, tolerance)
    revision.area_check = area

    # 1) declared area vs polygon
    area_issue = None
    if not area["passed"]:
        area_issue = dict(
            kind=ISSUE_AREA_MISMATCH,
            detail=area,
            summary=(f"declared {area['declared_area_ha']:.4f} ha vs polygon "
                     f"{area['polygon_area_ha']:.4f} ha "
                     f"({area['relative_diff']:.2%}, tolerance "
                     f"{tolerance:.2%})"),
        )

    # 2) CRS statement
    crs_issue = None
    if int(revision.crs_epsg) != int(station_crs):
        crs_issue = dict(
            kind=ISSUE_CRS,
            detail={"revision_crs_epsg": revision.crs_epsg,
                    "station_crs_epsg": station_crs},
            summary=(f"CRS statement EPSG:{revision.crs_epsg} differs from "
                     f"station survey CRS EPSG:{station_crs}"),
        )

    # 3) existing stems excluded by the proposed ring
    excluded = []
    for m in _existing_stem_rows(revision.plot):
        if not point_in_ring(m.x_m, m.y_m, ring):
            collected = m.collected_frame
            excluded.append({
                "tree_id": m.tree_id,
                "tree": f"{m.tree.plot.code}/"
                        f"{m.tree.current_field_number}",
                "field_number_seen": m.field_number_seen,
                "campaign": m.campaign.code,
                "x_m": m.x_m, "y_m": m.y_m,
                "collected_frame_revision": (
                    collected.revision_no if collected else None),
            })
    tree_issues = [
        dict(kind=ISSUE_EXCLUDED_TREE, detail=e,
             summary=(f"{e['tree']} @ {e['campaign']} at ({e['x_m']}, "
                      f"{e['y_m']}) is outside the proposed boundary"))
        for e in excluded
    ]

    # 4) overlap with same-stratum plots
    overlaps = []
    for other, other_frame in _same_stratum_other_boundaries(revision.plot):
        if bool(rings_overlap(ring, other_frame.boundary)):
            overlap_ha = float(ring_intersection_area_ha(
                ring, other_frame.boundary))
            overlaps.append({
                "other_plot": other.code,
                "other_frame_revision": other_frame.revision_no,
                "overlap_area_ha": round(overlap_ha, 6),
                "fraction_of_proposed": round(overlap_ha / poly_ha, 6)
                if poly_ha else None,
            })
    overlap_issues = [
        dict(kind=ISSUE_OVERLAP, detail=o,
             summary=(f"overlaps same-stratum plot {o['other_plot']} "
                      f"(~{o['overlap_area_ha']:.4f} ha)"))
        for o in overlaps
    ]

    findings = [i for i in (area_issue, crs_issue) if i] \
        + tree_issues + overlap_issues

    if persist:
        for f in findings:
            FrameRevisionIssue.objects.get_or_create(
                revision=revision, kind=f["kind"], summary=f["summary"],
                defaults={"detail": f["detail"], "status": ISSUE_OPEN})

    payload = {
        "area_check": area,
        "crs": {"revision_epsg": revision.crs_epsg,
                "station_epsg": station_crs,
                "passed": crs_issue is None},
        "excluded_stems": excluded,
        "excluded_stem_count": len(excluded),
        "overlaps": overlaps,
        "open_blocking_issue_count": (
            FrameRevisionIssue.objects.filter(
                revision=revision, status=ISSUE_OPEN).count()
            if persist else len(findings)),
        "checks_passed_now": len(findings) == 0,
    }
    revision.validation_payload = payload
    return payload


# ----------------------------------------------------------- CRUD workflow
@transaction.atomic
def create_or_get_revision(plot, *, boundary, declared_area_ha, crs_epsg,
                           crs_note="", reason=""):
    """
    Upload a resurveyed boundary.

    Identical geometry + declared area + CRS -> the SAME revision is
    returned (``created=False``); nothing is duplicated. Otherwise a new
    draft edition is created and immediately validated, so blocking
    findings exist as work items from the moment of upload.
    """
    ring = canonical_ring(boundary)
    chk = content_checksum(ring, declared_area_ha, crs_epsg)
    same = PlotFrameRevision.objects.filter(
        plot=plot, content_checksum=chk).select_for_update().first()
    if same:
        return same, False

    current = (PlotFrameRevision.objects
               .filter(plot=plot, status=FRAME_PUBLISHED)
               .order_by("-revision_no").first())
    revision = PlotFrameRevision.objects.create(
        plot=plot,
        revision_no=_next_revision_no(plot),
        status=FRAME_DRAFT,
        declared_area_ha=float(declared_area_ha),
        boundary=ring,
        polygon_area_ha=0.0,
        crs_epsg=int(crs_epsg),
        crs_note=crs_note or "",
        geometry_checksum=geometry_checksum(ring),
        content_checksum=chk,
        reason=reason or "",
        supersedes=current,
    )
    validate_revision(revision)
    revision.save()
    return revision, True


@transaction.atomic
def revalidate_revision(revision):
    """Re-run geometry/area checks on a draft/reviewed edition (no rewrite
    of published rows)."""
    _assert_not_published(revision)
    payload = validate_revision(revision)
    revision.save()
    return payload


@transaction.atomic
def submit_for_review(revision, review_note):
    if revision.status != FRAME_DRAFT:
        raise FrameWorkflowError(
            f"only draft editions can be submitted for review "
            f"(current: {revision.status})")
    if not review_note or not review_note.strip():
        raise FrameWorkflowError("a review note is required at QA handover")
    # fresh checks so reviewed cannot rest on stale geometry results
    validate_revision(revision)
    revision.status = FRAME_REVIEWED
    revision.review_note = review_note.strip()
    revision.reviewed_at = timezone.now()
    revision.save()
    return revision


@transaction.atomic
def resolve_issue(issue, resolution_note):
    """
    A documented human decision acknowledges a blocking finding. The
    geometry is NOT altered here: e.g. accepting an excluded historical
    stem records that decision for audit; the observation itself is never
    moved or deleted.
    """
    if issue.status != ISSUE_OPEN:
        raise FrameWorkflowError("issue is already resolved")
    if not resolution_note or not resolution_note.strip():
        raise FrameWorkflowError(
            "a resolution note documenting the decision is required")
    issue.status = ISSUE_RESOLVED
    issue.resolution_note = resolution_note.strip()
    issue.resolved_at = timezone.now()
    issue.save()
    return issue


@transaction.atomic
def publish_revision(revision, publication_reason):
    """
    Atomically advance the sampling frame.

    * lifecycle gate: only reviewed editions, every open issue resolved;
    * row lock on the plot + DB partial-unique (one published per plot)
      guarantee two racing requests cannot both produce a published row;
    * the Plot's current boundary/area and DRAFT estimates for the plot are
      updated INSIDE this transaction — a failure anywhere rolls the whole
      thing back, so neither a half-published boundary nor wrong estimates
      can survive.
    """
    from inventory.services.estimates_run import refresh_plot_draft_estimates

    if revision.status != FRAME_REVIEWED:
        raise FrameWorkflowError(
            f"only reviewed editions can be published "
            f"(current: {revision.status}); QA review is a separate step")
    if not publication_reason or not publication_reason.strip():
        raise FrameWorkflowError(
            "a documented publication reason is mandatory")

    with transaction.atomic():
        # Serialise concurrent publishers for this plot; the partial-unique
        # constraint is the hard backstop on every backend.
        locked = (PlotFrameRevision.objects
                  .select_for_update()
                  .filter(pk=revision.pk).first())
        plot = Plot.objects.select_for_update().get(pk=revision.plot_id)

        # Re-validate: geometry findings raised after review are NOT
        # silently recomputed away — they open new blocking issues.
        validate_revision(locked)
        locked.refresh_from_db()
        open_issues = list(locked.issues.filter(status=ISSUE_OPEN))
        if open_issues:
            raise FrameBlockedError(open_issues)

        newer = (PlotFrameRevision.objects
                 .filter(plot_id=locked.plot_id, status=FRAME_PUBLISHED)
                 .order_by("-revision_no").first())
        locked.supersedes = newer
        locked.status = FRAME_PUBLISHED
        locked.publication_reason = publication_reason.strip()
        locked.published_at = timezone.now()
        # Retire the previously-current published edition FIRST so the
        # partial-unique index (one published per plot) accepts the new row.
        # Its content is untouched: history remains fully readable and
        # confirmed estimates stay bound to it.
        if newer is not None:
            newer.status = FRAME_SUPERSEDED
            newer.save(_frame_system=True)
        try:
            locked.save()
        except IntegrityError as exc:  # race: someone published first
            raise FrameWorkflowError(
                "another frame edition for this plot was published first; "
                "only one published edition can exist at a time"
            ) from exc

        # Advance the CURRENT frame used by ingest and the map. Historical
        # polygons on older editions and on measurements are untouched.
        plot.boundary = [list(p) for p in locked.boundary]
        plot.declared_area_ha = locked.declared_area_ha
        plot.area_polygon_ha = locked.polygon_area_ha
        plot.x_m, plot.y_m = _ring_centre(locked.boundary)
        plot.save()

        # Every draft estimate for the plot is re-expanded against the new
        # edition in the SAME transaction. Confirmed editions are frozen and
        # deliberately left bound to the frames they were run on.
        frames = _current_frame_map_after_publish(plot, locked)
        refreshed = refresh_plot_draft_estimates(plot, locked, frames=frames)

    revision.refresh_from_db()
    return revision, refreshed


def _assert_not_published(revision):
    if revision.status == FRAME_PUBLISHED:
        raise FrameWorkflowError(
            f"frame v{revision.revision_no} is published and immutable")


def _ring_centre(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def _current_frame_map_after_publish(plot, newly_published):
    """
    Frame map as it must be seen INSIDE the publication transaction: the
    just-published edition for this plot, latest published for the others.
    """
    frames = {}
    for other in Plot.objects.select_related("stratum"):
        if other.id == plot.id:
            frames[other.code] = newly_published
        else:
            frames[other.code] = latest_published_frame(other)
    return frames


# ------------------------------------------------------------ compare/impact
def compare_frames(revision):
    """
    Old (currently published) vs proposed/new edition: boundary rings,
    declared vs polygon areas, CRS and the per-stem classification used by
    the map and the detail page toggle.
    """
    old = (PlotFrameRevision.objects
           .filter(plot_id=revision.plot_id, status=FRAME_PUBLISHED)
           .order_by("-revision_no").first())
    if old is None and revision.status == FRAME_PUBLISHED:
        old = (PlotFrameRevision.objects
               .filter(plot_id=revision.plot_id)
               .exclude(pk=revision.pk)
               .order_by("-revision_no").first())
    impact = frame_impact(revision, old=old)
    return {
        "plot": revision.plot.code,
        "old": None if old is None else {
            "revision": old.id,
            "revision_no": old.revision_no,
            "boundary": old.boundary,
            "declared_area_ha": old.declared_area_ha,
            "polygon_area_ha": old.polygon_area_ha,
            "crs_epsg": old.crs_epsg,
            "published_at": old.published_at,
        },
        "new": {
            "revision": revision.id,
            "revision_no": revision.revision_no,
            "status": revision.status,
            "boundary": revision.boundary,
            "declared_area_ha": revision.declared_area_ha,
            "polygon_area_ha": revision.polygon_area_ha,
            "crs_epsg": revision.crs_epsg,
        },
        "area_change_ha": round(
            revision.declared_area_ha
            - (old.declared_area_ha if old else revision.declared_area_ha), 6),
        "polygon_area_change_ha": round(
            revision.polygon_area_ha
            - (old.polygon_area_ha if old else revision.polygon_area_ha), 6),
        "geometry_identical": (
            old is not None
            and old.geometry_checksum == revision.geometry_checksum),
        **impact,
    }


def frame_impact(revision, old=None):
    """
    Classify every historical stem of the plot under the proposed ring and
    report the observations it excludes. Read-only: measurements are never
    modified.
    """
    if old is None:
        old = (PlotFrameRevision.objects
               .filter(plot_id=revision.plot_id, status=FRAME_PUBLISHED)
               .order_by("-revision_no").first())
    ring = canonical_ring(revision.boundary)
    old_ring = old.boundary if old else None

    inside, excluded, newly_inside = [], [], []
    for m in _existing_stem_rows(revision.plot):
        in_new = point_in_ring(m.x_m, m.y_m, ring)
        in_old = (point_in_ring(m.x_m, m.y_m, old_ring)
                  if old_ring is not None else True)
        row = {
            "measurement_id": m.id,
            "tree_id": m.tree_id,
            "tree": f"{m.tree.plot.code}/"
                    f"{m.tree.current_field_number}",
            "field_number_seen": m.field_number_seen,
            "campaign": m.campaign.code,
            "x_m": m.x_m, "y_m": m.y_m,
            "collected_frame_revision": (
                m.collected_frame.revision_no
                if m.collected_frame_id else None),
            "inside_old": in_old, "inside_new": in_new,
        }
        if in_new:
            inside.append(row)
            if not in_old:
                newly_inside.append(row)
        else:
            excluded.append(row)

    return {
        "affected": {
            "excluded_count": len(excluded),
            "newly_inside_count": len(newly_inside),
            "excluded": excluded,
            "newly_inside": newly_inside,
            "inside_count": len(inside),
        },
        "open_blocking_issues": [
            {"id": i.id, "kind": i.kind, "summary": i.summary,
             "detail": i.detail}
            for i in revision.issues.filter(status=ISSUE_OPEN)
        ],
        "resolved_issues": [
            {"id": i.id, "kind": i.kind, "summary": i.summary,
             "resolution_note": i.resolution_note}
            for i in revision.issues.filter(status=ISSUE_RESOLVED)
        ],
        "validation": revision.validation_payload,
        "publishable": revision.status == FRAME_REVIEWED and not any(
            revision.issues.filter(status=ISSUE_OPEN)),
    }
