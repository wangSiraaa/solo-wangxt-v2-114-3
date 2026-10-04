"""
REST API:

GET  /plots/                        plot positions + boundaries (React map)
GET  /trees/?campaign=CODE          individuals and remeasurement status
GET  /conflicts/                    same-number position contradictions
POST /conflicts/{id}/resolve/       human verification only
POST /imports/                      ingest a campaign's field rows
POST /estimates/                    run a DRAFT bound to a frame version
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance

Sampling-frame revision workflow (a new boundary never rewrites the frame):
GET  /frames/                       published sampling-frame versions
GET  /frames/latest/                newest frame + per-plot boundary/area
GET  /plot-revisions/               revision list (filter ?plot=CODE)
POST /plot-revisions/               propose draft (idempotent re-upload)
GET  /plot-revisions/{id}/          revision with pending items
POST /plot-revisions/{id}/revalidate/  re-check / replace geometry
POST /plot-revisions/{id}/review/      draft(clean) -> reviewed
POST /plot-revisions/{id}/publish/  reviewed -> published + new frame v
GET  /plot-revisions/{id}/compare/  old vs new boundary / area / stems
GET  /plot-revisions/{id}/impact/   affected individuals + estimate binding
"""
import hashlib

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_DISTINCT,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    EstimateVersion,
    IdentityConflict,
    Plot,
    PlotFrameRevision,
    SamplingFrameVersion,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
    VERSION_CONFIRMED,
)
from inventory.serializers import (
    CampaignSerializer,
    ConflictResolveSerializer,
    ConflictSerializer,
    EquationSerializer,
    EstimateVersionSerializer,
    FrameRevisionCreateSerializer,
    FrameRevisionPublishSerializer,
    FrameRevisionRevalidateSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotFrameRevisionSerializer,
    PlotSerializer,
    SamplingFrameVersionSerializer,
    SpeciesSerializer,
    StratumSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)
from inventory.services import frame as frame_service
from inventory.services.ingest import import_campaign_rows


class StratumViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Stratum.objects.all()
    serializer_class = StratumSerializer


class SpeciesViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Species.objects.all()
    serializer_class = SpeciesSerializer


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Campaign.objects.all()
    serializer_class = CampaignSerializer


class EquationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AllometricEquation.objects.prefetch_related("species").all()
    serializer_class = EquationSerializer


class PlotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Plot.objects.select_related("stratum").all()
    serializer_class = PlotSerializer


class TreeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TreeSerializer

    def get_queryset(self):
        qs = Tree.objects.select_related("plot", "species", "superseded_tree")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(measurements__campaign__code=campaign).distinct()
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(plot__code=plot)
        return qs


class MeasurementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MeasurementSerializer

    def get_queryset(self):
        qs = TreeMeasurement.objects.select_related("tree", "tree__plot",
                                                    "campaign")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        return qs


class ConflictViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConflictSerializer

    def get_queryset(self):
        qs = IdentityConflict.objects.select_related("plot")
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Human-in-the-loop resolution. Nothing here is automatic."""
        conflict = self.get_object()
        ser = ConflictResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if conflict.status != CONFLICT_OPEN:
            return Response(
                {"detail": f"conflict already resolved as {conflict.status}; "
                           "verification cannot be undone here."},
                status=status.HTTP_409_CONFLICT,
            )
        decision = ser.validated_data["status"]
        with transaction.atomic():
            if decision == CONFLICT_RENUMBER:
                # same individual: t2's tree row becomes a successor of t1's
                t2_tree = conflict.t2_measurement.tree
                t1_tree = conflict.t1_measurement.tree
                if t2_tree != t1_tree:
                    t2_tree.superseded_tree = t1_tree
                    t2_tree.current_field_number = (
                        conflict.t2_measurement.field_number_seen
                    )
                    t2_tree.save(update_fields=["superseded_tree",
                                               "current_field_number"])
            # distinct: do nothing — t1 and t2 rows stay separate and enter
            # mortality / ingrowth candidates respectively.
            conflict.status = decision
            conflict.resolution_note = ser.validated_data.get("note", "")
            conflict.resolved_at = timezone.now()
            conflict.save()
        return Response(ConflictSerializer(conflict).data)


class ImportViewSet(viewsets.ViewSet):
    def create(self, request):
        ser = MeasurementImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        campaign = Campaign.objects.filter(
            code=ser.validated_data["campaign"]
        ).first()
        if campaign is None:
            return Response({"detail": "unknown campaign"},
                            status=status.HTTP_404_NOT_FOUND)
        result = import_campaign_rows(
            campaign, ser.validated_data["rows"],
            area_tolerance=settings.PLOT_AREA_TOLERANCE,
        )

        # re-scan identity contradictions against the other campaign
        other = Campaign.objects.exclude(pk=campaign.pk).order_by(
            "measured_on").first()
        if other:
            t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)
            result["conflicts"] = scan_conflicts(t1, t2)
        return Response(result,
                        status=status.HTTP_207_MULTI_STATUS if result["rejected"]
                        else status.HTTP_200_OK)


class EstimateViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = EstimateVersion.objects.all().order_by("-created_at")
        return Response(EstimateVersionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            EstimateVersionSerializer(_get_version(pk)).data
        )

    def create(self, request):
        """
        Body: {"label": ..., "t1_campaign": CODE, "t2_campaign": CODE,
               "equation_ids": [...], "fpc": true,
               "frame_version": <int, default latest>}
        Creates (or recomputes) a DRAFT bound EXPLICITLY to one published
        sampling-frame version. Confirmation is a separate action.
        """
        label = request.data.get("label", "draft estimate")
        t1 = Campaign.objects.filter(
            code=request.data.get("t1_campaign")).first()
        t2 = Campaign.objects.filter(
            code=request.data.get("t2_campaign")).first()
        if not t1 or not t2 or t1.measured_on >= t2.measured_on:
            return Response(
                {"detail": "need t1 earlier than t2 campaign codes"},
                status=status.HTTP_400_BAD_REQUEST)
        eq_ids = request.data.get("equation_ids", [])
        equations_qs = AllometricEquation.objects.filter(
            id__in=eq_ids
        ).prefetch_related("species")
        if equations_qs.count() != len(eq_ids) or not eq_ids:
            return Response({"detail": "equation_ids invalid/empty"},
                            status=status.HTTP_400_BAD_REQUEST)

        # Explicit frame binding: default to the latest published frame.
        frame_version_param = request.data.get("frame_version", "latest")
        frame = frame_service.ensure_baseline_frame(settings.SURVEY_CRS_EPSG)
        if frame_version_param != "latest":
            try:
                fv = int(frame_version_param)
            except (TypeError, ValueError):
                return Response({"detail": "frame_version must be an int"},
                                status=status.HTTP_400_BAD_REQUEST)
            frame = SamplingFrameVersion.objects.filter(version=fv).first()
            if frame is None:
                return Response(
                    {"detail": f"unknown sampling frame version {fv}"},
                    status=status.HTTP_404_NOT_FOUND)
        else:
            frame = frame_service.latest_frame() or frame

        table_t1, table_t2, equations, plots, strata = (
            build_measurement_table(t1, t2, equations_qs, frame=frame)
        )
        uncovered = sorted({
            r["species"] for r in table_t1 + table_t2
            if r["species"] not in equations
        })
        renumber, distinct = resolved_identity_pairs(t1, t2)

        interval = round(
            (t2.measured_on - t1.measured_on).days / 365.25, 3)
        design = {
            "t1_code": t1.code, "t2_code": t2.code,
            "interval_years": interval,
            "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
            "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
            "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
            "recruitment_cm": settings.RECRUITMENT_DBH_CM,
            "fpc": bool(request.data.get("fpc", True)),
            "crs_epsg": settings.SURVEY_CRS_EPSG,
            "frame_version": frame.version,
        }
        result = estimate(table_t1, table_t2, equations, plots, strata,
                          design,
                          resolved_renumber_pairs=renumber,
                          resolved_distinct_pairs=distinct)
        result["species_without_equation"] = uncovered
        checksum = equation_checksum(equations)

        snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                       for code, s in strata.items()}
        design_snapshot = {**design,
                           "strata": snap_strata,
                           "plot_areas": {code: info["area_ha"]
                                          for code, info in plots.items()},
                           "plot_frame_revisions": {
                               code: info.get("frame_revision_id")
                               for code, info in plots.items()},
                           "equation_ids": sorted(eq_ids),
                           "equation_codes": {sp: e["code"] + "@" + e["version"]
                                              for sp, e in equations.items()},
                           "area_tolerance": settings.PLOT_AREA_TOLERANCE}

        version = EstimateVersion.objects.create(
            label=label, frame=frame, t1_campaign=t1, t2_campaign=t2,
            design_snapshot=design_snapshot,
            result_payload=result, equation_checksum=checksum,
        )
        version.equations.set(equations_qs)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            # re-verify checksum: equations must not have drifted since run
            from inventory.services.estimator import build_measurement_table
            eqs = version.equations.all().prefetch_related("species")
            equations = {}
            for e in eqs:
                for sp in e.species.all():
                    equations[sp.code] = {
                        "code": e.code, "version": e.version,
                        "a": e.a, "b": e.b, "c": e.c,
                        "dbh_min_cm": e.dbh_min_cm,
                        "dbh_max_cm": e.dbh_max_cm,
                        "height_required": e.height_required,
                        "residual_sigma": e.residual_sigma,
                        "citation": e.citation,
                    }
            current = equation_checksum(equations)
            if current != version.equation_checksum:
                return Response(
                    {"detail": "equations changed since the run; create a "
                               "new version rather than confirming stale "
                               "numbers."},
                    status=status.HTTP_409_CONFLICT)
            version.status = VERSION_CONFIRMED
            version.confirmed_at = timezone.now()
            version.save()
            # Lock the equations: a confirmed edition's equation is frozen
            # and a new coefficient set must be issued as a new equation row.
            from inventory.models import EQUATION_CONFIRMED
            eqs.update(status=EQUATION_CONFIRMED)
        return Response(EstimateVersionSerializer(version).data)


def _get_version(pk):
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations"), pk=pk)


class SamplingFrameViewSet(viewsets.ReadOnlyModelViewSet):
    """Published, append-only sampling-frame versions."""

    serializer_class = SamplingFrameVersionSerializer

    def get_queryset(self):
        # Listing frames implicitly materialises the baseline snapshot.
        frame_service.ensure_baseline_frame(settings.SURVEY_CRS_EPSG)
        return SamplingFrameVersion.objects.all().order_by("-version")

    @action(detail=False, methods=["get"], url_path="latest")
    def latest(self, request):
        frame = frame_service.latest_frame() or \
            frame_service.ensure_baseline_frame(settings.SURVEY_CRS_EPSG)
        return Response(SamplingFrameVersionSerializer(frame).data)


class PlotFrameRevisionViewSet(viewsets.ViewSet):
    """
    Plot sampling-frame revision workflow.

    draft -> reviewed -> published, with `blocked` whenever open pending
    items exist. Historical measurements are never rewritten; estimates
    bind explicitly to emitted frame versions.
    """

    def _get_revision(self, pk):
        return get_object_or_404(
            PlotFrameRevision.objects.select_related(
                "plot", "plot__stratum", "emitted_frame").prefetch_related(
                    "issues"), pk=pk)

    def list(self, request):
        qs = (
            PlotFrameRevision.objects
            .select_related("plot", "emitted_frame")
            .prefetch_related("issues")
            .order_by("plot__code", "-revision_no"))
        plot_code = request.query_params.get("plot")
        if plot_code:
            qs = qs.filter(plot__code=plot_code)
        state = request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return Response(PlotFrameRevisionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            PlotFrameRevisionSerializer(self._get_revision(pk)).data)

    def create(self, request):
        plot = Plot.objects.filter(
            code=request.data.get("plot")).first()
        if plot is None:
            return Response({"detail": "unknown plot"},
                            status=status.HTTP_404_NOT_FOUND)
        ser = FrameRevisionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            revision, created = frame_service.create_revision(
                plot,
                boundary=data["boundary"],
                declared_area_ha=data["declared_area_ha"],
                crs_epsg=data["crs_epsg"],
                tolerance=settings.PLOT_AREA_TOLERANCE,
                crs_note=data.get("crs_note", ""),
                reason=data.get("reason", ""))
        except frame_service.FrameRevisionError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except frame_service.FrameRevisionConflict as exc:
            return Response(
                {"detail": str(exc),
                 "existing_revision_id": (
                     PlotFrameRevision.objects.filter(
                         plot=plot).exclude(status="published")
                     .order_by("-revision_no").values_list("id", flat=True)
                     .first())},
                status=status.HTTP_409_CONFLICT)
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(
            PlotFrameRevisionSerializer(self._get_revision(revision.id)).data,
            status=code,
            headers={"X-Idempotent-Replay": "true" if not created else "false"})

    @action(detail=True, methods=["post"])
    def revalidate(self, request, pk=None):
        revision = self._get_revision(pk)
        ser = FrameRevisionRevalidateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        updates = {k: v for k, v in ser.validated_data.items() if v is not None}
        try:
            revision = frame_service.revalidate_revision(
                revision, settings.PLOT_AREA_TOLERANCE, updates=updates)
        except frame_service.FrameRevisionConflict as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except frame_service.FrameRevisionError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(
            PlotFrameRevisionSerializer(self._get_revision(revision.id)).data)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        revision = self._get_revision(pk)
        reason = request.data.get("reason", "")
        try:
            revision = frame_service.review_revision(revision, reason=reason)
        except frame_service.FrameRevisionConflict as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        return Response(
            PlotFrameRevisionSerializer(self._get_revision(revision.id)).data)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        revision = self._get_revision(pk)
        ser = FrameRevisionPublishSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            revision, new_frame = frame_service.publish_revision(
                revision, reason=ser.validated_data.get("reason", ""))
        except frame_service.FrameRevisionError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except frame_service.FrameRevisionConflict as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        payload = PlotFrameRevisionSerializer(
            self._get_revision(revision.id)).data
        payload["emitted_frame"] = SamplingFrameVersionSerializer(
            new_frame).data
        return Response(payload)

    @action(detail=True, methods=["get"])
    def compare(self, request, pk=None):
        revision = self._get_revision(pk)
        return Response(frame_service.revision_comparison(revision))

    @action(detail=True, methods=["get"])
    def impact(self, request, pk=None):
        revision = self._get_revision(pk)
        return Response(frame_service.revision_impact(revision))
