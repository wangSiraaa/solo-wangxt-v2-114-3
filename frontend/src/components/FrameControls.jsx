import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * Sampling-frame boundary switching shared by the map and plot detail.
 *
 * Views:
 *   current      — the published frame in force (Plot row)
 *   new          — the newest draft/reviewed edition per plot
 *   old          — the edition the new one supersedes
 *
 * The component also fetches each proposed edition's impact report so the
 * map/detail can highlight stems the new boundary excludes. Historical
 * observations are never moved: the impact report classifies their ORIGINAL
 * positions against the proposed ring (read-only).
 */

export function frameChoicesForPlot(plot, revisions) {
  const rs = (revisions || [])
    .filter((r) => r.plot === plot.id || r.plot_code === plot.code)
    .sort((a, b) => b.revision_no - a.revision_no);
  const proposed = rs.find((r) => r.status === "draft"
    || r.status === "reviewed");
  return { all: rs, proposed };
}

export function useFrameImpact(revisionId) {
  const [impact, setImpact] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    if (!revisionId) {
      setImpact(null);
      return undefined;
    }
    api.impactFrame(revisionId)
      .then((d) => alive && setImpact(d))
      .catch((e) => alive && setErr(e.message));
    return () => { alive = false; };
  }, [revisionId]);
  return { impact, err };
}

/**
 * Returns {boundary, area, label, revisionNo, status, excludedIds} for the
 * requested view on one plot.
 */
export function displayedBoundary(plot, revisions, view, impactByRevision) {
  const { all: rs, proposed } = frameChoicesForPlot(plot, revisions);
  const fallback = {
    boundary: plot.boundary, area: plot.declared_area_ha,
    revisionNo: plot.current_frame_revision_no, status: "published",
    label: `current frame v${plot.current_frame_revision_no ?? "?"}`,
    excludedIds: new Set(),
  };
  if (view === "new" && proposed) {
    const impact = impactByRevision?.[proposed.id];
    return {
      boundary: proposed.boundary,
      area: proposed.declared_area_ha,
      revisionNo: proposed.revision_no, status: proposed.status,
      label: `proposed v${proposed.revision_no} (${proposed.status})`,
      proposedId: proposed.id,
      excludedIds: new Set((impact?.affected?.excluded || [])
        .map((e) => e.measurement_id)),
      hasOpenIssues: (proposed.open_issue_count ?? 0) > 0,
    };
  }
  if (view === "old" && proposed) {
    const older = rs.find((r) => r.status === "published")
      || rs.find((r) => (r.status === "superseded"));
    if (older) {
      return {
        boundary: older.boundary, area: older.declared_area_ha,
        revisionNo: older.revision_no, status: older.status,
        label: `superseded/current v${older.revision_no}`,
        excludedIds: new Set(),
      };
    }
  }
  return fallback;
}

export default function BoundaryViewSwitch({ view, setView, hasProposed }) {
  const modes = [
    ["current", "Current (published)"],
    ["new", "New boundary (draft/reviewed)"],
    ["old", "Old boundary"],
  ];
  return (
    <span className="frame-switch" role="group"
          aria-label="boundary frame view">
      <strong>Boundary:</strong>
      {modes.map(([key, label]) => (
        <button key={key}
                className={`mini-tab ${view === key ? "active" : ""} ${
                  key !== "current" && !hasProposed ? "disabled" : ""}`}
                disabled={key !== "current" && !hasProposed}
                onClick={() => setView(key)}>
          {label}
        </button>
      ))}
    </span>
  );
}
