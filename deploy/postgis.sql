-- =====================================================================
-- PostGIS layer for the permanent-plot station.
--
-- The Django models store projected coordinates as plain (x_m, y_m) and
-- the plot boundary as a JSON ring so the identical schema runs on stock
-- sqlite3 for development. On PostgreSQL/PostGIS (settings
-- FOREST_DB=postgis) apply this script AFTER `manage.py migrate`:
--
--   createdb foreststation
--   psql -d foreststation -c "CREATE EXTENSION postgis;"
--   ./manage.py migrate
--   psql -d foreststation -f deploy/postgis.sql
--
-- CRS is FOREST_CRS_EPSG (default EPSG:32650 = UTM zone 50N). Change the
-- SRID below if you change that setting.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---- plots: trigger-maintained polygon geometry + area cross-check -----
ALTER TABLE inventory_plot
  ADD COLUMN IF NOT EXISTS geom geometry(Polygon, 32650);

CREATE OR REPLACE FUNCTION inventory_plot_geom_fill()
RETURNS trigger AS $$
DECLARE
  geojson text;
BEGIN
  SELECT json_build_object('type', 'Polygon',
                           'coordinates', json_build_array(NEW.boundary))
    INTO geojson;
  NEW.geom := ST_SetSRID(ST_GeomFromGeoJSON(geojson), 32650);

  -- polygon area and declared area must agree within 1%
  IF NEW.declared_area_ha > 0
     AND abs(ST_Area(NEW.geom) / 10000.0 - NEW.declared_area_ha)
         > 0.01 * NEW.declared_area_ha THEN
    RAISE EXCEPTION
      'plot %: polygon area % ha disagrees with declared % ha (>1%%)',
      NEW.code, ST_Area(NEW.geom) / 10000.0, NEW.declared_area_ha;
  END IF;
  NEW.area_polygon_ha := ST_Area(NEW.geom) / 10000.0;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_plot_geom_trigger ON inventory_plot;
CREATE TRIGGER inventory_plot_geom_trigger
BEFORE INSERT OR UPDATE ON inventory_plot
FOR EACH ROW EXECUTE FUNCTION inventory_plot_geom_fill();

-- backfill existing rows (triggers fire on UPDATE)
UPDATE inventory_plot SET boundary = boundary;

CREATE INDEX IF NOT EXISTS inventory_plot_geom_gix
  ON inventory_plot USING GIST (geom);

-- ---- tree measurements: point geometry ---------------------------------
ALTER TABLE inventory_treemeasurement
  ADD COLUMN IF NOT EXISTS geom geometry(Point, 32650);

CREATE OR REPLACE FUNCTION inventory_tree_meas_geom_fill()
RETURNS trigger AS $$
BEGIN
  NEW.geom := ST_SetSRID(ST_MakePoint(NEW.x_m, NEW.y_m), 32650);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_tree_meas_geom_trigger
  ON inventory_treemeasurement;
CREATE TRIGGER inventory_tree_meas_geom_trigger
BEFORE INSERT OR UPDATE ON inventory_treemeasurement
FOR EACH ROW EXECUTE FUNCTION inventory_tree_meas_geom_fill();

UPDATE inventory_treemeasurement SET x_m = x_m;

CREATE INDEX IF NOT EXISTS inventory_treemeasurement_geom_gix
  ON inventory_treemeasurement USING GIST (geom);
CREATE INDEX IF NOT EXISTS inventory_meas_campaign_label_idx
  ON inventory_treemeasurement (campaign_id, field_number_seen);

-- Every stem must be inside ITS OWN plot boundary.
ALTER TABLE inventory_treemeasurement DROP CONSTRAINT IF EXISTS
  inventory_stem_in_plot;
ALTER TABLE inventory_treemeasurement
  ADD CONSTRAINT inventory_stem_in_plot CHECK (
    EXISTS (
      SELECT 1
        FROM inventory_tree t
        JOIN inventory_plot p ON p.id = t.plot_id
       WHERE t.id = inventory_treemeasurement.tree_id
         AND ST_Contains(p.geom, inventory_treemeasurement.geom)
    )
  );

-- ---- unit integrity (defence in depth; the app layer checks first) -----
ALTER TABLE inventory_treemeasurement
  DROP CONSTRAINT IF EXISTS inventory_dbh_unit_known;
ALTER TABLE inventory_treemeasurement
  ADD CONSTRAINT inventory_dbh_unit_known
  CHECK (dbh_raw IS NULL OR dbh_unit IN ('cm', 'mm', 'in'));

ALTER TABLE inventory_treemeasurement
  DROP CONSTRAINT IF EXISTS inventory_height_unit_m;
ALTER TABLE inventory_treemeasurement
  ADD CONSTRAINT inventory_height_unit_m
  CHECK (height_raw IS NULL OR height_unit = 'm');

ALTER TABLE inventory_treemeasurement
  DROP CONSTRAINT IF EXISTS inventory_dbh_canonical_range;
ALTER TABLE inventory_treemeasurement
  ADD CONSTRAINT inventory_dbh_canonical_range
  CHECK (dbh_cm IS NULL OR (dbh_cm >= 1 AND dbh_cm <= 200));

-- ---- confirmed editions are immutable, even with direct SQL ------------
CREATE OR REPLACE FUNCTION inventory_estimate_version_freeze()
RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'confirmed'
     AND (NEW.status IS DISTINCT FROM 'confirmed'
          OR NEW.result_payload IS DISTINCT FROM OLD.result_payload
          OR NEW.equation_checksum IS DISTINCT FROM OLD.equation_checksum
          OR NEW.design_snapshot IS DISTINCT FROM OLD.design_snapshot) THEN
    RAISE EXCEPTION
      'EstimateVersion % is confirmed/frozen; create a new version.',
      OLD.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_estimate_version_freeze_trg
  ON inventory_estimateversion;
CREATE TRIGGER inventory_estimate_version_freeze_trg
BEFORE UPDATE ON inventory_estimateversion
FOR EACH ROW EXECUTE FUNCTION inventory_estimate_version_freeze();

-- ---- equations referenced by confirmed editions are locked -------------
CREATE OR REPLACE FUNCTION inventory_equation_freeze()
RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'confirmed'
     AND (NEW.a IS DISTINCT FROM OLD.a OR NEW.b IS DISTINCT FROM OLD.b
          OR NEW.c IS DISTINCT FROM OLD.c
          OR NEW.residual_sigma IS DISTINCT FROM OLD.residual_sigma
          OR NEW.dbh_min_cm IS DISTINCT FROM OLD.dbh_min_cm
          OR NEW.dbh_max_cm IS DISTINCT FROM OLD.dbh_max_cm) THEN
    RAISE EXCEPTION
      'Equation % v% locked by confirmed estimate; issue a new version.',
      OLD.code, OLD.version;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_equation_freeze_trg
  ON inventory_allometricequation;
CREATE TRIGGER inventory_equation_freeze_trg
BEFORE UPDATE ON inventory_allometricequation
FOR EACH ROW EXECUTE FUNCTION inventory_equation_freeze();

-- =====================================================================
-- Sampling-frame revisions
--
-- A re-surveyed boundary NEVER silently rewrites the published frame:
-- PlotFrameRevision rows are immutable once published and publish emits an
-- append-only SamplingFrameVersion. Historical TreeMeasurement rows keep
-- their coordinates (the stem-in-plot CHECK below references inventory_plot,
-- which retains the ORIGINAL survey polygon); a revision excludes or
-- includes nothing in the measurement table, it only raises a pending item.
-- =====================================================================

-- published plot-frame revisions: geometry/area/CRS frozen, no DELETE
CREATE OR REPLACE FUNCTION inventory_plot_revision_freeze()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND OLD.status = 'published' THEN
    RAISE EXCEPTION
      'PlotFrameRevision %/% is published and cannot be deleted.',
      OLD.plot_id, OLD.revision_no;
  END IF;
  IF OLD.status = 'published'
     AND (NEW.boundary IS DISTINCT FROM OLD.boundary
          OR NEW.declared_area_ha IS DISTINCT FROM OLD.declared_area_ha
          OR NEW.area_polygon_ha IS DISTINCT FROM OLD.area_polygon_ha
          OR NEW.crs_epsg IS DISTINCT FROM OLD.crs_epsg
          OR NEW.original_boundary IS DISTINCT FROM OLD.original_boundary
          OR NEW.original_declared_area_ha IS DISTINCT
             FROM OLD.original_declared_area_ha) THEN
    RAISE EXCEPTION
      'PlotFrameRevision %/% is published; propose a new revision instead.',
      OLD.plot_id, OLD.revision_no;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_plot_revision_freeze_trg
  ON inventory_plotframerevision;
CREATE TRIGGER inventory_plot_revision_freeze_trg
BEFORE UPDATE OR DELETE ON inventory_plotframerevision
FOR EACH ROW EXECUTE FUNCTION inventory_plot_revision_freeze();

-- only one published revision emits a frame version: frame versions are
-- append-only and never edited or deleted.
CREATE OR REPLACE FUNCTION inventory_frame_version_append_only()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION
      'SamplingFrameVersion % is append-only; publish a new revision instead.',
      OLD.version;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION
      'SamplingFrameVersion % cannot be deleted.', OLD.version;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_frame_version_append_only_trg
  ON inventory_samplingframeversion;
CREATE TRIGGER inventory_frame_version_append_only_trg
BEFORE UPDATE OR DELETE ON inventory_samplingframeversion
FOR EACH ROW EXECUTE FUNCTION inventory_frame_version_append_only();

-- a revision cannot be marked published without an emitted frame, and an
-- emitted frame always points back to a published revision.
ALTER TABLE inventory_plotframerevision
  DROP CONSTRAINT IF EXISTS inventory_revision_published_has_frame;
ALTER TABLE inventory_plotframerevision
  ADD CONSTRAINT inventory_revision_published_has_frame CHECK (
    status <> 'published' OR emitted_frame_id IS NOT NULL);

-- open frame issues keep their revision out of publication; the application
-- moves such a revision to 'blocked' (defence in depth: this CHECK forbids
-- publishing a revision that still carries an open issue).
CREATE OR REPLACE FUNCTION inventory_no_open_issues_when_published()
RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'published'
     AND EXISTS (SELECT 1 FROM inventory_frameissue i
                  WHERE i.revision_id = NEW.id AND i.status = 'open') THEN
    RAISE EXCEPTION
      'PlotFrameRevision %/% still has open pending items; cannot publish.',
      NEW.plot_id, NEW.revision_no;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_no_open_issues_publish_trg
  ON inventory_plotframerevision;
CREATE TRIGGER inventory_no_open_issues_publish_trg
BEFORE UPDATE OF status ON inventory_plotframerevision
FOR EACH ROW EXECUTE FUNCTION inventory_no_open_issues_when_published();
