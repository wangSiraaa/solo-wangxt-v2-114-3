"""
Running and refreshing DRAFT population estimates.

An EstimateVersion is ALWAYS bound to explicit sampling-frame editions
(``EstimateVersion.frames`` + the frame block of ``design_snapshot``). The
per-hectare expansion therefore uses each plot's bound edition area, not a
mutable Plot row.

Two entry points:

* :func:`run_draft_estimate` — create a new draft bound to the currently
  published frame edition of every plot;
* :func:`refresh_plot_draft_estimates` — when a frame edition is published,
  every existing DRAFT estimate for that plot is re-expanded against the new
  edition IN THE SAME TRANSACTION as publication. Confirmed editions are
  frozen and intentionally left untouched.
"""
from django.conf import settings
from django.db import transaction

from inventory.models import (
    AllometricEquation,
    EstimateVersion,
    Plot,
    PlotFrameRevision,
    VERSION_CONFIRMED,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)
from inventory.services.frames import latest_published_frame


def _published_frame_map():
    """The frame edition an estimate is bound to: latest published per plot."""
    return {p.code: latest_published_frame(p)
            for p in Plot.objects.select_related("stratum")}


def _frame_snapshot(frames):
    return {
        code: {
            "frame_id": f.id,
            "revision_no": f.revision_no,
            "status": f.status,
            "declared_area_ha": f.declared_area_ha,
            "polygon_area_ha": f.polygon_area_ha,
            "crs_epsg": f.crs_epsg,
        }
        for code, f in sorted(frames.items())
    }


def run_draft_estimate(*, label, t1, t2, equations_qs, fpc=True,
                       frames=None, commit=True):
    """
    Build and (optionally) persist a DRAFT estimate. The estimate is bound
    to the published frame edition of every plot.
    """
    frames = frames if frames is not None else _published_frame_map()
    if commit:
        return _run_and_persist(label=label, t1=t1, t2=t2,
                                equations_qs=equations_qs, fpc=fpc,
                                frames=frames)
    return _compute(label=label, t1=t1, t2=t2, equations_qs=equations_qs,
                    fpc=fpc, frames=frames)


def _compute(*, label, t1, t2, equations_qs, fpc, frames):
    table_t1, table_t2, equations, plots, strata = build_measurement_table(
        t1, t2, equations_qs, frames=frames)
    uncovered = sorted({
        r["species"] for r in table_t1 + table_t2
        if r["species"] not in equations
    })
    renumber, distinct = resolved_identity_pairs(t1, t2)

    interval = round((t2.measured_on - t1.measured_on).days / 365.25, 3)
    design = {
        "t1_code": t1.code, "t2_code": t2.code,
        "interval_years": interval,
        "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
        "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
        "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
        "recruitment_cm": settings.RECRUITMENT_DBH_CM,
        "fpc": bool(fpc),
        "crs_epsg": settings.SURVEY_CRS_EPSG,
    }
    result = estimate(table_t1, table_t2, equations, plots, strata, design,
                      resolved_renumber_pairs=renumber,
                      resolved_distinct_pairs=distinct)
    result["species_without_equation"] = uncovered
    result["frame_binding"] = {
        code: {"revision_no": f.revision_no, "status": f.status,
               "declared_area_ha": f.declared_area_ha}
        for code, f in sorted(frames.items())
    }
    checksum = equation_checksum(equations)

    snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                   for code, s in strata.items()}
    eq_ids = sorted(e.id for e in equations_qs)
    design_snapshot = {
        **design,
        "strata": snap_strata,
        "equation_ids": eq_ids,
        "equation_codes": {sp: e["code"] + "@" + e["version"]
                           for sp, e in equations.items()},
        "area_tolerance": settings.PLOT_AREA_TOLERANCE,
        # Explicit frame binding travels inside the frozen snapshot too.
        "frames": _frame_snapshot(frames),
    }
    return result, design_snapshot, checksum


@transaction.atomic
def _run_and_persist(*, label, t1, t2, equations_qs, fpc, frames):
    result, design_snapshot, checksum = _compute(
        label=label, t1=t1, t2=t2, equations_qs=equations_qs, fpc=fpc,
        frames=frames)
    version = EstimateVersion.objects.create(
        label=label, t1_campaign=t1, t2_campaign=t2,
        design_snapshot=design_snapshot,
        result_payload=result, equation_checksum=checksum,
    )
    version.equations.set(equations_qs)
    version.frames.set(frames.values())
    return version


def _equations_for_version(version):
    return list(
        AllometricEquation.objects
        .filter(id__in=version.design_snapshot.get("equation_ids", []))
        .prefetch_related("species")
    )


def refresh_plot_draft_estimates(plot, new_frame, frames=None):
    """
    Re-expand every DRAFT estimate against ``new_frame`` for ``plot``.

    Called INSIDE the frame-publication transaction: if any recompute
    raises, the publication rolls back too — no half-published boundary can
    ever coexist with estimates expanded on the wrong area.

    Returns a list of refreshed estimate ids. Confirmed editions are never
    touched (they are frozen against their originally bound frames).
    """
    if frames is None:
        frames = _published_frame_map()
    refreshed = []
    drafts = (EstimateVersion.objects
              .filter(frames__plot=plot)
              .exclude(status=VERSION_CONFIRMED)
              .distinct())
    for version in drafts:
        t1, t2 = version.t1_campaign, version.t2_campaign
        equations_qs = _equations_for_version(version)
        result, design_snapshot, checksum = _compute(
            label=version.label, t1=t1, t2=t2, equations_qs=equations_qs,
            fpc=version.design_snapshot.get("fpc", True), frames=frames)
        version.result_payload = result
        version.design_snapshot = design_snapshot
        version.equation_checksum = checksum
        version.save()
        version.equations.set(equations_qs)
        version.frames.set(frames.values())
        refreshed.append(version.id)
    return refreshed


def assert_version_frames_current(version):
    """
    409-grade guard: a draft must not be confirmed against a frame edition
    that has since been superseded. (Publication refreshes drafts in the
    same transaction, so under normal flow the binding is already current;
    this is the belt-and-braces check.)
    """
    bound = {f.plot_id: f for f in version.frames.select_related("plot")}
    for frame in bound.values():
        latest = (PlotFrameRevision.objects
                  .filter(plot_id=frame.plot_id, status="published")
                  .order_by("-revision_no").first())
        if latest is None or latest.id != frame.id:
            return False
    return True
