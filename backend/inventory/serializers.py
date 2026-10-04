from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    FrameIssue,
    IdentityConflict,
    Plot,
    PlotFrameRevision,
    SamplingFrameVersion,
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

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG


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

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
        ]


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


class EstimateVersionSerializer(serializers.ModelSerializer):
    frame_version = serializers.IntegerField(
        source="frame.version", read_only=True, allow_null=True)

    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "frame", "frame_version", "t1_campaign",
            "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "frame", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]


class FrameIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = FrameIssue
        fields = [
            "id", "kind", "status", "detail", "payload", "fingerprint",
            "resolution_note", "created_at", "resolved_at",
        ]


class PlotFrameRevisionSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    emitted_frame_version = serializers.IntegerField(
        source="emitted_frame.version", read_only=True, allow_null=True)
    issues = FrameIssueSerializer(many=True, read_only=True)
    open_issue_count = serializers.SerializerMethodField()

    class Meta:
        model = PlotFrameRevision
        fields = [
            "id", "plot", "plot_code", "revision_no", "status",
            "original_boundary", "original_declared_area_ha",
            "original_area_polygon_ha", "original_crs_epsg",
            "boundary", "declared_area_ha", "area_polygon_ha",
            "crs_epsg", "crs_note", "area_tolerance", "area_check",
            "content_checksum", "reason",
            "emitted_frame", "emitted_frame_version", "supersedes",
            "created_at", "reviewed_at", "published_at",
            "issues", "open_issue_count",
        ]

    def get_open_issue_count(self, obj):
        return sum(1 for i in obj.issues.all() if i.status == "open")


class SamplingFrameVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SamplingFrameVersion
        fields = [
            "id", "version", "source", "source_revision", "plot_payload",
            "note", "created_at",
        ]


class FrameRevisionCreateSerializer(serializers.Serializer):
    boundary = serializers.ListField(
        child=serializers.ListField(
            child=serializers.FloatField(), min_length=2, max_length=2))
    declared_area_ha = serializers.FloatField()
    crs_epsg = serializers.IntegerField()
    crs_note = serializers.CharField(
        required=False, allow_blank=True, default="")
    reason = serializers.CharField(
        required=False, allow_blank=True, default="")


class FrameRevisionRevalidateSerializer(serializers.Serializer):
    boundary = serializers.ListField(
        required=False,
        child=serializers.ListField(
            child=serializers.FloatField(), min_length=2, max_length=2))
    declared_area_ha = serializers.FloatField(required=False)
    crs_epsg = serializers.IntegerField(required=False)
    crs_note = serializers.CharField(
        required=False, allow_blank=True)


class FrameRevisionPublishSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


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
