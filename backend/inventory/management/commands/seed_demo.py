"""
Seed FICTIONAL demonstration data for the permanent-plot station.

The dataset is deliberately constructed to exercise every acceptance case:
  * remeasurement with real growth;
  * a VERIFIED zero-growth tree (same dbh, re-measured);
  * alive-but-not-measured (missing data, imputed, never called zero);
  * mortality (dead observation) and a tree not located;
  * ingrowth above recruitment + a sub-recruitment stem (excluded);
  * a verified renumber (same tree row, new tag);
  * a SAME-NUMBER / position contradiction (left open -> excluded);
  * a likely renumber with different label (left open -> excluded);
  * unequal plot areas and stratified expansion;
  * a deliberately bad unit row (mm entered as cm) rejected at ingest;
  * one stem above the equation dbh range (flagged extrapolation).

Coordinates are fictional UTM zone 50N metres (EPSG:32650).
All trees, species and equations are invented for demonstration.
"""
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import (
    AllometricEquation,
    Campaign,
    Plot,
    Species,
    Stratum,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.ingest import import_campaign_rows, verify_plot_area


def rect(ox, oy, w, d):
    """Rectangle ring from origin [m]."""
    return [
        [ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d], [ox, oy]
    ]


# (origin x, origin y, width m, depth m, declared area ha)
PLOTS = {
    "P01": dict(stratum="A", ox=500000, oy=4000000, w=100, d=50, area=0.50),
    "P02": dict(stratum="A", ox=500400, oy=4000000, w=70.71, d=70.71,
                area=0.50),
    "P03": dict(stratum="A", ox=500000, oy=4000500, w=50, d=40, area=0.20),
    "P04": dict(stratum="B", ox=501000, oy=4000000, w=100, d=100, area=1.00),
    "P05": dict(stratum="B", ox=501500, oy=4000000, w=100, d=100, area=1.00),
}

# number, species, (x offset, y offset), t1 tuple or None, t2 tuple or None
# tuple = (status, dbh, height); dbh None when not measured.
AM = "alive_measured"
AN = "alive_not_measured"
DE = "dead"
MI = "missing_tree"

P01_TREES = [
    ("001", "OAK", (10, 10), (AM, 24.0, 16.0), (AM, 25.2, 16.8)),
    ("002", "OAK", (25, 12), (AM, 30.5, 19.0), (AM, 31.4, 19.4)),
    ("003", "PIN", (40, 18), (AM, 35.0, 22.0), (AM, 36.9, 23.0)),
    # VERIFIED zero growth: same dbh cross-checked in the field notes.
    ("004", "OAK", (55, 20), (AM, 18.0, 13.5), (AM, 18.0, 13.5)),
    # alive at t2 but crew's dbh tape failed: MISSING measurement, not zero.
    ("005", "BIR", (70, 25), (AM, 12.0, 10.0), (AN, None, None)),
    ("006", "PIN", (85, 30), (AM, 40.0, 24.0), (DE, None, None)),
    # renumbered on the same tree row (tag replaced); position identical.
    ("007", "OAK", (20, 40), (AM, 22.0, 15.0), (AM, 22.9, 15.5)),
    # SAME NUMBER 008 at t2 but 25 m away -> open conflict, excluded.
    ("008", "PIN", (15, 30), (AM, 27.0, 18.0), (AM, 28.0, 18.5)),
    # 009 dies; new stem 009 appears at a different spot -> distinct case
    # left open in the seed (resolve via API to see mortality + ingrowth).
    ("009", "OAK", (60, 35), (AM, 16.0, 12.5), (DE, None, None)),
    # below recruitment threshold at t2 -> recorded, excluded from ingrowth
    ("201", "BIR", (90, 40), None, (AM, 4.2, 4.5)),
    # ingrowth
    ("202", "BIR", (92, 15), None, (AM, 7.6, 7.0)),
]

P01_CONFLICT_T2 = [
    # field label reused 008 at a contradictory position, new tree row
    dict(plot="P01", field_number="008", species="PIN",
         x_m=500040, y_m=4000030,
         status=AM, dbh_raw=20.0, dbh_unit="cm",
         height_raw=14.0, height_unit="m"),
    # new stem 009 far from the dead 009
    dict(plot="P01", field_number="009", species="BIR",
         x_m=500062, y_m=4000010,
         status=AM, dbh_raw=9.0, dbh_unit="cm",
         height_raw=8.0, height_unit="m"),
]
# label 007 is renamed 017 at t2 on the SAME tree row:
P01_RENUMBER = {"007": "017"}

P02_TREES = [
    ("001", "OAK", (12, 14), (AM, 26.0, 17.0), (AM, 27.1, 17.5)),
    ("002", "PIN", (30, 20), (AM, 33.0, 21.0), (AM, 34.4, 21.8)),
    ("003", "BIR", (45, 33), (AM, 10.0, 9.0), (AN, None, None)),
    ("004", "OAK", (55, 40), (AM, 21.0, 14.5), (AM, 21.8, 15.0)),
    ("005", "PIN", (60, 55), (AM, 28.0, 18.5), (MI, None, None)),
    ("006", "BIR", (20, 50), (AM, 14.0, 11.0), (AM, 15.1, 11.6)),
    # close geometry, DIFFERENT label 118 -> "possible renumber" conflict
    ("117", "OAK", (40, 60), (AM, 19.0, 13.0), None),
    ("118", "OAK", (40.4, 60.2), None, (AM, 19.7, 13.3)),
    ("201", "BIR", (65, 30), None, (AM, 6.4, 6.2)),
]

P03_TREES = [
    ("001", "PIN", (10, 10), (AM, 32.0, 20.0), (AM, 33.5, 20.7)),
    ("002", "OAK", (20, 15), (AM, 23.0, 15.5), (AM, 23.0, 15.5)),
    ("003", "BIR", (30, 20), (AM, 11.0, 9.5), (AM, 11.9, 10.0)),
    ("004", "PIN", (40, 28), (AM, 26.0, 17.5), (DE, None, None)),
]

P04_TREES = [
    ("001", "OAK", (15, 20), (AM, 45.0, 25.0), (AM, 46.5, 25.5)),
    # outside the equation dbh range -> extrapolation flag
    ("002", "OAK", (35, 40), (AM, 102.0, 34.0), (AM, 103.5, 34.5)),
    ("003", "PIN", (55, 30), (AM, 38.0, 23.0), (AM, 39.6, 23.7)),
    ("004", "BIR", (70, 60), (AM, 13.0, 10.5), (AM, 13.9, 11.0)),
    ("005", "PIN", (80, 80), (AM, 29.0, 19.0), (DE, None, None)),
    ("006", "OAK", (25, 85), (AM, 27.0, 17.5), (AN, None, None)),
    ("201", "BIR", (90, 20), None, (AM, 8.2, 7.6)),
]

P05_TREES = [
    ("001", "PIN", (20, 25), (AM, 31.0, 20.0), (AM, 32.6, 20.8)),
    ("002", "PIN", (40, 45), (AM, 36.0, 22.5), (AM, 37.5, 23.1)),
    ("003", "OAK", (60, 30), (AM, 24.0, 16.0), (AM, 25.0, 16.5)),
    ("004", "BIR", (75, 70), (AM, 10.0, 9.0), (AM, 10.7, 9.4)),
    ("005", "OAK", (85, 85), (AM, 20.0, 14.0), (AM, 20.0, 14.0)),
    ("006", "BIR", (30, 70), (AM, 15.0, 11.5), (MI, None, None)),
    ("201", "BIR", (50, 80), None, (AM, 5.8, 5.6)),
]

# Bad rows that ingest MUST reject:
BAD_ROWS_T2 = [
    # 250 mm entered as "250 cm": plausible-range check catches the unit mix-up
    dict(plot="P02", field_number="900", species="OAK",
         x_m=500430.0, y_m=4000040.0, status=AM,
         dbh_raw=250.0, dbh_unit="cm", height_raw=20.0, height_unit="m"),
    # height given in cm instead of m
    dict(plot="P03", field_number="901", species="BIR",
         x_m=500025.0, y_m=4000515.0, status=AM,
         dbh_raw=12.0, dbh_unit="cm", height_raw=950.0, height_unit="m"),
    # missing unit declaration
    dict(plot="P04", field_number="902", species="PIN",
         x_m=501060.0, y_m=4000060.0, status=AM,
         dbh_raw=30.0, height_raw=20.0, height_unit="m"),
    # stem outside the plot boundary
    dict(plot="P01", field_number="903", species="OAK",
         x_m=500300.0, y_m=4000200.0, status=AM,
         dbh_raw=20.0, dbh_unit="cm", height_raw=14.0, height_unit="m"),
]


class Command(BaseCommand):
    help = "Seed fictional permanent-plot data (idempential wipe + recreate)."

    @transaction.atomic
    def handle(self, *args, **options):
        from inventory.models import (
            EstimateVersion, FrameIssue, IdentityConflict,
            MeasurementImportRow, PlotFrameRevision, SamplingFrameVersion,
            Tree, TreeMeasurement,
        )
        # Dependent (revision/frame) rows first: they PROTECT plots.
        models = [EstimateVersion, FrameIssue, PlotFrameRevision,
                  SamplingFrameVersion, IdentityConflict, MeasurementImportRow,
                  TreeMeasurement, Tree, Plot, Campaign,
                  AllometricEquation, Species, Stratum]
        for m in models:
            m.objects.all().delete()

        sA = Stratum.objects.create(
            code="A", name="Upland oak–pine mosaic (fictional)", area_ha=120.0)
        sB = Stratum.objects.create(
            code="B", name="Riparian mixed broadleaf (fictional)", area_ha=85.0)

        species = {
            "OAK": Species.objects.create(
                code="OAK", name="Fictitious white oak (Quercus ficta)",
                family="Fagaceae"),
            "PIN": Species.objects.create(
                code="PIN", name="Fictitious red pine (Pinus demonstrabilis)",
                family="Pinaceae"),
            "BIR": Species.objects.create(
                code="BIR", name="Fictitious birch (Betula exemplaris)",
                family="Betulaceae"),
        }

        oak_eq = AllometricEquation.objects.create(
            code="OAK-AGB", version="1.0", status="confirmed",
            form="agb_kg = 0.1200 * dbh_cm^2.4000 * height_m^0.6000",
            a=0.1200, b=2.4000, c=0.6000,
            dbh_min_cm=5.0, dbh_max_cm=90.0, height_required=True,
            residual_sigma=0.18,
            citation="FICTIONAL demo equation, oak stratum A/B, 2021")
        pin_eq = AllometricEquation.objects.create(
            code="PIN-AGB", version="1.0", status="confirmed",
            form="agb_kg = 0.0950 * dbh_cm^2.4500 * height_m^0.6500",
            a=0.0950, b=2.4500, c=0.6500,
            dbh_min_cm=5.0, dbh_max_cm=80.0, height_required=True,
            residual_sigma=0.16,
            citation="FICTIONAL demo equation, pine, 2021")
        bir_eq = AllometricEquation.objects.create(
            code="BIR-AGB", version="2.1", status="confirmed",
            form="agb_kg = 0.1100 * dbh_cm^2.3500 * height_m^0.5500",
            a=0.1100, b=2.3500, c=0.5500,
            dbh_min_cm=5.0, dbh_max_cm=60.0, height_required=True,
            residual_sigma=0.20,
            citation="FICTIONAL demo equation, birch, revised 2022")
        oak_eq.species.set([species["OAK"]])
        pin_eq.species.set([species["PIN"]])
        bir_eq.species.set([species["BIR"]])

        t1 = Campaign.objects.create(
            code="2019", measured_on=date(2019, 7, 10),
            description="First census (fictional)")
        t2 = Campaign.objects.create(
            code="2024", measured_on=date(2024, 7, 15),
            description="Remeasurement (fictional)")

        plot_objs = {}
        for code, cfg in PLOTS.items():
            boundary = rect(cfg["ox"], cfg["oy"], cfg["w"], cfg["d"])
            plot = Plot(
                code=code, stratum=sA if cfg["stratum"] == "A" else sB,
                x_m=cfg["ox"] + cfg["w"] / 2,
                y_m=cfg["oy"] + cfg["d"] / 2,
                declared_area_ha=cfg["area"], boundary=boundary,
                area_polygon_ha=0.0)
            plot.area_polygon_ha = verify_plot_area(
                plot, settings.PLOT_AREA_TOLERANCE)
            plot.save()
            plot_objs[code] = plot

        # build rows per plot (x/y need plot origin)
        def rows_for(code, campaign_code):
            cfg = PLOTS[code]
            data = {
                "P01": P01_TREES, "P02": P02_TREES, "P03": P03_TREES,
                "P04": P04_TREES, "P05": P05_TREES,
            }[code]
            rows = []
            for num, sp, (dx, dy), v1, v2 in data:
                v = v1 if campaign_code == "2019" else v2
                if v is None:
                    continue
                shown = num
                if code == "P01" and campaign_code == "2024":
                    shown = P01_RENUMBER.get(num, num)
                rows.append(dict(
                    plot=code, field_number=shown, species=sp,
                    x_m=cfg["ox"] + dx, y_m=cfg["oy"] + dy, status=v[0],
                    dbh_raw=v[1], dbh_unit="cm" if v[1] is not None else None,
                    height_raw=v[2],
                    height_unit="m" if v[2] is not None else None,
                    notes=("verified zero growth cross-check"
                           if num == "004" and campaign_code == "2024"
                           else "")))
            return rows

        t1_rows, t2_rows = [], []
        for code in PLOTS:
            t1_rows.extend(rows_for(code, "2019"))
            r2 = rows_for(code, "2024")
            if code == "P01":
                r2.extend(P01_CONFLICT_T2)
            if code == "P02":
                r2.extend(BAD_ROWS_T2[:2])
            if code == "P03":
                r2.extend(BAD_ROWS_T2[2:3])
            if code == "P01":
                r2.extend(BAD_ROWS_T2[3:4])
            t2_rows.extend(r2)

        r1 = import_campaign_rows(t1, t1_rows,
                                  settings.PLOT_AREA_TOLERANCE)

        # Verified field-book renumber P01/007 -> P01/017 (tag replaced,
        # same individual): relabel the existing tree row BEFORE importing
        # its t2 measurement, so identity is the internal tree row.
        from inventory.models import Tree as _Tree
        old007 = _Tree.objects.get(plot__code="P01",
                                   current_field_number="007")
        old007.current_field_number = "017"
        old007.save(update_fields=["current_field_number"])

        r2 = import_campaign_rows(t2, t2_rows,
                                  settings.PLOT_AREA_TOLERANCE)
        self.stdout.write(
            f"t1 accepted={r1['n_accepted']} rejected={r1['n_rejected']}")
        self.stdout.write(
            f"t2 accepted={r2['n_accepted']} rejected={r2['n_rejected']}")
        for bad in r2["rejected"]:
            self.stdout.write("  REJECTED: " + bad["reason"])

        found = scan_conflicts(t1, t2)
        self.stdout.write(f"identity conflicts found: {len(found)}")
        for f in found:
            self.stdout.write(
                f"  {f['plot']}/{f['field_number']} -> "
                f"{f.get('t2_field_number')} d={f['distance_m']}m "
                f"[{f['hint']}]")

        # ---- sampling-frame revision demo scenarios ---------------------
        # P03 re-survey cleanly expands 0.20 -> 0.25 ha (every historical
        # stem stays inside): drafts -> reviewed -> PUBLISHED as frame v2.
        # P01's proposed boundary clips away its east strip and excludes
        # historical stems (005/006/201/202): it stays BLOCKED with pending
        # items and no frame version is emitted.
        from inventory.services import frame as frame_service
        p03 = plot_objs["P03"]
        p03_ring = rect(PLOTS["P03"]["ox"], PLOTS["P03"]["oy"], 50, 50)
        rev03, created03 = frame_service.create_revision(
            p03, p03_ring, declared_area_ha=0.25,
            crs_epsg=settings.SURVEY_CRS_EPSG,
            tolerance=settings.PLOT_AREA_TOLERANCE,
            crs_note="FICTIONAL 2026 differential-GPS re-survey",
            reason="north boundary monument relocated; area 0.20->0.25 ha")
        if rev03.status == "draft":
            frame_service.review_revision(rev03)
            rev03, f03 = frame_service.publish_revision(
                rev03, reason="north boundary monument relocated; "
                              "area 0.20->0.25 ha")
            self.stdout.write(
                f"frame revision P03 #{rev03.revision_no} published -> "
                f"frame v{f03.version}")
        else:
            self.stdout.write(
                f"P03 revision blocked: "
                f"{[i.kind for i in rev03.issues.filter(status='open')]}")

        p01 = plot_objs["P01"]
        p01_ring = rect(PLOTS["P01"]["ox"], PLOTS["P01"]["oy"], 70, 50)
        rev01, _ = frame_service.create_revision(
            p01, p01_ring, declared_area_ha=0.35,
            crs_epsg=settings.SURVEY_CRS_EPSG,
            tolerance=settings.PLOT_AREA_TOLERANCE,
            crs_note="FICTIONAL 2026 disputed east boundary",
            reason="proposed east trim (rejected: historical stems excluded)")
        self.stdout.write(
            f"frame revision P01 #{rev01.revision_no} [{rev01.status}]: "
            f"{[i.kind for i in rev01.issues.filter(status='open')]}")

        self.stdout.write(self.style.SUCCESS("seed complete"))
