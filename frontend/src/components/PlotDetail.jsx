import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * One plot: boundary + every individual's t1 -> t2 remeasurement.
 * Identity is the internal tree row; labels shown are what was on the tag.
 *
 * Boundary view switches between the ORIGINAL survey polygon and the latest
 * PUBLISHED sampling-frame polygon for this plot. A stem outside the viewed
 * polygon is highlighted (excluded by a revision) — its measurement row is
 * never moved; it stays attributed to the boundary in force at collection.
 */
export default function PlotDetail({ plotCode, ctx, onBack }) {
  const { plots, m1, m2, t1, t2, frameVersion } = ctx;
  const plot = plots.find((p) => p.code === plotCode);
  const [revisions, setRevisions] = useState([]);
  const [view, setView] = useState("original");

  useEffect(() => {
    api.revisions(plotCode).then(setRevisions).catch(() => {});
  }, [plotCode]);

  const rows = useMemo(() => {
    const a = m1.filter((m) => m.plot_code === plotCode);
    const b = m2.filter((m) => m.plot_code === plotCode);
    const byTree = new Map();
    a.forEach((m) => byTree.set(m.tree, { t1: m }));
    b.forEach((m) => {
      const cur = byTree.get(m.tree) || {};
      cur.t2 = m;
      byTree.set(m.tree, cur);
    });
    return [...byTree.values()].sort((x, y) => {
      const nx = (x.t2 || x.t1).field_number;
      const ny = (y.t2 || y.t1).field_number;
      return nx.localeCompare(ny);
    });
  }, [m1, m2, plotCode]);

  const frameEntry = frameVersion?.plot_payload?.[plotCode];
  const published = revisions.filter((r) => r.status === "published");
  const boundary = view === "revised" && frameEntry
    ? frameEntry.boundary : plot?.boundary;
  const shownArea = view === "revised" && frameEntry
    ? frameEntry.declared_area_ha : plot?.declared_area_ha;
  const shownPoly = view === "revised" && frameEntry
    ? frameEntry.area_polygon_ha : plot?.area_polygon_ha;
  const isRevised = !!frameEntry?.revision_id;

  if (!plot || !boundary) return null;
  const xs = [];
  const ys = [];
  plots.forEach((p) => {
    const e = frameVersion?.plot_payload?.[p.code];
    if (e?.boundary) e.boundary.forEach(([x, y]) => { xs.push(x); ys.push(y); });
  });
  plot.boundary.forEach(([x, y]) => { xs.push(x); ys.push(y); });
  if (frameEntry?.boundary)
    frameEntry.boundary.forEach(([x, y]) => { xs.push(x); ys.push(y); });
  const W = 640, H = 420, PAD = 40;
  const sx = (x) => PAD + (x - Math.min(...xs)) /
    (Math.max(...xs) - Math.min(...xs)) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - Math.min(...ys)) /
    (Math.max(...ys) - Math.min(...ys)) * (H - 2 * PAD);

  function inside(x, y, ring) {
    let inb = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > y) !== (yj > y) &&
          x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi) {
        inb = !inb;
      }
    }
    return inb;
  }

  function rowClass(r) {
    if (r.t1 && r.t2) {
      if (r.t2.status === "dead") return "mortality";
      if (r.t2.status === "missing_tree") return "missing";
      if (r.t2.status === "alive_not_measured") return "notmeasured";
      const d = Math.abs((r.t2.dbh_cm ?? 0) - (r.t1.dbh_cm ?? 0));
      if (d <= 0.15) return "zero";
      return "growth";
    }
    return r.t2 ? "ingrowth" : "lost";
  }

  return (
    <div>
      <button className="back" onClick={onBack}>← all plots</button>
      <h2>Plot {plot.code}
        <small> {shownArea} ha · stratum {plot.stratum_code}
          {" "}· polygon {shownPoly?.toFixed(4)} ha
          {isRevised && view === "revised" && " · frame-revised"}</small>
      </h2>

      <div className="frame-toggle">
        <span>Boundary view:</span>
        <button className={view === "original" ? "toggle active" : "toggle"}
                onClick={() => setView("original")}>original survey</button>
        <button className={view === "revised" ? "toggle active" : "toggle"}
                onClick={() => setView("revised")}
                disabled={!isRevised}>
          published frame v{frameVersion?.version ?? "–"}
        </button>
        <span className="hint">
          {published.length} published revision(s)
        </span>
      </div>

      <div className="detail-grid">
        <svg viewBox={`0 0 ${W} ${H}`} className="plot-map">
          {/* always show the other boundary faintly for comparison */}
          {isRevised && view === "original" &&
            <polygon points={frameEntry.boundary
              .map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
              className="boundary-revised ghost" />}
          {isRevised && view === "revised" &&
            <polygon points={plot.boundary
              .map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
              className="boundary-original ghost" />}
          <polygon
            points={boundary.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
            className={isRevised && view === "revised"
              ? "boundary boundary-revised" : "boundary boundary-original"} />
          {rows.map((r) => {
            const t1m = r.t1, t2m = r.t2;
            if (t1m && t2m) {
              const excluded = view === "revised"
                && (!inside(t1m.x_m, t1m.y_m, boundary)
                    || !inside(t2m.x_m, t2m.y_m, boundary));
              return (
                <g key={t1m.tree + (t2m?.id ?? "")}>
                  <line x1={sx(t1m.x_m)} y1={sy(t1m.y_m)}
                        x2={sx(t2m.x_m)} y2={sy(t2m.y_m)}
                        className="move-line" />
                  <circle cx={sx(t2m.x_m)} cy={sy(t2m.y_m)} r={6}
                          className={`stem ${rowClass(r)}${
                            excluded ? " stem-excluded" : ""}`} />
                  {excluded &&
                    <text x={sx(t2m.x_m)} y={sy(t2m.y_m) + 3}
                          className="excluded-cross">×</text>}
                </g>
              );
            }
            const m = t2m || t1m;
            const excluded = view === "revised"
              && !inside(m.x_m, m.y_m, boundary);
            return (
              <g key={m.id}>
                <circle cx={sx(m.x_m)} cy={sy(m.y_m)} r={6}
                        className={`stem ${rowClass(r)}${
                          excluded ? " stem-excluded" : ""}`} />
                {excluded &&
                  <text x={sx(m.x_m)} y={sy(m.y_m) + 3}
                        className="excluded-cross">×</text>}
              </g>
            );
          })}
        </svg>

        <table className="tree-table">
          <thead>
            <tr>
              <th>tag t1 → t2</th>
              <th>dbh {t1} cm</th>
              <th>dbh {t2} cm</th>
              <th>Δ dbh</th>
              <th>status / source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const label1 = r.t1?.field_number ?? "—";
              const label2 = r.t2?.field_number ?? "—";
              const d1 = r.t1?.dbh_cm;
              const d2 = r.t2?.dbh_cm;
              const delta = (d1 != null && d2 != null)
                ? (d2 - d1).toFixed(2) : "—";
              const cls = rowClass(r);
              const source = {
                growth: "survivor growth",
                zero: "VERIFIED zero growth (measured, cross-checked)",
                notmeasured: "alive but NOT measured — missing, ratio-imputed",
                mortality: "MORTALITY (dead observation)",
                missing: "not located at t2",
                ingrowth: d2 >= 5 ? "INGROWTH ≥ 5 cm"
                                  : "below recruitment — excluded",
                lost: "t1 only",
              }[cls];
              const probe = r.t2 || r.t1;
              const outsideNow = isRevised
                && !inside(probe.x_m, probe.y_m,
                           view === "revised" ? frameEntry.boundary
                                              : plot.boundary);
              const outsideViewed = isRevised && view === "revised"
                && !inside(probe.x_m, probe.y_m, boundary);
              return (
                <tr key={(r.t1 || r.t2).tree}
                    className={cls + (outsideViewed ? " row-excluded" : "")}>
                  <td>{label1}{label1 !== label2 ? ` → ${label2}` : ""}
                    {label1 !== label2 &&
                      <span className="renumber-badge"> renumber</span>}
                    {isRevised && outsideNow &&
                      <span className="excluded-badge">
                        {" "}outside frame v{frameVersion.version}
                      </span>}
                  </td>
                  <td>{d1 ?? "—"}
                    {r.t1?.dbh_unit && r.t1.dbh_unit !== "cm" &&
                      <small> ({r.t1.dbh_raw} {r.t1.dbh_unit})</small>}
                  </td>
                  <td>{d2 ?? "—"}
                    {r.t2?.dbh_unit && r.t2.dbh_unit !== "cm" &&
                      <small> ({r.t2.dbh_raw} {r.t2.dbh_unit})</small>}
                  </td>
                  <td>{delta}</td>
                  <td>{source}
                    {outsideViewed &&
                      <div className="bad-text">
                        × excluded by viewed boundary — historical record
                        retained, attributed to original
                      </div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {revisions.length > 0 && (
        <details className="revision-history">
          <summary>Frame revision history ({revisions.length})</summary>
          <ul>
            {revisions.map((r) => (
              <li key={r.id}>
                #{r.revision_no} <strong>{r.status}</strong> ·
                {" "}{r.original_area_polygon_ha.toFixed(4)} ha →
                {" "}{r.area_polygon_ha.toFixed(4)} ha ·
                EPSG:{r.crs_epsg}
                {r.published_at &&
                  ` · published ${new Date(r.published_at).toLocaleString()}`}
                {r.reason ? ` · “${r.reason}”` : ""}
                {r.open_issue_count > 0 &&
                  ` · ${r.open_issue_count} open pending item(s)`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
