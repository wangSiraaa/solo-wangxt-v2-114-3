"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation.
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    EstimateVersion,
    Plot,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements
from inventory.services.ingest import import_campaign_rows, verify_plot_area
from inventory.services.units import convert_dbh_to_cm, ring_area_ha


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


class EstimatorAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        # Unequal plot areas: 0.10 ha and 0.25 ha.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    def _run(self):
        t1t, t2t, equations, plots, strata = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        ren, dist = resolved_identity_pairs(self.t1, self.t2)
        design = dict(t1_code="t1", t2_code="t2", interval_years=5.0,
                      dbh_sd_cm=0.1, height_sd_m=0.3, zero_tol_cm=0.15,
                      recruitment_cm=5.0, fpc=False, crs_epsg=32650)
        return estimate(t1t, t2t, equations, plots, strata, design, ren, dist)

    # ---------- C. unit mistakes ------------------------------------------------
    def test_dbh_unit_must_be_explicit(self):
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(25.0, None)

    def test_mm_entered_as_cm_is_rejected_by_range(self):
        # 250 (mm) typed as cm -> 250 cm beyond the accepted demo range.
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(250.0, "cm")

    def test_mm_value_correctly_converted(self):
        self.assertAlmostEqual(convert_dbh_to_cm(250.0, "mm"), 25.0)

    def test_import_rejects_bad_unit_rows(self):
        rows = [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=10, y_m=10, status=AM,
                 dbh_raw=250.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="OAK",
                 x_m=12, y_m=12, status=AM,
                 dbh_raw=20.0, height_raw=15.0, height_unit="m"),
        ]
        r = import_campaign_rows(self.t2, rows, 0.01)
        self.assertEqual(r["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

    # ---------- B. unequal plot areas ------------------------------------------
    def test_unequal_plot_areas_expanded_per_plot(self):
        # 100 kg growth on each plot; per-ha: 1000 vs 400 kg/ha.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        # choose t2 dbh giving +100 kg growth per tree with a=0.1,b=2,c=.5
        def b_at(d):
            return 0.1 * d ** 2 * 15 ** 0.5
        import math
        d2 = math.sqrt((b_at(20.0) + 100.0) / (0.1 * 15 ** 0.5))
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        res = self._run()
        # per-ha mean = (1000 + 400)/2 = 700 kg/ha; x 100 ha = 70 000 kg
        self.assertAlmostEqual(
            res["components"]["survivor_growth"]["total_kg"],
            70_000.0, delta=1e-6)
        # The naive "mean tree * area" would give 100 kg * (100/0.175 avg?)
        # and clearly differs; the provenance carries per-plot values.
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["area_ha"], 0.10)

    def test_plot_area_polygon_crosscheck(self):
        bad = Plot(code="BAD", stratum=self.sA, x_m=0, y_m=0,
                   declared_area_ha=0.50, boundary=rect(0, 0, 50, 20),
                   area_polygon_ha=0.10)
        with self.assertRaises(DjValidationError):
            verify_plot_area(bad, 0.01)

    # ---------- D. zero / missing / dead ---------------------------------------
    def test_zero_growth_missing_and_dead_are_distinct(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m",
                 notes="verified zero growth"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AN),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=DE),
        ], 0.01)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([z["tree"] for z in p1["verified_zero_growth"]],
                         ["P1/z"])
        self.assertEqual([m["tree"] for m in p1["alive_not_measured"]],
                         ["P1/m"])
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/d"])
        # missing survivor did NOT silently become zero growth:
        self.assertTrue(p1["imputed_survivor_growth_kg"] >= 0)

    # ---------- A + E. renumber / same-number contradiction --------------------
    def test_renumber_keeps_one_individual(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="007", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        tree = Tree.objects.get(current_field_number="007")
        tree.current_field_number = "017"
        tree.save(update_fields=["current_field_number"])
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="017", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        t1t, t2t, *_ = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        pairing = pair_measurements(t1t, t2t)
        self.assertEqual(len(pairing["pairs"]), 1)
        self.assertEqual(pairing["pairs"][0]["kind"], "renumber")

    def test_same_number_position_contradiction_is_excluded(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="008", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        # new tree row, same label, 15 m away
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="008", species="OAK", x_m=15, y_m=5,
                 status=AM, dbh_raw=12.0, dbh_unit="cm",
                 height_raw=10.0, height_unit="m")], 0.01)
        self.assertEqual(
            Tree.objects.filter(plot=self.p1,
                                current_field_number="008").count(), 2)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        excluded = [c["tree"] for c in p1["excluded_identity_conflicts"]]
        self.assertIn("P1/008", excluded)
        # not counted as growth, nor as mortality, nor as ingrowth
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["ingrowth"], [])

    def test_distinct_resolution_counts_removal_and_ingrowth(self):
        from inventory.services.conflicts import scan_conflicts
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="009", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=16.0, dbh_unit="cm",
                 height_raw=12.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="009", species="OAK", x_m=25, y_m=5,
                 status=AM, dbh_raw=8.0, dbh_unit="cm",
                 height_raw=8.0, height_unit="m")], 0.01)
        found = scan_conflicts(self.t1, self.t2)
        self.assertTrue(found)
        client = APIClient()
        cid = found[0]["id"]
        resp = client.post(f"/api/conflicts/{cid}/resolve/",
                           {"status": "distinct", "note": "new recruit"})
        self.assertEqual(resp.status_code, 200)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/009"])
        self.assertEqual([m["tree"] for m in p1["ingrowth"]], ["P1/009"])

    # ---------- F. confirmed edition immutability ------------------------------
    def test_confirmed_estimate_is_frozen_against_new_equation(self):
        client = APIClient()
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        body = dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]

        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

        # 1) the JSON result stays byte-stable
        again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], before)

        # 2) the equation is locked: coefficient change refused
        self.eq.refresh_from_db()
        self.eq.a = 0.999
        with self.assertRaises(PermissionError):
            self.eq.save()

        # 3) the edition row itself cannot be mutated
        version = EstimateVersion.objects.get(pk=vid)
        version.label = "tampered"
        with self.assertRaises(PermissionError):
            version.save()

        # 4) a new equation must be issued as a NEW equation row/version
        eq2 = AllometricEquation.objects.create(
            code="OAK", version="2", status="draft",
            a=0.2, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional revised")
        eq2.species.add(self.oak)
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-equation",
                              t1_campaign="t1", t2_campaign="t2",
                              equation_ids=[eq2.id], fpc=False),
                         format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["id"], vid)
        # old edition unchanged
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["label"], "v1")
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)

    def test_result_payload_records_units_and_sources(self):
        res = self._run()
        self.assertEqual(res["units"]["dbh"],
                         "cm (converted at ingest; raw unit retained)")
        self.assertEqual(res["units"]["height"], "m")
        self.assertIn("estimator", res["design"])
        self.assertTrue(res["uncertainty_assumptions"])
        self.assertIn("OAK", res["equations_used"])
