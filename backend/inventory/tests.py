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


# ---------------------------------------------------------------------------
# Sampling-frame revision acceptance tests
# ---------------------------------------------------------------------------
import tempfile  # noqa: E402

from inventory.models import (  # noqa: E402
    FRAME_DRAFT,
    FRAME_PUBLISHED,
    FRAME_REVIEWED,
    ISSUE_AREA_MISMATCH,
    ISSUE_EXCLUDED_TREE,
    ISSUE_OPEN,
    ISSUE_OVERLAP,
    FrameRevisionIssue,
    PlotFrameRevision,
)
from inventory.services.frames import (  # noqa: E402
    create_or_get_revision,
    latest_published_frame,
    publish_revision,
    submit_for_review,
)


def _tree_rows(plot):
    return list(
        TreeMeasurement.objects
        .filter(tree__plot=plot)
        .select_related("tree", "campaign", "collected_frame")
        .order_by("id"))


class FrameRevisionAcceptanceTests(TestCase):
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
        # 0.10 ha plot 50x20 with one survivor near the west edge.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=25, y_m=10,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)

    def _confirmed_and_draft(self, new_area):
        """Estimate confirmed on the 0.10 ha frame, then a fresh draft."""
        client = APIClient()
        r = client.post("/api/estimates/",
                        dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                             equation_ids=[self.eq.id], fpc=False),
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]
        self.assertEqual(r.json()["frames"][0]["revision_no"], 1)
        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)
        return client, vid, before

    # 1) eligible area revision: only NEW drafts change per-ha expansion
    def test_area_revision_only_changes_new_draft_expansion(self):
        import math
        client, vid, before = self._confirmed_and_draft(0.20)

        # resurvey: polygon corrected to 100x20 = 0.20 ha, declared matches
        rev, created = create_or_get_revision(
            self.p1, boundary=rect(0, 0, 100, 20),
            declared_area_ha=0.20, crs_epsg=32650,
            reason="boundary resurvey 2025")
        self.assertTrue(created)
        self.assertEqual(rev.status, FRAME_DRAFT)
        self.assertFalse(rev.has_open_blocking_issues)
        rev = submit_for_review(rev, "QA: resurveyed with total station")
        self.assertEqual(rev.status, FRAME_REVIEWED)
        rev, refreshed = publish_revision(rev, "registered boundary correction")
        self.assertEqual(rev.status, FRAME_PUBLISHED)
        self.assertFalse(refreshed, "no drafts existed to refresh")

        # old confirmed edition is byte-identical
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)
        prov = old["result_payload"]["provenance"]["plots"][0]
        self.assertEqual(prov["area_ha"], 0.10)
        self.assertEqual(prov["frame_revision_no"], 1)

        # new draft expands with 0.20 ha: per-ha value halves, total halves
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-frame", t1_campaign="t1",
                              t2_campaign="t2", equation_ids=[self.eq.id],
                              fpc=False), format="json")
        self.assertEqual(r2.status_code, 201, r2.content)
        after = r2.json()["result_payload"]["components"]["survivor_growth"]
        self.assertAlmostEqual(
            after["total_kg"], before["total_kg"] * 0.5, delta=1e-6)
        self.assertEqual(r2.json()["frames"][0]["revision_no"], rev.revision_no)
        prov2 = r2.json()["result_payload"]["provenance"]["plots"][0]
        self.assertEqual(prov2["area_ha"], 0.20)
        self.assertEqual(prov2["frame_revision_no"], rev.revision_no)
        # old confirmed still unchanged
        old_again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            old_again["result_payload"]["components"]["survivor_growth"],
            before)

    # 2) revision excluding a historical stem: blocked, data untouched
    def test_revision_excluding_historical_stem_is_blocked(self):
        client = APIClient()
        # shift boundary east so the tree at x=5 falls outside
        r = client.post("/api/frame-revisions/",
                        dict(plot="P1", boundary=rect(20, 0, 50, 20),
                             declared_area_ha=0.10, crs_epsg=32650,
                             reason="eastern strip only"),
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)
        rev_id = r.json()["id"]
        kinds = {i["kind"] for i in r.json()["issues"]}
        self.assertIn(ISSUE_EXCLUDED_TREE, kinds)
        self.assertTrue(any(i["status"] == ISSUE_OPEN
                            for i in r.json()["issues"]))

        impact = client.get(f"/api/frame-revisions/{rev_id}/impact/").json()
        self.assertEqual(impact["affected"]["excluded_count"], 2)
        self.assertEqual(
            {e["tree"] for e in impact["affected"]["excluded"]}, {"P1/1"})

        # draft -> review is allowed for QA, but publication is blocked
        rr = client.post(f"/api/frame-revisions/{rev_id}/submit_review/",
                         {"review_note": "checked by GIS desk"}, format="json")
        self.assertEqual(rr.status_code, 200, rr.content)
        pr = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                         {"publication_reason": "try anyway"}, format="json")
        self.assertEqual(pr.status_code, 422)
        self.assertIn("blocking_issues", pr.json())

        # nothing happened to historical measurements or the current frame
        meas = _tree_rows(self.p1)
        self.assertEqual(len(meas), 2)
        self.assertTrue(all(m.x_m == 5 and m.y_m == 5 for m in meas))
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.declared_area_ha, 0.10)
        self.assertEqual(self.p1.boundary, rect(0, 0, 50, 20))
        self.assertEqual(latest_published_frame(self.p1).revision_no, 1)

        # a human documents the decision per issue, THEN publish proceeds
        for issue in FrameRevisionIssue.objects.filter(
                revision_id=rev_id, status=ISSUE_OPEN):
            ir = client.post(f"/api/frame-issues/{issue.id}/resolve/",
                             {"resolution_note":
                              "excluded observation retained on frame v1; "
                              "accepted by chief surveyor J. Doe"},
                             format="json")
            self.assertEqual(ir.status_code, 200, ir.content)
        pr2 = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                          {"publication_reason":
                           "documented boundary correction"}, format="json")
        self.assertEqual(pr2.status_code, 200, pr2.content)
        # the historical measurements STILL sit at their original position,
        # attributed to frame v1 — not migrated, deleted or rewritten
        for m in _tree_rows(self.p1):
            m.refresh_from_db()
            self.assertEqual(m.collected_frame.revision_no, 1)
            self.assertEqual((m.x_m, m.y_m), (5, 5))

    # 2b) area beyond tolerance raises its own blocking issue
    def test_area_beyond_tolerance_blocks_publication(self):
        rev, _ = create_or_get_revision(
            self.p1, boundary=rect(0, 0, 50, 20),
            declared_area_ha=0.12, crs_epsg=32650)
        self.assertTrue(rev.issues.filter(
            kind=ISSUE_AREA_MISMATCH, status=ISSUE_OPEN).exists())
        rev = submit_for_review(rev, "qa")
        with self.assertRaises(Exception):
            publish_revision(rev, "no")

    # 2c) overlap with another same-stratum plot blocks publication
    def test_same_stratum_overlap_blocks_publication(self):
        Plot.objects.create(
            code="P2", stratum=self.sA, x_m=120, y_m=10,
            declared_area_ha=0.10, boundary=rect(100, 0, 50, 20),
            area_polygon_ha=0.10)
        rev, _ = create_or_get_revision(
            self.p1, boundary=rect(0, 0, 140, 20),
            declared_area_ha=0.28, crs_epsg=32650)
        self.assertTrue(rev.issues.filter(
            kind=ISSUE_OVERLAP, status=ISSUE_OPEN).exists())

    # 3) identical geometry upload is idempotent
    def test_identical_geometry_upload_is_idempotent(self):
        client = APIClient()
        body = dict(plot="P1", boundary=rect(0, 0, 50, 20),
                    declared_area_ha=0.10, crs_epsg=32650,
                    reason="same ring")
        # identical to the historical baseline frame -> that SAME edition is
        # returned idempotently (200), no duplicate row
        r1 = client.post("/api/frame-revisions/", body, format="json")
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual(r1.json()["revision_no"], 1)
        r2 = client.post("/api/frame-revisions/", body, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(PlotFrameRevision.objects.filter(
            plot=self.p1).count(), 1)  # just the baseline

        # genuinely new geometry creates a NEW draft edition ...
        r3 = client.post("/api/frame-revisions/",
                         {**body, "boundary": rect(0, 0, 60, 20),
                          "declared_area_ha": 0.12}, format="json")
        self.assertEqual(r3.status_code, 201)
        # ... and re-uploading it returns the same revision
        r4 = client.post("/api/frame-revisions/",
                         {**body, "boundary": rect(0, 0, 60, 20),
                          "declared_area_ha": 0.12}, format="json")
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r3.json()["id"], r4.json()["id"])

        # same ring but a CORRECTED declared area is a distinct edition
        r5 = client.post("/api/frame-revisions/",
                         {**body, "boundary": rect(0, 0, 50, 20),
                          "declared_area_ha": 0.1005}, format="json")
        self.assertEqual(r5.status_code, 201)
        self.assertNotEqual(r5.json()["id"], r1.json()["id"])

    # 3b) lifecycle gates via API
    def test_publish_requires_review_and_reason(self):
        client = APIClient()
        # new geometry within area tolerance (1004 m2 polygon vs 0.10 ha)
        r = client.post("/api/frame-revisions/",
                        dict(plot="P1", boundary=rect(0, 0, 50.2, 20),
                             declared_area_ha=0.10, crs_epsg=32650),
                        format="json")
        self.assertEqual(r.status_code, 201, r.content)
        rev_id = r.json()["id"]
        # cannot publish a draft
        pr = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                         {"publication_reason": "x"}, format="json")
        self.assertEqual(pr.status_code, 409)
        # review requires a note
        sr = client.post(f"/api/frame-revisions/{rev_id}/submit_review/",
                         {"review_note": ""}, format="json")
        self.assertEqual(sr.status_code, 400)
        sr = client.post(f"/api/frame-revisions/{rev_id}/submit_review/",
                         {"review_note": "qa ok"}, format="json")
        self.assertEqual(sr.status_code, 200)
        # publish without reason rejected; with reason succeeds
        pr0 = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                          {"publication_reason": ""}, format="json")
        self.assertEqual(pr0.status_code, 400)
        pr1 = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                          {"publication_reason": "routine resurvey"},
                          format="json")
        self.assertEqual(pr1.status_code, 200, pr1.content)
        # published row is immutable in the model layer
        published = PlotFrameRevision.objects.get(pk=rev_id)
        published.declared_area_ha = 0.99
        with self.assertRaises(PermissionError):
            published.save()
        # republishing an already-current published edition stays singular
        pr2 = client.post(f"/api/frame-revisions/{rev_id}/publish/",
                          {"publication_reason": "again"}, format="json")
        self.assertEqual(pr2.status_code, 409)
        self.assertEqual(PlotFrameRevision.objects.filter(
            plot=self.p1, status=FRAME_PUBLISHED).count(), 1)

    # 4) two racing publish requests -> exactly one published edition
    def test_concurrent_publish_only_one_wins(self):
        # Real connection-level race: the scenario runs in a subprocess with
        # its own file-backed sqlite database (the in-process test DB is an
        # in-memory shared-cache database that cannot share state across
        # separate connections). The DB partial-unique index is the backstop
        # that guarantees a single published edition.
        import os
        import subprocess
        import sys

        tmpdir = tempfile.mkdtemp()
        db_path = f"{tmpdir}/race.sqlite3"
        script = f'''
import json, threading, os, sys

# Point the backend at the file-backed race DB BEFORE django.setup().
sys.path.insert(0, os.getcwd())
import django.conf.global_settings as _gs
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "foreststation.settings")

# Monkeypatch via a tiny settings shim module imported before setup.
import importlib, types
import foreststation.settings as base
shim = types.ModuleType("foreststation.race_settings")
for name in dir(base):
    setattr(shim, name, getattr(base, name))
shim.DATABASES = {{
    "default": {{
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": {db_path!r},
        "OPTIONS": {{"timeout": 20}},
    }}
}}
sys.modules["foreststation.race_settings"] = shim
os.environ["DJANGO_SETTINGS_MODULE"] = "foreststation.race_settings"

import django
django.setup()
from django.core.management import call_command
call_command("migrate", verbosity=0, run_syncdb=False)

from inventory.models import (AllometricEquation, Campaign, Plot, Species,
                              Stratum, PlotFrameRevision, FRAME_REVIEWED)
from inventory.services.frames import (create_or_get_revision,
                                       submit_for_review, publish_revision,
                                       FrameWorkflowError)
from inventory.services.ingest import import_campaign_rows

AM = "alive_measured"
s = Stratum.objects.create(code="A", name="A", area_ha=100.0)
oak = Species.objects.create(code="OAK", name="Oak")
eq = AllometricEquation.objects.create(
    code="OAK", version="1", status="confirmed",
    a=0.1, b=2.0, c=0.5, dbh_min_cm=5, dbh_max_cm=100,
    residual_sigma=0.1, citation="x")
eq.species.add(oak)
c1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
c2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
def rect(ox, oy, w, d):
    return [[ox, oy], [ox+w, oy], [ox+w, oy+d], [ox, oy+d], [ox, oy]]
plot = Plot.objects.create(
    code="R1", stratum=s, x_m=25, y_m=10,
    declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
    area_polygon_ha=0.10)
import_campaign_rows(c1, [
    dict(plot="R1", field_number="1", species="OAK", x_m=5, y_m=5,
         status=AM, dbh_raw=20.0, dbh_unit="cm",
         height_raw=15.0, height_unit="m")], 0.01)
import_campaign_rows(c2, [
    dict(plot="R1", field_number="1", species="OAK", x_m=5, y_m=5,
         status=AM, dbh_raw=21.0, dbh_unit="cm",
         height_raw=15.3, height_unit="m")], 0.01)
r2 = create_or_get_revision(
    plot, boundary=rect(0, 0, 60, 20),
    declared_area_ha=0.12, crs_epsg=32650)[0]
submit_for_review(r2, "qa")
r3 = create_or_get_revision(
    plot, boundary=rect(0, 0, 70, 20),
    declared_area_ha=0.14, crs_epsg=32650)[0]
submit_for_review(r3, "qa")
id2, id3 = r2.id, r3.id

results = {{}}
def worker(rid, key):
    from django.db import connection
    try:
        r = PlotFrameRevision.objects.get(pk=rid)
        publish_revision(r, "concurrent publication")
        results[key] = "ok"
    except Exception as exc:
        results[key] = type(exc).__name__
    finally:
        connection.close()

ta = threading.Thread(target=worker, args=(id2, "a"))
tb = threading.Thread(target=worker, args=(id3, "b"))
ta.start(); tb.start(); ta.join(); tb.join()

published = list(PlotFrameRevision.objects.filter(
    plot=plot, status="published").values_list("id", flat=True))
losers = {{i: PlotFrameRevision.objects.get(pk=i).status
          for i in (id2, id3) if i not in published}}
print(json.dumps({{"results": results, "published": published,
                  "losers": losers}}))
'''
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            self.fail("race subprocess failed:\n" + proc.stderr
                      + proc.stdout)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(
            sorted(payload["results"].values()).count("ok"), 1, payload)
        self.assertEqual(len(payload["published"]), 1, payload)
        # the loser remains reviewed — never half-published
        self.assertEqual(list(payload["losers"].values()),
                         [FRAME_REVIEWED], payload)

    # 5) failed validation leaves no half-published boundary / bad estimate
    def test_failed_publish_leaves_nothing_behind(self):
        # draft estimate exists; then a blocked revision is forced to
        # reviewed and publish fails: frame, plot and draft all stay put.
        client = APIClient()
        r = client.post("/api/estimates/",
                        dict(label="d", t1_campaign="t1", t2_campaign="t2",
                             equation_ids=[self.eq.id], fpc=False),
                        format="json")
        draft_vid = r.json()["id"]
        draft_before = r.json()["result_payload"]

        rev, _ = create_or_get_revision(
            self.p1, boundary=rect(20, 0, 50, 20),
            declared_area_ha=0.10, crs_epsg=32650)
        rev = submit_for_review(rev, "qa")
        with self.assertRaises(Exception):
            publish_revision(rev, "should fail")

        rev.refresh_from_db()
        self.assertEqual(rev.status, FRAME_REVIEWED)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.declared_area_ha, 0.10)
        self.assertEqual(self.p1.boundary, rect(0, 0, 50, 20))
        # draft estimate untouched (no wrong-area re-expansion)
        draft_after = client.get(f"/api/estimates/{draft_vid}/").json()
        self.assertEqual(draft_after["result_payload"], draft_before)
        prov = draft_after["result_payload"]["provenance"]["plots"][0]
        self.assertEqual(prov["frame_revision_no"], 1)

        # an invalid upload itself persists no revision and no issue
        before_count = PlotFrameRevision.objects.count()
        bad = client.post("/api/frame-revisions/",
                          dict(plot="P1", boundary=[[0, 0], [1, 1]],
                               declared_area_ha=0.1, crs_epsg=32650),
                          format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(PlotFrameRevision.objects.count(), before_count)

    # 6) successful publish atomically refreshes draft estimates in-tx
    def test_publish_refreshes_existing_drafts_against_new_frame(self):
        client = APIClient()
        r = client.post("/api/estimates/",
                        dict(label="stale-draft", t1_campaign="t1",
                             t2_campaign="t2", equation_ids=[self.eq.id],
                             fpc=False), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid, before = r.json()["id"], \
            r.json()["result_payload"]["components"]["survivor_growth"]

        rev, _ = create_or_get_revision(
            self.p1, boundary=rect(0, 0, 100, 20),
            declared_area_ha=0.20, crs_epsg=32650)
        rev = submit_for_review(rev, "qa resurvey")
        rev, refreshed = publish_revision(rev, "correction registered")
        self.assertIn(vid, refreshed)
        after = client.get(f"/api/estimates/{vid}/").json()
        self.assertAlmostEqual(
            after["result_payload"]["components"]["survivor_growth"]
                 ["total_kg"],
            before["total_kg"] * 0.5, delta=1e-6)
        # binding moved to the new frame edition
        self.assertEqual(after["frames"][0]["revision_no"], rev.revision_no)

    # 7) measurements record the frame they were collected under
    def test_measurements_keep_collected_frame_attribution(self):
        # t1/t2 measurements in setUp landed on the baseline v1 frame
        m = _tree_rows(self.p1)[0]
        self.assertIsNotNone(m.collected_frame)
        self.assertEqual(m.collected_frame.revision_no, 1)
        rev, _ = create_or_get_revision(
            self.p1, boundary=rect(0, 0, 100, 20),
            declared_area_ha=0.20, crs_epsg=32650)
        rev = submit_for_review(rev, "qa")
        publish_revision(rev, "go")
        # historical rows unchanged
        for old in _tree_rows(self.p1):
            old.refresh_from_db()
            self.assertEqual(old.collected_frame.revision_no, 1)
        # a NEW measurement on the new frame is attributed to that edition
        t3 = Campaign.objects.create(code="t3", measured_on="2025-06-01")
        import_campaign_rows(t3, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=90, y_m=5, status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m")], 0.01)
        newm = TreeMeasurement.objects.get(campaign=t3)
        self.assertEqual(newm.collected_frame.revision_no, rev.revision_no)

    # 8) compare & impact query API
    def test_compare_and_impact_apis(self):
        client = APIClient()
        r = client.post("/api/frame-revisions/",
                        dict(plot="P1", boundary=rect(20, 0, 50, 20),
                             declared_area_ha=0.10, crs_epsg=32650),
                        format="json")
        rid = r.json()["id"]
        cmp = client.get(f"/api/frame-revisions/{rid}/compare/").json()
        self.assertEqual(cmp["old"]["revision_no"], 1)
        self.assertEqual(cmp["new"]["revision_no"], 2)
        self.assertFalse(cmp["geometry_identical"])
        self.assertEqual(cmp["affected"]["excluded_count"], 2)
        impact = client.get(f"/api/frame-revisions/{rid}/impact/").json()
        self.assertFalse(impact["publishable"])  # still a draft
        self.assertTrue(impact["open_blocking_issues"])
        # list filtering
        ls = client.get("/api/frame-revisions/?plot=P1&status=draft").json()
        self.assertEqual({x["id"] for x in ls}, {rid})
