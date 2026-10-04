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

# Sampling-frame revision lifecycle. A resurveyed boundary NEVER overwrites
# the already-published frame in place: every edition of boundary + declared
# area + CRS statement is its own immutable row, and estimates pin the frame
# edition they were expanded against.
FRAME_DRAFT = "draft"
FRAME_REVIEWED = "reviewed"
FRAME_PUBLISHED = "published"
FRAME_SUPERSEDED = "superseded"
FRAME_STATUS_CHOICES = [
    (FRAME_DRAFT, "Draft — resurveyed boundary under check"),
    (FRAME_REVIEWED, "Reviewed — human QA done, awaiting publication"),
    (FRAME_PUBLISHED, "Published — the current sampling frame; immutable"),
    (FRAME_SUPERSEDED,
     "Superseded — a previously published edition, retained as history"),
]

# Blocking findings raised while checking a proposed frame. Each one is a
# work item that MUST be resolved by a human before publication; the system
# never publishes past them silently.
ISSUE_EXCLUDED_TREE = "excluded_tree"
ISSUE_AREA_MISMATCH = "area_mismatch"
ISSUE_OVERLAP = "overlap"
ISSUE_CRS = "crs_mismatch"
FRAME_ISSUE_KIND_CHOICES = [
    (ISSUE_EXCLUDED_TREE, "New boundary excludes an existing stem position"),
    (ISSUE_AREA_MISMATCH, "Declared area vs polygon area beyond tolerance"),
    (ISSUE_OVERLAP, "Overlaps another plot of the same stratum"),
    (ISSUE_CRS, "CRS statement differs from the station survey CRS"),
]
ISSUE_OPEN = "open"
ISSUE_RESOLVED = "resolved"
FRAME_ISSUE_STATUS_CHOICES = [
    (ISSUE_OPEN, "Open — blocks publication"),
    (ISSUE_RESOLVED, "Resolved by a documented human decision"),
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

    # Frame edition in force WHEN this observation was collected. Set at
    # ingest from the plot's then-published frame (null for pre-frame
    # history). A newly published boundary never mutates this value: the
    # measurement keeps belonging to the boundary it was taken under — it is
    # never migrated, deleted or rewritten by a revision.
    collected_frame = models.ForeignKey(
        "PlotFrameRevision", on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="measurements_collected",
        help_text="Published frame edition in force when this was observed.",
    )

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
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    equations = models.ManyToManyField(AllometricEquation, related_name="estimates")
    # The exact sampling-frame edition per plot this run was expanded
    # against. An estimate can NEVER be run without pinning frame editions:
    # the per-hectare expansion uses the area recorded on that edition.
    frames = models.ManyToManyField(
        "PlotFrameRevision", related_name="estimates",
        help_text="Plot frame editions this estimate is explicitly bound to.",
    )
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


class PlotFrameRevision(models.Model):
    """
    One edition of a plot's sampling-frame statement.

    Each resurvey produces a NEW row carrying boundary ring, declared area
    and an explicit CRS statement. The lifecycle is draft -> reviewed ->
    published; published rows are frozen (and at most one published edition
    may exist per plot — a newer publication supersedes the older one rather
    than editing it).

    Important non-effects of publication:
      * the already-published frame of OTHER plots never changes;
      * historical TreeMeasurement rows keep ``collected_frame`` pointing at
        the boundary they were taken under — never migrated or rewritten;
      * an EstimateVersion is bound to explicit frame editions, so a
        confirmed estimate keeps expanding with the areas it was run on.

    A proposed edition that excludes existing stems, whose declared area
    disagrees with its polygon beyond tolerance, or whose polygon overlaps a
    same-stratum plot carries open FrameRevisionIssues and cannot be
    published until a human resolves each one.
    """

    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="frame_revisions"
    )
    revision_no = models.PositiveIntegerField(
        help_text="1-based edition number per plot."
    )
    status = models.CharField(
        max_length=10, choices=FRAME_STATUS_CHOICES, default=FRAME_DRAFT
    )
    # Proposed resurvey values. Declared area is the station's (possibly
    # corrected) declaration; polygon area is computed from the ring and the
    # two are cross-checked, never silently substituted for each other.
    declared_area_ha = models.FloatField()
    boundary = models.JSONField(
        help_text="Proposed boundary ring [[x_m, y_m], ...] in crs_epsg."
    )
    polygon_area_ha = models.FloatField(
        help_text="Shoelace area of boundary in ha at draft creation."
    )
    area_check = models.JSONField(
        default=dict,
        help_text="Area cross-check result: declared/polygon/relative diff/"
                  "tolerance/passed.",
    )
    crs_epsg = models.IntegerField(
        help_text="Explicit CRS statement for this resurvey (EPSG code)."
    )
    crs_note = models.CharField(
        max_length=240, blank=True,
        help_text="Surveyor/CRS explanation travelling with this edition.",
    )
    geometry_checksum = models.CharField(
        max_length=64,
        help_text="SHA-256 of the canonical proposed ring. Identifies same "
                  "geometry across editions (old/new comparison).",
    )
    content_checksum = models.CharField(
        max_length=64,
        help_text="SHA-256 of ring + declared area + CRS statement. An "
                  "identical re-upload is idempotent (returns this edition); "
                  "a same-ring area/CRS correction is a NEW edition.",
    )
    reason = models.CharField(
        max_length=400, blank=True,
        help_text="Why this revision exists (boundary resurvey reason)."
    )
    validation_payload = models.JSONField(
        default=dict,
        help_text="Full check output: area, CRS, excluded stems, overlaps.",
    )
    # The frame edition this edition would supersede when published.
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="superseded_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=400, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    publication_reason = models.CharField(
        max_length=400, blank=True,
        help_text="Mandatory documented reason for publication.",
    )

    class Meta:
        ordering = ["plot__code", "-revision_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["plot", "revision_no"],
                name="frame_revision_no_unique_per_plot",
            ),
            # Re-uploading the same geometry + same area/CRS statement must
            # find — and only ever find — one edition. A same-ring but
            # different declared area or CRS is a distinct revision.
            models.UniqueConstraint(
                fields=["plot", "content_checksum"],
                name="frame_content_checksum_unique_per_plot",
            ),
            # At most one published edition per plot. Enforced in the DB so
            # two racing publish requests cannot both succeed.
            models.UniqueConstraint(
                fields=["plot"],
                condition=models.Q(status=FRAME_PUBLISHED),
                name="frame_one_published_per_plot",
            ),
        ]

    _FROZEN_STATUSES = (FRAME_PUBLISHED, FRAME_SUPERSEDED)

    def save(self, *args, _frame_system=False, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status in self._FROZEN_STATUSES:
                immutable = (
                    "declared_area_ha", "boundary", "polygon_area_ha",
                    "area_check", "crs_epsg", "crs_note", "geometry_checksum",
                    "content_checksum", "reason", "validation_payload",
                    "supersedes_id",
                )
                changed = [f for f in immutable
                           if getattr(original, f) != getattr(self, f)]
                # The ONLY legal movement of a published row is the frame
                # workflow retiring it to "superseded" during publication of
                # a newer edition (system transition, content untouched).
                allowed_transition = (
                    _frame_system
                    and original.status == FRAME_PUBLISHED
                    and self.status == FRAME_SUPERSEDED
                    and not changed
                )
                if changed or not allowed_transition:
                    raise PermissionError(
                        f"PlotFrameRevision {self.plot.code} "
                        f"v{self.revision_no} is {original.status} and "
                        f"immutable; illegal changes "
                        f"{changed or ['status']}. Publish a new revision "
                        "instead."
                    )
        super().save(*args, **kwargs)

    @property
    def has_open_blocking_issues(self):
        return self.issues.filter(status=ISSUE_OPEN).exists()

    def __str__(self):
        return f"{self.plot.code} frame v{self.revision_no} [{self.status}]"


class FrameRevisionIssue(models.Model):
    """
    A blocking finding on a proposed frame edition.

    Issues are raised at draft creation / re-validation and persist as
    auditable work items. Every OPEN issue blocks publication; a documented
    human decision (resolve note) is the only way past one — publication
    never recomputes them away.
    """

    revision = models.ForeignKey(
        PlotFrameRevision, on_delete=models.CASCADE, related_name="issues"
    )
    kind = models.CharField(max_length=20, choices=FRAME_ISSUE_KIND_CHOICES)
    detail = models.JSONField(
        default=dict,
        help_text="Structured finding (tree label/coords, areas, other "
                  "plot code, overlap fraction, CRS codes…).",
    )
    summary = models.CharField(max_length=400)
    status = models.CharField(
        max_length=10, choices=FRAME_ISSUE_STATUS_CHOICES, default=ISSUE_OPEN
    )
    resolution_note = models.CharField(max_length=400, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # One work item per finding target on an edition (re-runs of
            # validation upsert rather than duplicating).
            models.UniqueConstraint(
                fields=["revision", "kind", "summary"],
                name="frame_issue_unique_per_revision_kind_target",
            ),
        ]

    def __str__(self):
        return f"v{self.revision_id} {self.kind}: {self.summary[:40]}"
