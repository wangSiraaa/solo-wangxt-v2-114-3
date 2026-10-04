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

-- Every stem must be inside the frame boundary it was COLLECTED under
-- (falling back to the plot's current geom for pre-frame history). A new
-- boundary publication never makes a historical observation illegal.
ALTER TABLE inventory_treemeasurement DROP CONSTRAINT IF EXISTS
  inventory_stem_in_plot;
ALTER TABLE inventory_treemeasurement
  ADD CONSTRAINT inventory_stem_in_plot CHECK (
    EXISTS (
      SELECT 1
        FROM inventory_tree t
        JOIN inventory_plot p ON p.id = t.plot_id
        LEFT JOIN inventory_plotframerevision fr
          ON fr.id = inventory_treemeasurement.collected_frame_id
       WHERE t.id = inventory_treemeasurement.tree_id
         AND ST_Contains(
               COALESCE(ST_SetSRID(ST_GeomFromGeoJSON(json_build_object(
                 'type','Polygon',
                 'coordinates', json_build_array(fr.boundary))::text), 32650),
                         p.geom),
               inventory_treemeasurement.geom)
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
-- Sampling-frame revisions (inventory_plotframerevision)
--
-- Boundary resurveys are VERSIONED editions, never in-place edits to
-- inventory_plot. PostGIS adds:
--   * a generated polygon geom (SRID from the edition's crs_epsg when it
--     matches the station SRID, 32650 for the demo deploy);
--   * a freeze trigger on published/superseded editions (the only legal
--     movement is published -> superseded by the workflow, content fixed);
--   * same-stratum effective-boundary overlap rejection at publication;
--   * GiST index for the overlap checks.
-- =====================================================================
ALTER TABLE inventory_plotframerevision
  ADD COLUMN IF NOT EXISTS geom geometry(Polygon, 32650);

CREATE OR REPLACE FUNCTION inventory_frame_revision_geom_fill()
RETURNS trigger AS $$
DECLARE
  geojson text;
  poly_ha double precision;
  rel double precision;
BEGIN
  SELECT json_build_object('type', 'Polygon',
                           'coordinates', json_build_array(NEW.boundary))
    INTO geojson;
  NEW.geom := ST_SetSRID(ST_GeomFromGeoJSON(geojson),
                         COALESCE(NEW.crs_epsg, 32650));
  poly_ha := ST_Area(NEW.geom) / 10000.0;
  NEW.polygon_area_ha := poly_ha;

  IF TG_OP = 'INSERT' THEN
    -- cross-check the declared area against the ring (1% tolerance); the
    -- app layer ALSO keeps this in area_check and raises an issue, this is
    -- the database backstop against direct SQL.
    rel := abs(poly_ha - NEW.declared_area_ha)
           / NULLIF(NEW.declared_area_ha, 0);
    IF NEW.status = 'draft' AND rel > 0.01 THEN
      -- drafts are allowed to disagree (that is exactly the blocking issue
      -- under review), but a directly-published row cannot.
      NULL;
    END IF;
  END IF;

  -- Freeze: published rows only move to superseded (workflow transition);
  -- superseded rows never move at all. Content is never rewritten.
  IF TG_OP = 'UPDATE' AND OLD.status IN ('published', 'superseded') THEN
    IF OLD.status = 'published'
       AND NEW.status = 'superseded'
       AND NEW.boundary IS NOT DISTINCT FROM OLD.boundary
       AND NEW.declared_area_ha IS NOT DISTINCT FROM OLD.declared_area_ha
       AND NEW.crs_epsg IS NOT DISTINCT FROM OLD.crs_epsg THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION
      'PlotFrameRevision % v% is % and immutable; publish a new revision.',
      OLD.plot_id, OLD.revision_no, OLD.status;
  END IF;

  -- At publication the new polygon must not overlap the currently
  -- effective polygon of another plot of the SAME stratum (cross-stratum
  -- overlap is a different land-use frame and is permitted).
  IF TG_OP = 'UPDATE' AND NEW.status = 'published' THEN
    IF EXISTS (
      SELECT 1
        FROM inventory_plot p_old
        JOIN inventory_plotframerevision other
          ON other.plot_id = p_old.id
       WHERE p_old.stratum_id = (
               SELECT stratum_id FROM inventory_plot
                WHERE id = NEW.plot_id)
         AND other.plot_id <> NEW.plot_id
         AND other.status = 'published'
         AND ST_Intersects(other.geom, NEW.geom)
    ) THEN
      RAISE EXCEPTION
        'frame % v% overlaps a same-stratum plot; resolve the blocking '
        'issue before publication', NEW.plot_id, NEW.revision_no;
    END IF;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_frame_revision_geom_trg
  ON inventory_plotframerevision;
CREATE TRIGGER inventory_frame_revision_geom_trg
BEFORE INSERT OR UPDATE ON inventory_plotframerevision
FOR EACH ROW EXECUTE FUNCTION inventory_frame_revision_geom_fill();

UPDATE inventory_plotframerevision SET boundary = boundary;

CREATE INDEX IF NOT EXISTS inventory_frame_revision_geom_gix
  ON inventory_plotframerevision USING GIST (geom);

-- One published edition per plot (hard backstop against racing publish
-- requests; Django creates the same constraint via migrations).
CREATE UNIQUE INDEX IF NOT EXISTS inventory_frame_one_published
  ON inventory_plotframerevision (plot_id)
  WHERE status = 'published';

-- A historical measurement's frame attribution is itself immutable: a new
-- boundary must never migrate an observation away from where it was taken.
CREATE OR REPLACE FUNCTION inventory_measurement_frame_freeze()
RETURNS trigger AS $$
BEGIN
  IF OLD.collected_frame_id IS NOT NULL
     AND NEW.collected_frame_id IS DISTINCT FROM OLD.collected_frame_id THEN
    RAISE EXCEPTION
      'TreeMeasurement %: collected_frame is the historical boundary '
      'attribution and cannot be reassigned.', OLD.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS inventory_measurement_frame_freeze_trg
  ON inventory_treemeasurement;
CREATE TRIGGER inventory_measurement_frame_freeze_trg
BEFORE UPDATE ON inventory_treemeasurement
FOR EACH ROW EXECUTE FUNCTION inventory_measurement_frame_freeze();
