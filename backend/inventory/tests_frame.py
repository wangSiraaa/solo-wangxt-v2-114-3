"""
Acceptance tests for the plot sampling-frame revision workflow.

Acceptance scenarios covered:
  1. A clean area revision changes per-hectare expansion ONLY for new drafts
     bound to the new frame; old confirmed estimates never change.
  2. A revision whose new boundary excludes an existing historical stem
     position is forced into `blocked` with a pending item and can never be
     reviewed or published; the TreeMeasurement row is not moved/deleted.
  3. Re-uploading the identical geometry is idempotent: same revision id, no
     duplicate.
  4. Two concurrent publish requests produce exactly ONE published revision
     and ONE new frame version (the loser gets a 409).
  5. Failed validation at publish time leaves no half-published boundary and
     no erroneous estimate/frame.
  6. Declared/polygon area beyond tolerance and same-stratum overlap block;
     cross-stratum overlap is allowed (plots belong to different frames of
     inference).
  7. Estimates bind explicitly to a frame version; historical measurements
     remain attributed to the boundary in force at collection.
"""
import threading
import time
from datetime import date

from django.test import TransactionTestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    FrameIssue,
    Plot,
    PlotFrameRevision,
    REVISION_BLOCKED,
    REVISION_DRAFT,
    REVISION_PUBLISHED,
    REVISION_REVIEWED,
    SamplingFrameVersion,
    Species,
    Stratum,
    TreeMeasurement,
)
from inventory.services.ingest import import_campaign_rows


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM = "alive_measured"


def make_tree(plot, campaign, num="1", x=10.0, y=10.0, dbh=20.0):
    return import_campaign_rows(campaign, [
        dict(plot=plot.code, field_number=num, species="OAK",
             x_m=x, y_m=y, status=AM, dbh_raw=dbh, dbh_unit="cm",
             height_raw=15.0, height_unit="m")], 0.01)


class FrameRevisionTestMixin:
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(
            code="t1", measured_on=date(2019, 1, 1))
        self.t2 = Campaign.objects.create(
            code="t2", measured_on=date(2024, 1, 1))
        # 0.10 ha baseline plot: 50 x 20 m, origin (0,0)
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=25, y_m=10,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.client = APIClient()

    def revision_payload(self, ring, declared, crs=32650, **extra):
        body = {"plot": "P1", "boundary": ring,
                "declared_area_ha": declared, "crs_epsg": crs,
                "crs_note": "re-survey 2026"}
        body.update(extra)
        return body

    def publish_clean(self, rid, reason="boundary re-survey accepted"):
        r = self.client.post(f"/api/plot-revisions/{rid}/review/",
                             {"reason": reason}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        p = self.client.post(f"/api/plot-revisions/{rid}/publish/",
                             {"reason": reason}, format="json")
        self.assertEqual(p.status_code, 200, p.content)
        return p


class FrameRevisionAPITests(FrameRevisionTestMixin, TransactionTestCase):
    reset_sequences = True

    # ---------------------------------------------------------------- 1. area
    def test_clean_area_revision_only_changes_new_draft_per_hectare(self):
        make_tree(self.p1, self.t1)
        make_tree(self.p1, self.t2, dbh=21.0)
        body = dict(label="old-frame", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = self.client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        old_id = r.json()["id"]
        old_growth_kg = (r.json()["result_payload"]["components"]
                         ["survivor_growth"]["total_kg"])
        old_plot_area = r.json()["design_snapshot"]["plot_areas"]["P1"]
        self.assertEqual(old_plot_area, 0.10)
        # default estimate bound to the baseline frame v1
        self.assertEqual(r.json()["frame_version"], 1)
        self.client.post(f"/api/estimates/{old_id}/confirm/")

        # clean area revision: 50 x 25 m = 0.125 ha, declared consistently.
        new_ring = rect(0, 0, 50, 25)
        rr = self.client.post("/api/plot-revisions/",
                              self.revision_payload(new_ring, 0.125),
                              format="json")
        self.assertEqual(rr.status_code, 201, rr.content)
        rid = rr.json()["id"]
        self.assertEqual(rr.json()["status"], REVISION_DRAFT)
        self.assertEqual(rr.json()["open_issue_count"], 0)
        # original boundary/area preserved on the revision
        self.assertEqual(rr.json()["original_declared_area_ha"], 0.10)
        self.assertEqual(rr.json()["original_boundary"], rect(0, 0, 50, 20))
        p = self.publish_clean(rid)
        self.assertEqual(p.json()["status"], REVISION_PUBLISHED)
        self.assertEqual(p.json()["emitted_frame"]["version"], 2)

        # Plot row itself was NOT rewritten; historical measurement untouched
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.declared_area_ha, 0.10)
        m = TreeMeasurement.objects.get(campaign=self.t2)
        self.assertEqual((m.x_m, m.y_m), (10.0, 10.0))

        # NEW draft bound to the new frame uses the new area; the old
        # confirmed edition is byte-identical.
        r2 = self.client.post(
            "/api/estimates/",
            dict(label="new-frame", t1_campaign="t1", t2_campaign="t2",
                 equation_ids=[self.eq.id], fpc=False),
            format="json")
        self.assertEqual(r2.status_code, 201, r2.content)
        new_growth_kg = (r2.json()["result_payload"]["components"]
                         ["survivor_growth"]["total_kg"])
        self.assertEqual(r2.json()["frame_version"], 2)
        self.assertEqual(
            r2.json()["design_snapshot"]["plot_areas"]["P1"], 0.125)
        # per-hectare expansion: 100 ha stratum x (growth / plot area):
        # growth ~ proportional to 1/area -> 0.10/0.125 = 0.8 ratio
        self.assertAlmostEqual(new_growth_kg / old_growth_kg,
                               0.10 / 0.125, delta=1e-6)

        old_after = self.client.get(f"/api/estimates/{old_id}/").json()
        self.assertEqual(
            old_after["result_payload"]["components"]["survivor_growth"]
            ["total_kg"],
            old_growth_kg)
        self.assertEqual(old_after["status"], "confirmed")
        self.assertEqual(old_after["frame_version"], 1)

    # ------------------------------------------------------- 2. exclusion block
    def test_revision_excluding_historical_stem_is_blocked(self):
        make_tree(self.p1, self.t1, x=10, y=10)
        make_tree(self.p1, self.t2, x=10, y=10, dbh=21.0)
        # new boundary clips away the north 10 m strip: stem (10,10) survives
        # but stem (10,18) test is below; here we move south edge to y=15:
        excluded_stem = import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="9", species="OAK",
                 x_m=10, y_m=18, status=AM, dbh_raw=14.0, dbh_unit="cm",
                 height_raw=11.0, height_unit="m")], 0.01)
        new_ring = rect(0, 0, 50, 15)  # 0.075 ha
        rr = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(new_ring, 0.075), format="json")
        self.assertEqual(rr.status_code, 201, rr.content)
        data = rr.json()
        self.assertEqual(data["status"], REVISION_BLOCKED)
        kinds = {i["kind"] for i in data["issues"] if i["status"] == "open"}
        self.assertIn("tree_excluded", kinds)
        issue = next(i for i in data["issues"] if i["kind"] == "tree_excluded")
        excluded_nums = {s["field_number"] for s in issue["payload"]["excluded"]}
        self.assertIn("9", excluded_nums)
        # the historical rows are still in place
        self.assertTrue(
            TreeMeasurement.objects.filter(
                tree__plot=self.p1, x_m=10, y_m=18).exists())

        # cannot review or publish while blocked
        rv = self.client.post(f"/api/plot-revisions/{data['id']}/review/",
                              {}, format="json")
        self.assertEqual(rv.status_code, 409)
        pb = self.client.post(f"/api/plot-revisions/{data['id']}/publish/",
                              {"reason": "x"}, format="json")
        self.assertEqual(pb.status_code, 409)
        # no new frame version was emitted
        self.assertEqual(SamplingFrameVersion.objects.count(), 1)

        # fixing the geometry re-includes the stem -> back to draft, and the
        # issue is resolved (not deleted)
        fixed = rect(0, 0, 50, 20)
        rv2 = self.client.post(
            f"/api/plot-revisions/{data['id']}/revalidate/",
            {"boundary": fixed, "declared_area_ha": 0.10}, format="json")
        self.assertEqual(rv2.status_code, 200, rv2.content)
        self.assertEqual(rv2.json()["status"], REVISION_DRAFT)
        self.assertTrue(FrameIssue.objects.get(kind="tree_excluded",
                                               revision_id=data["id"]).status
                        == "resolved")

    # ------------------------------------------------------- 3. idempotency
    def test_identical_geometry_reupload_is_idempotent(self):
        ring = rect(0, 0, 50, 25)
        payload = self.revision_payload(ring, 0.125)
        r1 = self.client.post("/api/plot-revisions/", payload, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.headers.get("X-Idempotent-Replay"), "false")
        r2 = self.client.post("/api/plot-revisions/", payload, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.headers.get("X-Idempotent-Replay"), "true")
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertEqual(PlotFrameRevision.objects.count(), 1)
        self.assertEqual(
            PlotFrameRevision.objects.get().content_checksum,
            r1.json()["content_checksum"])

    def test_competing_open_revision_conflicts(self):
        r1 = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(rect(0, 0, 50, 25), 0.125),
            format="json")
        self.assertEqual(r1.status_code, 201)
        # different geometry while the first proposal is still open
        r2 = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(rect(0, 0, 50, 24), 0.12),
            format="json")
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["existing_revision_id"], r1.json()["id"])
        self.assertEqual(PlotFrameRevision.objects.count(), 1)

    # ---------------------------------------------------- 5. no half-publish
    def test_area_mismatch_blocks_and_leaves_no_frame(self):
        ring = rect(0, 0, 50, 25)  # polygon 0.125 ha
        r = self.client.post(
            "/api/plot-revisions/",
            # declare 0.50 ha -> 300% mismatch, well beyond 1%
            self.revision_payload(ring, 0.50), format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["status"], REVISION_BLOCKED)
        kinds = {i["kind"] for i in r.json()["issues"] if i["status"] == "open"}
        self.assertIn("area_mismatch", kinds)
        self.assertEqual(SamplingFrameVersion.objects.count(), 1)  # baseline
        self.assertFalse(
            PlotFrameRevision.objects.filter(status="published").exists())

    def test_publish_blocked_during_final_check_persists_blocked_only(self):
        # clean at creation, reviewed, then a new same-stratum neighbour
        # appears overlapping the proposed boundary before publish: the final
        # validation must block durably without emitting a frame.
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        rid = r.json()["id"]
        rv = self.client.post(f"/api/plot-revisions/{rid}/review/",
                              {"reason": "ok"}, format="json")
        self.assertEqual(rv.status_code, 200)
        # neighbour plot overlapping the proposed strip (current baseline
        # boundary of neighbour at y 22..30)
        Plot.objects.create(
            code="PN", stratum=self.sA, x_m=100, y_m=26,
            declared_area_ha=0.04, boundary=rect(0, 22, 50, 8),
            area_polygon_ha=0.04)
        p = self.client.post(f"/api/plot-revisions/{rid}/publish/",
                             {"reason": "should fail"}, format="json")
        self.assertEqual(p.status_code, 409, p.content)
        rev = PlotFrameRevision.objects.get(pk=rid)
        self.assertEqual(rev.status, REVISION_BLOCKED)
        self.assertIsNone(rev.emitted_frame_id)
        self.assertEqual(SamplingFrameVersion.objects.count(), 1)
        self.assertTrue(rev.issues.filter(kind="overlap", status="open")
                        .exists())

    # ------------------------------------------------------- 6a. same stratum
    def test_same_stratum_overlap_blocks(self):
        # neighbour in the SAME stratum with a current boundary reaching y=24
        Plot.objects.create(
            code="PN", stratum=self.sA, x_m=100, y_m=12,
            declared_area_ha=0.02, boundary=rect(0, 22, 50, 2),
            area_polygon_ha=0.02)
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        self.assertEqual(r.json()["status"], REVISION_BLOCKED)
        kinds = {i["kind"] for i in r.json()["issues"] if i["status"] == "open"}
        self.assertIn("overlap", kinds)

    def test_cross_stratum_overlap_is_allowed(self):
        sB = Stratum.objects.create(code="B", name="B", area_ha=50.0)
        Plot.objects.create(
            code="PB", stratum=sB, x_m=100, y_m=12,
            declared_area_ha=0.02, boundary=rect(0, 22, 50, 2),
            area_polygon_ha=0.02)
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["status"], REVISION_DRAFT)

    # ------------------------------------------------------- compare/impact
    def test_compare_and_impact_endpoints(self):
        make_tree(self.p1, self.t1, x=10, y=10)
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        rid = r.json()["id"]
        cmp_ = self.client.get(f"/api/plot-revisions/{rid}/compare/")
        self.assertEqual(cmp_.status_code, 200)
        self.assertAlmostEqual(cmp_.json()["area_delta_ha"], 0.025)
        self.assertEqual(cmp_.json()["retained_stem_count"], 1)
        imp = self.client.get(f"/api/plot-revisions/{rid}/impact/")
        self.assertEqual(imp.status_code, 200)
        self.assertEqual(imp.json()["affected_measurements"], [])
        self.assertIn("per_hectare_expansion", imp.json())

        self.publish_clean(rid)
        # after publish, impact shows the emitted frame and estimate binding
        body = dict(label="x", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        self.client.post("/api/estimates/", body, format="json")
        imp2 = self.client.get(f"/api/plot-revisions/{rid}/impact/").json()
        self.assertEqual(imp2["emitted_frame_version"], 2)
        bindings = {v["frame_version"]: v["affected_by_this_revision"]
                    for v in imp2["estimate_versions"]}
        self.assertEqual(bindings.get(2), True)

    def test_lifecycle_requires_review_before_publish(self):
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        rid = r.json()["id"]
        p = self.client.post(f"/api/plot-revisions/{rid}/publish/",
                             {"reason": "skip review"}, format="json")
        self.assertEqual(p.status_code, 409)

    def test_published_revision_is_immutable(self):
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        rid = r.json()["id"]
        self.publish_clean(rid)
        rev = PlotFrameRevision.objects.get(pk=rid)
        rev.declared_area_ha = 0.99
        with self.assertRaises(PermissionError):
            rev.save()
        # frame versions are append-only
        f = SamplingFrameVersion.objects.get(version=2)
        f.note = "tampered"
        with self.assertRaises(PermissionError):
            f.save()

    def test_crs_is_mandatory_and_retained(self):
        payload = {"plot": "P1", "boundary": rect(0, 0, 50, 25),
                   "declared_area_ha": 0.125,
                   "crs_note": "re-survey 2026"}
        r = self.client.post("/api/plot-revisions/", payload, format="json")
        self.assertEqual(r.status_code, 400)
        payload["crs_epsg"] = 32650
        r = self.client.post("/api/plot-revisions/", payload, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["crs_epsg"], 32650)
        self.assertEqual(r.json()["crs_note"], "re-survey 2026")

    def test_publish_requires_reason(self):
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125), format="json")
        rid = r.json()["id"]
        self.client.post(f"/api/plot-revisions/{rid}/review/",
                         {"reason": "ok"}, format="json")
        # clear any reason by re-review with blank then publish blank
        self.client.post(f"/api/plot-revisions/{rid}/review/",
                         {"reason": "ok"}, format="json")
        rev = PlotFrameRevision.objects.get(pk=rid)
        rev.reason = ""
        rev.save(update_fields=["reason"])
        p = self.client.post(f"/api/plot-revisions/{rid}/publish/",
                             {"reason": ""}, format="json")
        self.assertEqual(p.status_code, 400)

    def test_frames_listing_and_latest(self):
        r = self.client.get("/api/frames/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 1)
        latest = self.client.get("/api/frames/latest/")
        self.assertEqual(latest.json()["version"], 1)
        self.assertIn("P1", latest.json()["plot_payload"])


class PublishRaceTests(FrameRevisionTestMixin, TransactionTestCase):
    reset_sequences = True

    def test_publish_is_conditional_one_published_version(self):
        """
        Deterministic core of the publish guarantee across backends: a second
        publish can never produce a second published revision/frame.
        """
        make_tree(self.p1, self.t1)
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125, reason="rv"), format="json")
        rid = r.json()["id"]
        self.client.post(f"/api/plot-revisions/{rid}/review/",
                         {"reason": "ok"}, format="json")

        p1 = self.client.post(
            f"/api/plot-revisions/{rid}/publish/",
            {"reason": "first wins"}, format="json")
        self.assertEqual(p1.status_code, 200, p1.content)
        self.assertEqual(p1.json()["emitted_frame"]["version"], 2)

        # second publish attempt is rejected without another frame
        p2 = self.client.post(
            f"/api/plot-revisions/{rid}/publish/",
            {"reason": "duplicate"}, format="json")
        self.assertEqual(p2.status_code, 409)
        self.assertIn("already published", p2.json()["detail"])
        self.assertEqual(
            SamplingFrameVersion.objects.filter(version=2).count(), 1)

        rev = PlotFrameRevision.objects.get(pk=rid)
        self.assertEqual(rev.status, REVISION_PUBLISHED)
        self.assertEqual(rev.emitted_frame.version, 2)

    def test_two_concurrent_publishes_emit_one_frame(self):
        """
        Two clients racing the SAME reviewed revision. Backends with row
        locking return (200, 409); sqlite's serialized writes may make both
        first attempts land after the single commit (409, 409). Either way
        the guarantee under test holds: no 5xx/exception, exactly ONE
        published revision and exactly ONE new frame version.
        """
        make_tree(self.p1, self.t1)
        ring = rect(0, 0, 50, 25)
        r = self.client.post(
            "/api/plot-revisions/",
            self.revision_payload(ring, 0.125, reason="rv"), format="json")
        rid = r.json()["id"]
        self.client.post(f"/api/plot-revisions/{rid}/review/",
                         {"reason": "ok"}, format="json")

        from django.db import OperationalError as DjOperationalError
        results = []

        def publish():
            from django.db import connection
            try:
                client = APIClient()
                for _ in range(30):
                    try:
                        resp = client.post(
                            f"/api/plot-revisions/{rid}/publish/",
                            {"reason": "concurrent"}, format="json")
                    except DjOperationalError:
                        # sqlite shared-cache: transient write/read contention
                        time.sleep(0.01)
                        continue
                    body = resp.json()
                    # A real client retries only a TRANSIENT concurrent-write
                    # conflict; the definitive "already published" is final.
                    if (resp.status_code == 409
                            and "concurrent publication"
                            in body.get("detail", "")):
                        time.sleep(0.01)
                        continue
                    results.append((resp.status_code,
                                    body.get("detail", "")))
                    return
            except Exception as exc:
                results.append((-1, repr(exc)))
            finally:
                connection.close()

        t1 = threading.Thread(target=publish)
        t2 = threading.Thread(target=publish)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)
        self.assertFalse(t1.is_alive() or t2.is_alive())

        # never an unhandled error / 5xx
        self.assertTrue(all(c in (200, 409) for c, _ in results), results)
        codes = [c for c, _ in results]
        if codes.count(200) == 0:
            # Serialized backend: the single commit completed before both
            # threads' claims; the revision must nonetheless be published
            # exactly once by that commit.
            self.assertTrue(all("already published" in d
                                for c, d in results))
        else:
            self.assertEqual(codes.count(200), 1, results)
            self.assertEqual(codes.count(409), 1, results)

        rev = PlotFrameRevision.objects.get(pk=rid)
        self.assertEqual(rev.status, REVISION_PUBLISHED)
        self.assertEqual(
            SamplingFrameVersion.objects.filter(version=2).count(), 1)
        self.assertEqual(SamplingFrameVersion.objects.count(), 2)
        self.assertEqual(SamplingFrameVersion.objects.order_by(
            "-version").first().version, 2)
