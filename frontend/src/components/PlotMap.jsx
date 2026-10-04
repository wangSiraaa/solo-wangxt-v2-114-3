import React, { useMemo, useState } from "react";

const STATUS_STYLE = {
  alive_measured: { color: "#2e7d32", label: "alive, measured" },
  alive_not_measured: { color: "#e6a700", label: "alive, NOT measured" },
  dead: { color: "#b71c1c", label: "dead" },
  missing_tree: { color: "#888", label: "not located" },
};

/**
 * SVG overview: plot boundaries with the remeasurement status of each
 * individual at t2. Boundaries can be viewed under the ORIGINAL survey or the
 * latest published sampling frame; plots revised by the selected frame are
 * outlined and stems excluded by a revised boundary are flagged (the
 * historical measurement itself never moves).
 */
export default function PlotMap({ ctx, onSelect }) {
  const { plots, m2, frameVersion } = ctx;
  const [view, setView] = useState("latest"); // "latest" | "original"

  const frameByPlot = useMemo(() => {
    const m = {};
    const payload = frameVersion?.plot_payload || {};
    for (const p of plots) {
      const e = payload[p.code];
      if (e) m[p.code] = e;
    }
    return m;
  }, [plots, frameVersion]);

  // stems that fall outside the currently viewed boundary per plot
  const excluded = useMemo(() => {
    const set = new Set();
    if (view !== "latest") return set;
    for (const m of m2) {
      const e = frameByPlot[m.plot_code];
      if (!e || e.revision_id == null) continue;
      if (!inside(m.x_m, m.y_m, e.boundary)) set.add(m.id);
    }
    return set;
  }, [m2, frameByPlot, view]);

  const bounds = useMemo(() => {
    const xs = [], ys = [];
    plots.forEach((p) => {
      const e = view === "latest" ? frameByPlot[p.code] : null;
      (e?.boundary || p.boundary).forEach(([x, y]) => {
        xs.push(x); ys.push(y);
      });
    });
    if (!xs.length) return null;
    return { minX: Math.min(...xs), minY: Math.min(...ys),
             maxX: Math.max(...xs), maxY: Math.max(...ys) };
  }, [plots, frameByPlot, view]);

  if (!bounds) return <p>No plots loaded.</p>;
  const W = 1000, H = 520, PAD = 60;
  const sx = (x) => PAD + (x - bounds.minX) /
    (bounds.maxX - bounds.minX) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - bounds.minY) /
    (bounds.maxY - bounds.minY) * (H - 2 * PAD);

  const m2ByPlot = {};
  m2.forEach((m) => (m2ByPlot[m.plot_code] ||= []).push(m));

  const revisedCount = Object.values(frameByPlot)
    .filter((e) => e.revision_id != null).length;

  return (
    <div>
      <div className="frame-toggle">
        <span>Boundary view:</span>
        <button className={view === "original" ? "toggle active" : "toggle"}
                onClick={() => setView("original")}>
          original survey
        </button>
        <button className={view === "latest" ? "toggle active" : "toggle"}
                onClick={() => setView("latest")}>
          published frame v{frameVersion?.version ?? "–"}
        </button>
        <span className="chip">{revisedCount} revised plot(s)</span>
      </div>
      <div className="legend">
        {Object.entries(STATUS_STYLE).map(([k, v]) => (
          <span key={k} className="legend-item">
            <span className="dot" style={{ background: v.color }} />
            {v.label}
          </span>
        ))}
        <span className="legend-item">
          <span className="line-swatch original" /> original boundary
        </span>
        <span className="legend-item">
          <span className="line-swatch revised" /> revised boundary
        </span>
        <span className="legend-item">
          <span className="excluded-marker">×</span> stem excluded by revised
          boundary (historical measurement retained)
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="map">
        {plots.map((p) => {
          const entry = view === "latest" ? frameByPlot[p.code] : null;
          const ring = entry?.boundary || p.boundary;
          const area = entry ? entry.declared_area_ha : p.declared_area_ha;
          const revised = entry?.revision_id != null;
          const pts = ring.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ");
          return (
            <g key={p.code} className="plot-shape" onClick={() => onSelect(p.code)}>
              <polygon points={pts}
                       className={revised ? "boundary boundary-revised"
                                          : "boundary boundary-original"} />
              <text x={sx(p.x_m)} y={sy(p.y_m)} className="plot-label">
                {p.code} · {area} ha · {p.stratum_code}
                {revised && " · revised"}
              </text>
              {(m2ByPlot[p.code] || []).map((m) => {
                const s = STATUS_STYLE[m.status] || STATUS_STYLE.alive_measured;
                const isExcluded = excluded.has(m.id);
                return (
                  <g key={m.id}>
                    <circle cx={sx(m.x_m)} cy={sy(m.y_m)}
                            r={m.status === "dead" ? 7 : 5}
                            fill={s.color}
                            stroke={isExcluded ? "#000"
                              : m.status === "alive_not_measured" ? "#000"
                              : "#fff"}
                            strokeWidth={isExcluded ? 2.5 : 1}>
                      <title>{`${p.code}/${m.field_number} — ${s.label}${
                        isExcluded ? " — EXCLUDED by revised boundary" : ""}
	dbh ${m.dbh_cm ?? "—"} cm · h ${m.height_m ?? "—"} m`}</title>
                    </circle>
                    {isExcluded &&
                      <text x={sx(m.x_m)} y={sy(m.y_m) + 3}
                            className="excluded-cross">×</text>}
                  </g>
                );
              })}
            </g>
          );
        })}
      </svg>
      <p className="hint">
        Circles are individuals at {ctx.t2}. Switch the boundary view to see
        the original survey polygons vs the latest published frame; a × marks
        a historical stem position now outside the revised boundary. Click a
        plot for t1→t2 remeasurement detail.
      </p>
    </div>
  );
}

function inside(x, y, ring) {
  let isIn = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) &&
        x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi) {
      isIn = !isIn;
    }
  }
  return isIn;
}
