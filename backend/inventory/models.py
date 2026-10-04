"""
Domain model for repeated-measure permanent forest plots.

Explicit design choices
=======================
* Every measured quantity carries its unit — the database never stores a
  "dbh number" without dbh_unit next to it. Accepted units: cm/mm/in for
  dbh, m for height. Unit conversion happens at ingest; canonical storage
  is centimetres (dbh) and metres (height).
* Tree coordinates are projected (x_m, y_m) in SURVEY_CRS_EPSG; plot
  boundaries are GeoJSON polygons in the same CRS. With PostGIS,
  deploy/postgis.sql adds generated geometry columns + GiST indexes.
* Individual tree identity across surveys is NOT assumed from equal tree
  numbers. Same number + contradictory position opens an IdentityConflict
  and the pair stays out of growth estimation until a human resolves it.
* Missing measurements (alive but not measured) are distinct from real
  zero growth (measured, |Δdbh| <= tolerance, cross-checked) and from
  mortality (a mortality observation exists).
* Allometric equations are versioned. EstimateVersion freezes the
  equations and a SHA-256 checksum; confirmed versions are locked and a
  new equation can never silently alter a published estimate.
"""
from django.db import models


# ---------------------------------------------------------------- constants
DBH_UNITS = ["cm", "mm", "in"]
HEIGHT_UNITS = ["m"]

DBH_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54}

# Tree measurement status — three mutually exclusive situations that a
# careless field database tends to collapse into "dbh = 0".
STATUS_ALIVE_MEASURED = "alive_measured"
STATUS_ALIVE_NOT_MEASURED = "alive_not_measured"   # missing, NOT zero
STATUS_DEAD = "dead"                                # mortality
STATUS_MISSING = "missing_tree"                     # not located at all
TREE_STATUS_CHOICES = [
    (STATUS_ALIVE_MEASURED, "Alive and remeasured"),
    (STATUS_ALIVE_NOT_MEASURED, "Alive but not measured (missing data)"),
    (STATUS_DEAD, "Dead (mortality observation)"),
    (STATUS_MISSING, "Tree not located"),
]

CONFLICT_OPEN = "open"
CONFLICT_RENUMBER = "renumber"          # same tree, new number
CONFLICT_DISTINCT = "distinct"          # genuinely different individuals
CONFLICT_CHOICES = [
    (CONFLICT_OPEN, "Unverified — excluded from estimates"),
    (CONFLICT_RENUMBER, "Verified renumber (same individual)"),
    (CONFLICT_DISTINCT, "Verified distinct individuals"),
]

EQUATION_DRAFT = "draft"
EQUATION_CONFIRMED = "confirmed"
EQUATION_RETIRED = "retired"
EQUATION_STATUS_CHOICES = [
    (EQUATION_DRAFT, "Draft (not usable for estimates)"),
    (EQUATION_CONFIRMED, "Confirmed (locked when used)"),
    (EQUATION_RETIRED, "Retired"),
]

VERSION_DRAFT = "draft"
VERSION_CONFIRMED = "confirmed"
VERSION_SUPERSEDED = "superseded"
VERSION_STATUS_CHOICES = [
    (VERSION_DRAFT, "Draft — recomputes with current data/equations"),
    (VERSION_CONFIRMED, "Confirmed — frozen, immutable"),
    (VERSION_SUPERSEDED, "Superseded by a newer confirmed version"),
]

# ---------------------------------------------------------------------------
# Sampling-frame revision lifecycle.
#
# A re-surveyed plot boundary must NEVER silently rewrite the published
# sampling frame: every boundary / area / CRS statement becomes an immutable
# revision moving draft -> reviewed -> published (or blocked by open pending
# items). Historical TreeMeasurement rows stay bound to the boundary in force
# when they were collected; a new boundary can never migrate, delete or
# rewrite them. Estimates bind explicitly to one published SamplingFrameVersion.
REVISION_DRAFT = "draft"
REVISION_REVIEWED = "reviewed"
REVISION_PUBLISHED = "published"
REVISION_BLOCKED = "blocked"
REVISION_STATUS_CHOICES = [
    (REVISION_DRAFT, "Draft"),
    (REVISION_REVIEWED, "Reviewed — ready to publish"),
    (REVISION_PUBLISHED, "Published — a new sampling frame version"),
    (REVISION_BLOCKED, "Blocked by unresolved pending items"),
]

ISSUE_AREA_MISMATCH = "area_mismatch"
ISSUE_TREE_EXCLUDED = "tree_excluded"
ISSUE_OVERLAP = "overlap"
FRAME_ISSUE_KIND_CHOICES = [
    (ISSUE_AREA_MISMATCH, "Declared area outside tolerance of polygon area"),
    (ISSUE_TREE_EXCLUDED, "New boundary excludes an existing stem position"),
    (ISSUE_OVERLAP, "Boundary overlaps a same-stratum neighbour plot"),
]
ISSUE_OPEN = "open"
ISSUE_RESOLVED = "resolved"
FRAME_ISSUE_STATUS_CHOICES = [
    (ISSUE_OPEN, "Open — blocks publication"),
    (ISSUE_RESOLVED, "Resolved by a later validation"),
]

FRAME_SOURCE_BASELINE = "baseline"
FRAME_SOURCE_REVISION = "plot_revision"
FRAME_SOURCE_CHOICES = [
    (FRAME_SOURCE_BASELINE, "Baseline frame from the original survey polygons"),
    (FRAME_SOURCE_REVISION, "Emitted by publishing a plot frame revision"),
]


class Stratum(models.Model):
    """Sampling stratum with known land area (the sampling frame)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    area_ha = models.FloatField(
        help_text="Known stratum land area in hectares (sampling frame)."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class Plot(models.Model):
    """A permanent sample plot. Area is per plot, not assumed constant."""

    code = models.CharField(max_length=16, unique=True)
    stratum = models.ForeignKey(
        Stratum, on_delete=models.PROTECT, related_name="plots"
    )
    # Projected coordinates of plot centre, metres.
    x_m = models.FloatField(help_text="Plot centre X, projected CRS [m].")
    y_m = models.FloatField(help_text="Plot centre Y, projected CRS [m].")
    declared_area_ha = models.FloatField(
        help_text="Declared plot area in hectares. Plots may differ in size."
    )
    # GeoJSON-like ring: [[x, y], ...] in projected metres.
    boundary = models.JSONField(
        help_text="Boundary ring [[x_m, y_m], ...] in projected CRS."
    )
    area_polygon_ha = models.FloatField(
        help_text="Polygon area in ha, computed at ingest and cross-checked."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Species(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=160)
    family = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name_plural = "species"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class AllometricEquation(models.Model):
    """
    Versioned allometric biomass equation.

        agb_kg = a * (dbh_cm ** b) * (height_m ** c)

    Applicability is explicit (species list, dbh range, source/citation).
    Residual error is stored as a dimensionless multiplicative sigma
    (agb * exp(eps), eps ~ N(0, residual_sigma^2)) and propagated into
    equation-error components of uncertainty.
    """

    code = models.CharField(max_length=32)
    version = models.CharField(max_length=16)
    species = models.ManyToManyField(Species, related_name="equations")
    status = models.CharField(
        max_length=16, choices=EQUATION_STATUS_CHOICES, default=EQUATION_DRAFT
    )
    form = models.CharField(
        max_length=64,
        default="agb = a * dbh_cm^b * height_m^c",
        help_text="Human-readable equation form.",
    )
    a = models.FloatField()
    b = models.FloatField()
    c = models.FloatField()
    dbh_min_cm = models.FloatField()
    dbh_max_cm = models.FloatField()
    height_required = models.BooleanField(default=True)
    residual_sigma = models.FloatField(
        help_text="Multiplicative residual SD of ln(agb) [dimensionless]."
    )
    citation = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "version"]
        unique_together = [("code", "version")]

    @property
    def coefficient_checksum_source(self):
        return (
            f"{self.code}|{self.version}|{self.a:.10g}|{self.b:.10g}|"
            f"{self.c:.10g}|{self.dbh_min_cm:.6g}|{self.dbh_max_cm:.6g}|"
            f"{self.residual_sigma:.6g}"
        )

    _FROZEN_FIELDS = ("code", "version", "a", "b", "c", "dbh_min_cm",
                      "dbh_max_cm", "residual_sigma", "form",
                      "height_required")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == EQUATION_CONFIRMED:
                changed = [
                    f for f in self._FROZEN_FIELDS
                    if getattr(original, f) != getattr(self, f)
                ]
                if changed:
                    raise PermissionError(
                        f"Equation {self.code} v{self.version} is locked by a "
                        f"confirmed estimate; changed fields {changed}. Issue "
                        "a new equation code/version instead."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} v{self.version} [{self.status}]"


class Campaign(models.Model):
    """One measurement occasion (survey edition)."""

    code = models.CharField(max_length=16, unique=True)
    measured_on = models.DateField()
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["measured_on"]

    def __str__(self):
        return f"{self.code} ({self.measured_on})"


class Tree(models.Model):
    """
    A tracked individual. The field number is a label, not a primary key:
    renumbers keep the same Tree row (current_field_number changes), while
    a same-number/different-location finding becomes a second Tree row plus
    an IdentityConflict until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT, related_name="trees")
    current_field_number = models.CharField(max_length=16)
    species = models.ForeignKey(
        Species, on_delete=models.PROTECT, related_name="trees"
    )
    first_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="trees_first"
    )
    superseded_tree = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="successors",
        help_text="Set when this row re-numbers an earlier tree (verified).",
    )

    class Meta:
        # Field numbers are LABELS, not identities: a renumber keeps one
        # Tree row, a same-number/different-position finding creates two
        # Tree rows carrying the same label in one plot. The authoritative
        # label per occasion is TreeMeasurement.field_number_seen.
        indexes = [
            models.Index(fields=["plot", "current_field_number"]),
        ]
        ordering = ["plot__code", "current_field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.current_field_number}"


class TreeMeasurement(models.Model):
    """One tree measured (or sought and found dead/missing) at one campaign."""

    tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, related_name="measurements"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="measurements"
    )
    field_number_seen = models.CharField(
        max_length=16,
        help_text="Number physically on the tag at this campaign.",
    )
    x_m = models.FloatField(help_text="Stem position X, projected CRS [m].")
    y_m = models.FloatField(help_text="Stem position Y, projected CRS [m].")
    status = models.CharField(max_length=20, choices=TREE_STATUS_CHOICES)

    # Raw entry keeps the original unit; canonical *_cm / *_m columns are
    # converted at ingest. Both are retained so a unit error is auditable.
    dbh_raw = models.FloatField(null=True, blank=True)
    dbh_unit = models.CharField(max_length=2, null=True, blank=True,
                                choices=[(u, u) for u in DBH_UNITS])
    dbh_cm = models.FloatField(null=True, blank=True)

    height_raw = models.FloatField(null=True, blank=True)
    height_unit = models.CharField(max_length=2, null=True, blank=True,
                                   choices=[(u, u) for u in HEIGHT_UNITS])
    height_m = models.FloatField(null=True, blank=True)

    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        unique_together = [("tree", "campaign")]
        ordering = ["tree__plot__code", "tree__current_field_number"]

    def __str__(self):
        return f"{self.tree} @ {self.campaign.code}: {self.status}"


class IdentityConflict(models.Model):
    """
    Same field number at both campaigns, but positions contradict the
    hypothesis "same individual". Never auto-merged: stays open (and the
    trees are excluded from survivor growth) until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT)
    field_number = models.CharField(max_length=16)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t2"
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t1"
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t2"
    )
    distance_m = models.FloatField(
        help_text="Distance between the two reported positions [m]."
    )
    status = models.CharField(
        max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_OPEN
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.field_number}: {self.status}"


class MeasurementImportRow(models.Model):
    """Audit trail for raw imported rows, including rejected ones."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_rows"
    )
    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="import_rows"
    )
    raw_payload = models.JSONField()
    accepted = models.BooleanField()
    rejection_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EstimateVersion(models.Model):
    """
    An immutable edition of the population estimate.

    On confirmation:
      * status -> confirmed (edits to the row are blocked in save());
      * equations referenced get locked;
      * result_payload + equation_checksum are frozen.
    A later new allometric equation creates a NEW version; the confirmed
    one can never be silently changed.
    """

    label = models.CharField(max_length=120)
    frame = models.ForeignKey(
        "SamplingFrameVersion",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="estimates",
        help_text="The published sampling-frame version (plot boundaries and "
                  "areas) this estimate is computed against. Explicit, never "
                  "silently switched by a later boundary revision.",
    )
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    equations = models.ManyToManyField(AllometricEquation, related_name="estimates")
    status = models.CharField(
        max_length=12, choices=VERSION_STATUS_CHOICES, default=VERSION_DRAFT
    )
    design_snapshot = models.JSONField(
        help_text="Stratum areas, expansion design, thresholds, CRS, "
                  "uncertainty assumptions used for this run."
    )
    result_payload = models.JSONField(
        null=True, blank=True,
        help_text="Full component breakdown + provenance + uncertainty."
    )
    equation_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == VERSION_CONFIRMED:
                raise PermissionError(
                    "EstimateVersion is confirmed/frozen; create a new "
                    "version instead of mutating this one."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} [{self.status}]"


class SamplingFrameVersion(models.Model):
    """
    An immutable edition of the sampling frame.

    Version 1 is the *baseline* frame built from the originally surveyed
    polygons; each later version is emitted atomically when a PlotFrameRevision
    is published. Plots not affected by that revision keep the same boundary
    payload (carried verbatim), so a frame version is a self-contained snapshot
    of every plot's boundary, declared and polygon area and CRS.

    Frame versions are append-only: once published they are frozen and can
    never be rewritten. Estimates bind to one by FK and therefore remain
    reproducible even after later revisions publish.
    """

    version = models.PositiveIntegerField(unique=True)
    source_revision = models.ForeignKey(
        "PlotFrameRevision",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="emitted_frames",
        help_text="Null for the baseline frame; the revision that emitted it.",
    )
    source = models.CharField(
        max_length=16, choices=FRAME_SOURCE_CHOICES,
        default=FRAME_SOURCE_REVISION,
    )
    # {plot_code: {"stratum", "boundary", "declared_area_ha",
    #              "area_polygon_ha", "crs_epsg", "x_m", "y_m",
    #              "revision_id" | null}}
    plot_payload = models.JSONField()
    note = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError(
                "SamplingFrameVersion is append-only and immutable; publish a "
                "new revision instead of editing this frame version."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            "SamplingFrameVersion is append-only and cannot be deleted.")

    def __str__(self):
        return f"frame v{self.version}"


class PlotFrameRevision(models.Model):
    """
    A proposed revision of ONE plot's boundary / declared area / CRS note.

    The original boundary and area are snapshotted at creation
    (original_* columns), so the history is never lost. The revision carries
    the area cross-check result and, on publication, the human-given reason.

    Lifecycle: draft -> reviewed -> published. Validations that fail (new
    boundary excludes existing stems, polygon/declared area beyond tolerance,
    overlap with a same-stratum neighbour) open FrameIssue rows and force the
    revision into `blocked`; publication is impossible until re-validation is
    clean (back to draft, then review, then publish). Historical
    TreeMeasurement rows are NEVER moved, deleted or rewritten by a revision;
    they are only listed as affected pending items.

    Published rows are immutable; every later change is a NEW revision row.
    """

    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="frame_revisions"
    )
    revision_no = models.PositiveIntegerField(
        help_text="1-based revision sequence within the plot.")
    status = models.CharField(
        max_length=10, choices=REVISION_STATUS_CHOICES, default=REVISION_DRAFT
    )

    # The ORIGINAL geometry in force when the revision was proposed (kept
    # forever; a revision never destructively replaces the Plot row).
    original_boundary = models.JSONField()
    original_declared_area_ha = models.FloatField()
    original_area_polygon_ha = models.FloatField()
    original_crs_epsg = models.PositiveIntegerField()

    # The proposed geometry / area statement / CRS note.
    boundary = models.JSONField()
    declared_area_ha = models.FloatField()
    area_polygon_ha = models.FloatField(
        help_text="Polygon area computed at creation; cross-check result is "
                  "carried in the area_check payload."
    )
    crs_epsg = models.PositiveIntegerField(
        help_text="CRS of the proposed boundary coordinates, stated "
                  "explicitly with every revision.")
    crs_note = models.CharField(
        max_length=300, blank=True,
        help_text="Free-text CRS statement (datum, zone, source survey).")

    area_tolerance = models.FloatField(
        help_text="Relative tolerance used for the declared/polygon check.")
    area_check = models.JSONField(
        help_text='{"relative_error", "within_tolerance", "detail"}')
    content_checksum = models.CharField(
        max_length=64,
        help_text="SHA-256 of boundary+area+CRS — idempotent re-upload key.")

    reason = models.CharField(
        max_length=400, blank=True,
        help_text="Human-given reason; mandatory before publication.")
    emitted_frame = models.ForeignKey(
        SamplingFrameVersion, on_delete=models.PROTECT,
        null=True, blank=True, related_name="plot_revisions",
    )
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="superseded_by",
        help_text="The previously published revision (null = original survey).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "revision_no"]
        unique_together = [("plot", "revision_no")]
        constraints = [
            # Exactly one OPEN (draft/reviewed/blocked) revision per plot:
            # a blocked geometry must be re-validated in place, so competing
            # proposals can never pile up and compete at publication.
            models.UniqueConstraint(
                fields=["plot"],
                condition=~models.Q(status=REVISION_PUBLISHED),
                name="uniq_one_open_revision_per_plot",
            ),
        ]

    _FROZEN_STATUSES = (REVISION_PUBLISHED,)

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == REVISION_PUBLISHED:
                frozen = [
                    "boundary", "declared_area_ha", "area_polygon_ha",
                    "crs_epsg", "crs_note", "area_tolerance", "area_check",
                    "content_checksum", "original_boundary",
                    "original_declared_area_ha", "original_area_polygon_ha",
                    "original_crs_epsg", "plot_id", "revision_no",
                ]
                changed = [f for f in frozen
                           if getattr(original, f) != getattr(self, f)]
                if changed:
                    raise PermissionError(
                        f"PlotFrameRevision {self.plot.code} #"
                        f"{self.revision_no} is published; its boundary is "
                        f"frozen. Changed: {changed}. Propose a new revision.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == REVISION_PUBLISHED:
            raise PermissionError("published frame revisions cannot be deleted")
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.plot.code} revision #{self.revision_no} [{self.status}]"


class FrameIssue(models.Model):
    """
    A pending item raised while validating a PlotFrameRevision.

    Open issues force the revision into `blocked` and make publishing
    impossible; the geometry is never silently accepted and estimates are
    never computed from it. Issues are resolved only by re-validating a
    geometry on which the condition no longer holds (never hand-waved away),
    and the row is retained for the audit trail.
    """

    revision = models.ForeignKey(
        PlotFrameRevision, on_delete=models.PROTECT, related_name="issues"
    )
    kind = models.CharField(max_length=20, choices=FRAME_ISSUE_KIND_CHOICES)
    status = models.CharField(
        max_length=10, choices=FRAME_ISSUE_STATUS_CHOICES, default=ISSUE_OPEN
    )
    detail = models.CharField(max_length=600)
    payload = models.JSONField(
        default=dict, blank=True,
        help_text="Machine-readable detail: excluded stems, overlap area, "
                  "areas, etc.",
    )
    fingerprint = models.CharField(
        max_length=64,
        help_text="Stable hash of kind+payload so re-validation reuses rows.")
    resolution_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["revision__plot__code", "revision__revision_no", "id"]
        unique_together = [("revision", "fingerprint")]
