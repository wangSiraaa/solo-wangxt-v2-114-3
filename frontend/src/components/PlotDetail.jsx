import React, { useMemo } from "react";
import FrameRevisionWorkbench from "./FrameRevisionWorkbench.jsx";
import { displayedBoundary, useFrameImpact } from "./FrameControls.jsx";

/**
 * One plot: boundary + every individual's t1 -> t2 remeasurement.
 * Identity is the internal tree row; labels shown are what was on the tag.
 * Boundary can be switched between the current published frame, a proposed
 * new edition and the old one; stems excluded by the proposal are marked.
 */
export default function PlotDetail({ plotCode, ctx, onBack }) {
  const { plots, m1, m2, t1, t2, revisions, boundaryView, setBoundaryView,
          refreshRevisions } = ctx;
  const plot = plots.find((p) => p.code === plotCode);

  const proposed = useMemo(() => (revisions || [])
    .filter((r) => (r.plot === plot?.id || r.plot_code === plotCode)
      && (r.status === "draft" || r.status === "reviewed"))
    .sort((a, b) => b.revision_no - a.revision_no)[0],
    [revisions, plot, plotCode]);
  const { impact } = useFrameImpact(proposed?.id);

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

  if (!plot) return null;
  const view = displayedBoundary(plot, revisions, boundaryView,
    proposed?.id ? { [proposed.id]: impact } : null);
  const ring = view.boundary;
  const xs = ring.map(([x]) => x);
  const ys = ring.map(([, y]) => y);
  const W = 640, H = 420, PAD = 40;
  const sx = (x) => PAD + (x - Math.min(...xs)) /
    (Math.max(...xs) - Math.min(...xs) || 1) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - Math.min(...ys)) /
    (Math.max(...ys) - Math.min(...ys) || 1) * (H - 2 * PAD);

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
        <small> frame v{view.revisionNo} ({view.status}) · {view.area} ha ·
          stratum {plot.stratum_code} · polygon
          {" "}{Number(view.status === "published"
            ? plot.area_polygon_ha : view.area).toFixed(4)} ha</small>
      </h2>

      <div className="detail-grid">
        <svg viewBox={`0 0 ${W} ${H}`} className="plot-map">
          <polygon
            points={ring.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
            className={`boundary frame-${view.status}`} />
          {boundaryView !== "current" && (
            <polygon
              points={plot.boundary.map(
                ([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
              className="boundary frame-current-hint" />
          )}
          {rows.map((r) => {
            const t1m = r.t1, t2m = r.t2;
            if (t1m && t2m) {
              const excluded = view.excludedIds.has(t2m.id);
              return (
                <g key={t1m.tree + (t2m?.id ?? "")}>
                  <line x1={sx(t1m.x_m)} y1={sy(t1m.y_m)}
                        x2={sx(t2m.x_m)} y2={sy(t2m.y_m)}
                        className="move-line" />
                  <circle cx={sx(t2m.x_m)} cy={sy(t2m.y_m)} r={6}
                          className={`stem ${rowClass(r)}${
                            excluded ? " stem-excluded-dot" : ""}`} />
                </g>
              );
            }
            const m = t2m || t1m;
            const excluded = view.excludedIds.has(m.id);
            return <circle key={m.id} cx={sx(m.x_m)} cy={sy(m.y_m)} r={6}
                           className={`stem ${rowClass(r)}${
                             excluded ? " stem-excluded-dot" : ""}`} />;
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
              <th>frame</th>
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
              const frameNo = r.t2?.collected_frame_revision
                ?? r.t1?.collected_frame_revision;
              return (
                <tr key={(r.t1 || r.t2).tree} className={cls}>
                  <td>{label1}{label1 !== label2 ? ` → ${label2}` : ""}
                    {label1 !== label2 &&
                      <span className="renumber-badge"> renumber</span>}
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
                  <td>{source}</td>
                  <td className={frameNo && frameNo !== view.revisionNo
                    ? "frame-old" : ""}>
                    v{frameNo ?? "?"}
                    {frameNo && frameNo !== view.revisionNo &&
                      <small> collected on older frame</small>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <FrameRevisionWorkbench
        plot={plot} revisions={revisions}
        boundaryView={boundaryView} setBoundaryView={setBoundaryView}
        onChanged={refreshRevisions} />
    </div>
  );
}
