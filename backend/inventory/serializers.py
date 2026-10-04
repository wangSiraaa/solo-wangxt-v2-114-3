from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    FrameRevisionIssue,
    IdentityConflict,
    Plot,
    PlotFrameRevision,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)


class StratumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stratum
        fields = ["id", "code", "name", "area_ha"]


class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "code", "name", "family"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["id", "code", "measured_on", "description"]


class PlotSerializer(serializers.ModelSerializer):
    stratum_code = serializers.CharField(source="stratum.code", read_only=True)
    stratum_name = serializers.CharField(source="stratum.name", read_only=True)
    crs_epsg = serializers.SerializerMethodField()
    current_frame_revision_no = serializers.SerializerMethodField()
    frame_revisions = serializers.SerializerMethodField()

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
            "current_frame_revision_no", "frame_revisions",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG

    def get_current_frame_revision_no(self, obj):
        published = [r for r in self._revisions(obj) if r.status == "published"]
        return published[0].revision_no if published else None

    def get_frame_revisions(self, obj):
        return [
            {"id": r.id, "revision_no": r.revision_no, "status": r.status,
             "declared_area_ha": r.declared_area_ha,
             "polygon_area_ha": r.polygon_area_ha, "crs_epsg": r.crs_epsg,
             "open_issue_count": self._open_counts().get(r.id, 0)}
            for r in self._revisions(obj)
        ]

    def _revisions(self, obj):
        self._prime()
        return self._frame_by_plot.get(obj.id, [])

    def _open_counts(self):
        self._prime()
        return self._open_counts_map

    def _prime(self):
        if getattr(self, "_primed", False):
            return
        from inventory.models import PlotFrameRevision
        self._frame_by_plot = {}
        self._open_counts_map = {}
        for r in PlotFrameRevision.objects.prefetch_related("issues"):
            self._frame_by_plot.setdefault(r.plot_id, []).append(r)
            self._open_counts_map[r.id] = sum(
                1 for i in r.issues.all() if i.status == "open")
        self._primed = True


class EquationSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )

    class Meta:
        model = AllometricEquation
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation", "created_at",
        ]


class TreeSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    species_code = serializers.CharField(source="species.code", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(
        source="superseded_tree", read_only=True
    )

    class Meta:
        model = Tree
        fields = [
            "id", "plot", "plot_code", "species_code",
            "current_field_number", "first_campaign", "supersedes",
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="tree.plot.code", read_only=True)
    field_number = serializers.CharField(source="field_number_seen")
    collected_frame_revision = serializers.SerializerMethodField()

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
            "collected_frame", "collected_frame_revision",
        ]

    def get_collected_frame_revision(self, obj):
        return obj.collected_frame.revision_no if obj.collected_frame_id else None


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "field_number", "t1_campaign", "t2_campaign",
            "t1_measurement", "t2_measurement", "distance_m",
            "status", "resolution_note", "resolved_at",
        ]
        read_only_fields = ["distance_m", "resolved_at"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class FrameRevisionIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = FrameRevisionIssue
        fields = [
            "id", "kind", "detail", "summary", "status",
            "resolution_note", "resolved_at", "created_at",
        ]
        read_only_fields = ["kind", "detail", "summary", "resolved_at",
                            "created_at"]


class FrameRevisionIssueResolveSerializer(serializers.Serializer):
    resolution_note = serializers.CharField()


class PlotFrameRevisionSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    issues = FrameRevisionIssueSerializer(many=True, read_only=True)
    supersedes_revision_no = serializers.SerializerMethodField()

    class Meta:
        model = PlotFrameRevision
        fields = [
            "id", "plot", "plot_code", "revision_no", "status",
            "declared_area_ha", "boundary", "polygon_area_ha", "area_check",
            "crs_epsg", "crs_note", "geometry_checksum", "content_checksum",
            "reason", "validation_payload",
            "supersedes", "supersedes_revision_no",
            "created_at", "reviewed_at", "review_note",
            "published_at", "publication_reason", "issues",
        ]
        read_only_fields = [
            "revision_no", "status", "polygon_area_ha", "area_check",
            "geometry_checksum", "content_checksum", "validation_payload",
            "supersedes", "created_at", "reviewed_at", "review_note",
            "published_at", "publication_reason",
        ]

    def get_supersedes_revision_no(self, obj):
        return obj.supersedes.revision_no if obj.supersedes_id else None


class FrameRevisionCreateSerializer(serializers.Serializer):
    boundary = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField()),
        help_text="Ring of [x_m, y_m] pairs in the stated CRS.")
    declared_area_ha = serializers.FloatField(min_value=0.0001)
    crs_epsg = serializers.IntegerField(min_value=1000, max_value=99999)
    crs_note = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True)


class FrameReviewSerializer(serializers.Serializer):
    review_note = serializers.CharField()


class FramePublishSerializer(serializers.Serializer):
    publication_reason = serializers.CharField()


class EstimateVersionSerializer(serializers.ModelSerializer):
    frames = serializers.SerializerMethodField()

    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "frames", "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]

    def get_frames(self, obj):
        if not hasattr(obj, "_frames_cache"):
            obj._frames_cache = list(
                obj.frames.select_related("plot").all())
        return [
            {"id": f.id, "plot": f.plot_id,
             "plot_code": f.plot.code, "revision_no": f.revision_no,
             "status": f.status, "declared_area_ha": f.declared_area_ha}
            for f in obj._frames_cache
        ]


class MeasurementImportRowSerializer(serializers.Serializer):
    """One raw field row. Units are mandatory with every value."""

    plot = serializers.CharField()
    field_number = serializers.CharField()
    species = serializers.CharField()
    x_m = serializers.FloatField()
    y_m = serializers.FloatField()
    status = serializers.ChoiceField(
        choices=["alive_measured", "alive_not_measured", "dead", "missing_tree"]
    )
    dbh_raw = serializers.FloatField(required=False, allow_null=True)
    dbh_unit = serializers.ChoiceField(choices=["cm", "mm", "in"],
                                       required=False, allow_null=True)
    height_raw = serializers.FloatField(required=False, allow_null=True)
    height_unit = serializers.ChoiceField(choices=["m"],
                                          required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Used ONLY to record a field-book verified renumber. Never inferred.
    verified_renumber_of_tree = serializers.IntegerField(
        required=False, allow_null=True
    )


class MeasurementImportSerializer(serializers.Serializer):
    campaign = serializers.CharField()
    rows = MeasurementImportRowSerializer(many=True)
