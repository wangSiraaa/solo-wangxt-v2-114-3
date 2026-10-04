import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import BoundaryViewSwitch, { displayedBoundary } from "./FrameControls.jsx";

const STATUS_STYLE = {
  alive_measured: { color: "#2e7d32", label: "alive, measured" },
  alive_not_measured: { color: "#e6a700", label: "alive, NOT measured" },
  dead: { color: "#b71c1c", label: "dead" },
  missing_tree: { color: "#888", label: "not located" },
};

/**
 * SVG overview: all plot boundaries (to projected-metre scale) with the
 * remeasurement status of each individual at t2. Boundaries can be switched
 * between the current published frame and a proposed resurvey; individuals
 * a proposed boundary would exclude are ringed in magenta.
 */
export default function PlotMap({ ctx, onSelect }) {
  const { plots, m2, revisions, boundaryView, setBoundaryView } = ctx;
  const [impacts, setImpacts] = useState({});

  const proposedIds = useMemo(() => new Set(
    (revisions || [])
      .filter((r) => r.status === "draft" || r.status === "reviewed")
      .map((r) => r.id)), [revisions]);

  useEffect(() => {
    let alive = true;
    Promise.all([...proposedIds].map((id) =>
      api.impactFrame(id).then((d) => [id, d])
        .catch(() => [id, null])))
      .then((pairs) => alive && setImpacts(Object.fromEntries(
        pairs.filter(([, d]) => d))));
    return () => { alive = false; };
  }, [proposedIds]);

  const shown = useMemo(() => {
    const map = {};
    plots.forEach((p) => {
      map[p.code] = displayedBoundary(p, revisions, boundaryView, impacts);
    });
    return map;
  }, [plots, revisions, boundaryView, impacts]);

  const bounds = useMemo(() => {
    const xs = [], ys = [];
    Object.values(shown).forEach((s) => s.boundary.forEach(([x, y]) => {
      xs.push(x); ys.push(y);
    }));
    if (!xs.length) return null;
    return { minX: Math.min(...xs), minY: Math.min(...ys),
             maxX: Math.max(...xs), maxY: Math.max(...ys) };
  }, [shown]);

  if (!bounds) return <p>No plots loaded.</p>;
  const W = 1000, H = 520, PAD = 60;
  const sx = (x) => PAD + (x - bounds.minX) /
    (bounds.maxX - bounds.minX) * (W - 2 * PAD);
  // flip Y so north is up
  const sy = (y) => H - PAD - (y - bounds.minY) /
    (bounds.maxY - bounds.minY) * (H - 2 * PAD);

  const m2ByPlot = {};
  m2.forEach((m) => (m2ByPlot[m.plot_code] ||= []).push(m));

  return (
    <div>
      <div className="legend">
        {Object.entries(STATUS_STYLE).map(([k, v]) => (
          <span key={k} className="legend-item">
            <span className="dot" style={{ background: v.color }} />
            {v.label}
          </span>
        ))}
        <span className="legend-item">
          <span className="excluded-ring">◯</span> excluded by proposed
          boundary
        </span>
        <BoundaryViewSwitch view={boundaryView} setView={setBoundaryView}
                            hasProposed={proposedIds.size > 0} />
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="map">
        {plots.map((p) => {
          const view = shown[p.code];
          const pts = view.boundary.map(
            ([x, y]) => `${sx(x)},${sy(y)}`).join(" ");
          return (
            <g key={p.code} className="plot-shape" onClick={() => onSelect(p.code)}>
              <polygon points={pts}
                       className={`boundary frame-${view.status}`} />
              {view.proposedId && (
                <polygon
                  points={plots.find((q) => q.code === p.code).boundary
                    .map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
                  className="boundary frame-current-hint" />
              )}
              <text x={sx(p.x_m)} y={sy(p.y_m)} className="plot-label">
                {p.code} · {view.area} ha · {p.stratum_code} · v{view.revisionNo}
              </text>
              {(m2ByPlot[p.code] || []).map((m) => {
                const s = STATUS_STYLE[m.status] || STATUS_STYLE.alive_measured;
                const excluded = view.excludedIds.has(m.id);
                return (
                  <circle key={m.id} cx={sx(m.x_m)} cy={sy(m.y_m)}
                          r={m.status === "dead" ? 7 : 5}
                          fill={excluded ? "#fff" : s.color}
                          className={excluded ? "stem-excluded" : ""}
                          stroke={excluded ? "#b0349f"
                            : m.status === "alive_not_measured"
                              ? "#000" : "#fff"}
                          strokeWidth={excluded ? 2.5
                            : m.status === "alive_not_measured" ? 1.5 : 1}>
                    <title>{`${p.code}/${m.field_number} — ${s.label}
	dbh ${m.dbh_cm ?? "—"} cm · h ${m.height_m ?? "—"} m${
  excluded ? "\nEXCLUDED by proposed boundary v" + view.revisionNo : ""}${
  m.collected_frame_revision
    ? `\ncollected on frame v${m.collected_frame_revision}` : ""}`}</title>
                  </circle>
                );
              })}
            </g>
          );
        })}
      </svg>
      <p className="hint">
        Circles are individuals at {ctx.t2}. Yellow = alive but not measured
        (missing, never zero); red = mortality observation; grey = not
        located. Magenta rings mark stems that a proposed new boundary would
        exclude — those historical observations stay on their original
        boundary and block publication until resolved. Click a plot for
        t1→t2 detail and the frame revision workbench.
      </p>
    </div>
  );
}
