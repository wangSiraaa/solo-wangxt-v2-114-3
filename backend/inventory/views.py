"""
REST API:

GET  /plots/                        plot positions + boundaries (React map)
GET  /trees/?campaign=CODE          individuals and remeasurement status
GET  /conflicts/                    same-number position contradictions
POST /conflicts/{id}/resolve/       human verification only
POST /imports/                      ingest a campaign's field rows
POST /estimates/                    run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance

Sampling-frame revision (boundary resurvey; old published frame preserved):
GET  /frame-revisions/                          editions (+ ?plot=, ?status=)
POST /frame-revisions/                         upload resurvey -> draft
GET  /frame-revisions/{id}/                    edition + blocking issues
POST /frame-revisions/{id}/revalidate/         re-run geometry checks
POST /frame-revisions/{id}/submit_review/      draft -> reviewed (QA note)
POST /frame-revisions/{id}/publish/            reviewed -> published
GET  /frame-revisions/{id}/compare/            old vs new boundary + issues
GET  /frame-revisions/{id}/impact/             affected historical stems
POST /frame-issues/{id}/resolve/               documented human resolution
"""
import hashlib

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    EstimateVersion,
    FrameRevisionIssue,
    IdentityConflict,
    Plot,
    PlotFrameRevision,
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
    FramePublishSerializer,
    FrameRevisionCreateSerializer,
    FrameReviewSerializer,
    FrameRevisionIssueResolveSerializer,
    PlotFrameRevisionSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SpeciesSerializer,
    StratumSerializer,
    TreeSerializer,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import equation_checksum
from inventory.services.estimates_run import (
    assert_version_frames_current,
    run_draft_estimate,
)
from inventory.services.frames import (
    FrameBlockedError,
    FrameWorkflowError,
    compare_frames,
    create_or_get_revision,
    frame_impact,
    publish_revision,
    resolve_issue,
    revalidate_revision,
    submit_for_review,
)
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
    queryset = Plot.objects.select_related("stratum").prefetch_related(
        "frame_revisions__issues")
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
               "equation_ids": [...], "fpc": true}
        Creates (or recomputes) a DRAFT. The draft is explicitly bound to
        the currently published frame edition of every plot. Confirmation
        is a separate action.
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

        try:
            version = run_draft_estimate(
                label=label, t1=t1, t2=t2, equations_qs=equations_qs,
                fpc=bool(request.data.get("fpc", True)))
        except Exception as exc:  # validation must not leave a draft behind
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        # The frame binding must be current: a frame published AFTER this
        # draft was last refreshed refreshes drafts in the same publishing
        # transaction, so this should only fire for concurrent staleness.
        if not assert_version_frames_current(version):
            return Response(
                {"detail": "this draft is bound to a superseded frame "
                           "edition; re-run the draft against the current "
                           "frame before confirming."},
                status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            # re-verify checksum: equations must not have drifted since run
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
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations", "frames",
                                                 "frames__plot"), pk=pk)


def _get_revision(pk):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        PlotFrameRevision.objects.select_related("plot")
        .prefetch_related("issues"), pk=pk)


def _frame_error_response(exc, http_status=status.HTTP_409_CONFLICT):
    payload = {"detail": str(exc)}
    issues = getattr(exc, "issues", None)
    if issues:
        payload["blocking_issues"] = [
            {"id": i.id, "kind": i.kind, "summary": i.summary}
            for i in issues]
    return Response(payload, status=http_status)


class FrameRevisionViewSet(viewsets.ViewSet):
    """
    Sampling-frame editions: draft -> reviewed -> published.

    Every resurvey is a NEW immutable edition; the already-published frame
    (and confirmed estimates bound to it) is never rewritten. Editions that
    exclude stems, fail the area cross-check or overlap a same-stratum plot
    carry open blocking issues and cannot be published.
    """

    def list(self, request):
        qs = (PlotFrameRevision.objects
              .select_related("plot").prefetch_related("issues")
              .order_by("plot__code", "-revision_no"))
        plot = request.query_params.get("plot")
        if plot:
            qs = qs.filter(plot__code=plot)
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return Response(
            PlotFrameRevisionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(PlotFrameRevisionSerializer(_get_revision(pk)).data)

    def create(self, request):
        ser = FrameRevisionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        plot_code = (request.data.get("plot")
                     or request.parser_context["kwargs"].get("plot"))
        plot = Plot.objects.filter(code=plot_code).first()
        if plot is None:
            return Response({"detail": f"unknown plot {plot_code!r}"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            revision, created = create_or_get_revision(
                plot, boundary=v["boundary"],
                declared_area_ha=v["declared_area_ha"],
                crs_epsg=v["crs_epsg"], crs_note=v.get("crs_note", ""),
                reason=v.get("reason", ""))
        except Exception as exc:
            # Invalid geometry/area: nothing is persisted, no half-edition.
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        data = PlotFrameRevisionSerializer(revision).data
        # Idempotent upload: identical content returns the SAME revision.
        return Response(data, status=(status.HTTP_201_CREATED if created
                                      else status.HTTP_200_OK))

    @action(detail=True, methods=["post"])
    def revalidate(self, request, pk=None):
        revision = _get_revision(pk)
        try:
            payload = revalidate_revision(revision)
        except FrameWorkflowError as exc:
            return _frame_error_response(exc)
        revision.refresh_from_db()
        data = PlotFrameRevisionSerializer(revision).data
        data["latest_validation"] = payload
        return Response(data)

    @action(detail=True, methods=["post"], url_path="submit_review")
    def submit_review(self, request, pk=None):
        revision = _get_revision(pk)
        ser = FrameReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            revision = submit_for_review(revision, ser.validated_data
                                         ["review_note"])
        except FrameWorkflowError as exc:
            return _frame_error_response(exc)
        return Response(PlotFrameRevisionSerializer(revision).data)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """
        reviewed -> published. Atomic with Plot update and draft-estimate
        refresh; two racing publish calls can produce only ONE published
        edition (DB unique constraint), the loser gets 409.
        """
        revision = _get_revision(pk)
        ser = FramePublishSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            revision, refreshed = publish_revision(
                revision, ser.validated_data["publication_reason"])
        except FrameBlockedError as exc:
            return _frame_error_response(exc, status.HTTP_422_UNPROCESSABLE_ENTITY)
        except FrameWorkflowError as exc:
            return _frame_error_response(exc, status.HTTP_409_CONFLICT)
        data = PlotFrameRevisionSerializer(revision).data
        data["refreshed_draft_estimate_ids"] = refreshed
        return Response(data)

    @action(detail=True, methods=["get"])
    def compare(self, request, pk=None):
        return Response(compare_frames(_get_revision(pk)))

    @action(detail=True, methods=["get"])
    def impact(self, request, pk=None):
        return Response(frame_impact(_get_revision(pk)))


class FrameIssueResolveView(viewsets.ViewSet):
    """POST /frame-issues/{id}/resolve/ — documented human resolution."""

    def resolve(self, request, pk=None):
        from django.shortcuts import get_object_or_404
        issue = get_object_or_404(FrameRevisionIssue, pk=pk)
        ser = FrameRevisionIssueResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            issue = resolve_issue(issue,
                                  ser.validated_data["resolution_note"])
        except FrameWorkflowError as exc:
            return _frame_error_response(exc)
        revision = (PlotFrameRevision.objects.prefetch_related("issues")
                    .get(pk=issue.revision_id))
        return Response(PlotFrameRevisionSerializer(revision).data)
