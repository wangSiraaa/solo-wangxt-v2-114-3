import React, { useMemo } from "react";

const STATUS_STYLE = {
  alive_measured: { color: "#2e7d32", label: "alive, measured" },
  alive_not_measured: { color: "#e6a700", label: "alive, NOT measured" },
  dead: { color: "#b71c1c", label: "dead" },
  missing_tree: { color: "#888", label: "not located" },
};

/**
 * SVG overview: all plot boundaries (to projected-metre scale) with the
 * remeasurement status of each individual at t2. Click a plot to open it.
 */
export default function PlotMap({ ctx, onSelect }) {
  const { plots, m2 } = ctx;

  const bounds = useMemo(() => {
    const xs = [], ys = [];
    plots.forEach((p) => p.boundary.forEach(([x, y]) => {
      xs.push(x); ys.push(y);
    }));
    if (!xs.length) return null;
    return { minX: Math.min(...xs), minY: Math.min(...ys),
             maxX: Math.max(...xs), maxY: Math.max(...ys) };
  }, [plots]);

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
          <span className="square">▭</span> plot boundary (unequal areas)
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="map">
        {plots.map((p) => {
          const pts = p.boundary.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ");
          return (
            <g key={p.code} className="plot-shape" onClick={() => onSelect(p.code)}>
              <polygon points={pts} className="boundary" />
              <text x={sx(p.x_m)} y={sy(p.y_m)} className="plot-label">
                {p.code} · {p.declared_area_ha} ha · {p.stratum_code}
              </text>
              {(m2ByPlot[p.code] || []).map((m) => {
                const s = STATUS_STYLE[m.status] || STATUS_STYLE.alive_measured;
                return (
                  <circle key={m.id} cx={sx(m.x_m)} cy={sy(m.y_m)}
                          r={m.status === "dead" ? 7 : 5}
                          fill={s.color}
                          stroke={m.status === "alive_not_measured"
                            ? "#000" : "#fff"}
                          strokeWidth={m.status === "alive_not_measured" ? 1.5 : 1}>
                    <title>{`${p.code}/${m.field_number} — ${s.label}
dbh ${m.dbh_cm ?? "—"} cm · h ${m.height_m ?? "—"} m`}</title>
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
        located. Click a plot for t1→t2 remeasurement detail.
      </p>
    </div>
  );
}
