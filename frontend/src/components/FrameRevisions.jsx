import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const STATUS_COLOR = {
  draft: "#1565c0",
  reviewed: "#2e7d32",
  published: "#37474f",
  blocked: "#b71c1c",
};

function ringToText(ring) {
  return JSON.stringify(ring, null, 0);
}

function parseRing(text) {
  const data = JSON.parse(text);
  if (!Array.isArray(data) || data.length < 3)
    throw new Error("need [[x,y],...] with ≥3 vertices");
  return data.map(([x, y]) => [Number(x), Number(y)]);
}

/**
 * Sampling-frame revision workbench.
 *
 * Boundary/area/CRS statements move draft -> reviewed -> published as
 * immutable PlotFrameRevisions; the original boundary, area check and
 * publication reason are retained. Excluded stems, area-tolerance and
 * same-stratum overlap failures open pending items and BLOCK publication.
 */
export default function FrameRevisions({ ctx, onSelectPlot }) {
  const { plots, frameVersion, refreshFrame } = ctx;
  const [plotCode, setPlotCode] = useState(plots[0]?.code || "");
  const [revisions, setRevisions] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [compare, setCompare] = useState(null);
  const [impact, setImpact] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // proposal form
  const [ringText, setRingText] = useState("");
  const [declared, setDeclared] = useState("");
  const [crs, setCrs] = useState(32650);
  const [crsNote, setCrsNote] = useState("");
  const [reason, setReason] = useState("");

  const plot = plots.find((p) => p.code === plotCode);
  const openRev = revisions.find((r) => r.status !== "published");

  useEffect(() => {
    if (!plotCode) return;
    api.revisions(plotCode).then((rs) => {
      setRevisions(rs);
      const first = rs[0];
      setOpenId(first ? first.id : null);
    }).catch((e) => setErr(e.message));
  }, [plotCode]);

  useEffect(() => {
    if (plot) {
      setRingText(ringToText(plot.boundary));
      setDeclared(plot.declared_area_ha);
      setCrs(plot.crs_epsg ?? 32650);
    }
    setCompare(null);
    setImpact(null);
  }, [plotCode, plot]);

  useEffect(() => {
    if (openId == null) {
      setCompare(null);
      setImpact(null);
      return;
    }
    api.compareRevision(openId).then(setCompare).catch(() => {});
    api.impactRevision(openId).then(setImpact).catch(() => {});
  }, [openId]);

  async function reload(keepOpen) {
    const rs = await api.revisions(plotCode);
    setRevisions(rs);
    await refreshFrame();
    if (keepOpen) {
      const fresh = rs.find((r) => r.id === keepOpen);
      if (fresh) setOpenId(fresh.id);
      if (fresh) {
        setCompare(await api.compareRevision(fresh.id).catch(() => null));
        setImpact(await api.impactRevision(fresh.id).catch(() => null));
      }
    }
  }

  async function run(fn) {
    setBusy(true);
    setErr("");
    try {
      const r = await fn();
      await reload(r?.id ?? openId);
      return r;
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function propose() {
    let boundary;
    try {
      boundary = parseRing(ringText);
    } catch (e) {
      setErr(`boundary JSON: ${e.message}`);
      return;
    }
    const r = await run(() =>
      api.createRevision({
        plot: plotCode,
        boundary,
        declared_area_ha: Number(declared),
        crs_epsg: Number(crs),
        crs_note: crsNote,
      }));
    if (r) setOpenId(r.id);
  }

  async function revalidate() {
    let boundary;
    try {
      boundary = parseRing(ringText);
    } catch (e) {
      setErr(`boundary JSON: ${e.message}`);
      return;
    }
    await run(() => api.revalidateRevision(openId, {
      boundary,
      declared_area_ha: Number(declared),
      crs_epsg: Number(crs),
      crs_note: crsNote,
    }));
  }

  const current = openId != null
    ? revisions.find((r) => r.id === openId) : null;

  // when a blocked/open revision is selected, the form mirrors it
  useEffect(() => {
    if (current && current.status !== "published") {
      setRingText(ringToText(current.boundary));
      setDeclared(current.declared_area_ha);
      setCrs(current.crs_epsg);
      setCrsNote(current.crs_note || "");
    }
  }, [openId]);

  const frameEntry = frameVersion?.plot_payload?.[plotCode];
  const latestIsRevised = frameEntry && frameEntry.revision_id != null;

  return (
    <div>
      <h2>Plot sampling-frame revisions</h2>
      <p className="hint">
        A re-surveyed boundary never silently rewrites the published frame.
        Each proposal is an immutable <strong>draft → reviewed → published
        </strong> revision retaining the <em>original</em> boundary, area
        cross-check and publication reason; publishing emits one new
        append-only frame version. Historical tree measurements stay bound
        to the boundary in force at collection — they are listed as affected
        individuals, never migrated or deleted. Estimates bind explicitly to
        a frame version.
      </p>
      {err && <div className="error">{err}</div>}

      <div className="frame-controls">
        <label>Plot
          <select value={plotCode} onChange={(e) => setPlotCode(e.target.value)}>
            {plots.map((p) => <option key={p.code} value={p.code}>{p.code}</option>)}
          </select>
        </label>
        <button onClick={() => onSelectPlot(plotCode)}>open on map →</button>
        <span className="chip ok-chip">
          latest frame v{frameVersion?.version}
          {latestIsRevised ? ` (this plot revised by revision #${
            revisions.find((r) => r.id === frameEntry.revision_id)?.revision_no
              ?? "?"})` : " (original survey)"}
        </span>
      </div>

      <section className="revision-list">
        <h3>Revisions</h3>
        {revisions.length === 0 && <p>No revisions proposed for {plotCode}.</p>}
        <table className="version-table">
          <tbody>
            {revisions.map((r) => (
              <tr key={r.id}
                  className={openId === r.id ? "selected-row" : ""}
                  onClick={() => setOpenId(r.id)}>
                <td>#{r.revision_no}</td>
                <td><span className="badge"
                  style={{ background: STATUS_COLOR[r.status] }}>
                  {r.status}</span></td>
                <td>{r.area_polygon_ha.toFixed(4)} ha
                  {!r.area_check?.within_tolerance && r.area_check &&
                    <span className="warn-chip chip"> area mismatch</span>}
                </td>
                <td>{r.open_issue_count} open</td>
                <td>{r.published_at
                  ? new Date(r.published_at).toLocaleString() : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {(!openRev || openRev?.status === "published") && plot && (
        <section className="propose-form">
          <h3>Propose a revision</h3>
          <RevisionForm
            ringText={ringText} setRingText={setRingText}
            declared={declared} setDeclared={setDeclared}
            crs={crs} setCrs={setCrs}
            crsNote={crsNote} setCrsNote={setCrsNote}
            reason={reason} setReason={setReason}
            disabled={busy} onPropose={propose}
            proposal
          />
        </section>
      )}

      {current && current.status !== "published" && (
        <section className={`revision-detail ${current.status}`}>
          <h3>Revision #{current.revision_no}
            <span className="badge" style={{ background: STATUS_COLOR[current.status] }}>
              {current.status}</span>
          </h3>

          <RevisionForm
            ringText={ringText} setRingText={setRingText}
            declared={declared} setDeclared={setDeclared}
            crs={crs} setCrs={setCrs}
            crsNote={crsNote} setCrsNote={setCrsNote}
            reason={reason} setReason={setReason}
            disabled={busy} onRevalidate={revalidate}
          />

          <div className="area-check">
            <strong>Area cross-check:</strong>{" "}
            {current.area_check?.detail || "—"}{" "}
            <span className={current.area_check?.within_tolerance
              ? "ok-text" : "bad-text"}>
              {current.area_check?.within_tolerance ? "within tolerance"
                                                    : "OUT OF TOLERANCE"}
            </span>
          </div>

          {current.issues?.length > 0 && (
            <div className="issues">
              <h4>Pending items ({current.open_issue_count} open)</h4>
              {current.issues.map((i) => (
                <div key={i.id}
                     className={`issue ${i.status === "open" ? "open" : "resolved"}`}>
                  <strong>{i.kind.replace("_", " ")}</strong>
                  <span className="issue-status">[{i.status}]</span>
                  <div>{i.detail}</div>
                  {i.kind === "tree_excluded" && (
                    <ul className="stem-list">
                      {i.payload.excluded?.map((s) => (
                        <li key={s.measurement_id}>
                          {plotCode}/{s.field_number} · {s.campaign} ·
                          {" "}({s.x_m}, {s.y_m}) — historical measurement
                          retained, not moved
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="workflow-actions">
            <button disabled={busy} onClick={revalidate}>
              re-validate / update geometry
            </button>
            <button disabled={busy || current.status === "blocked"}
                    onClick={() => run(() =>
                      api.reviewRevision(current.id, { reason }))}>
              mark reviewed
            </button>
            <button className="primary"
                    disabled={busy || current.status !== "reviewed"}
                    onClick={async () => {
                      if (!reason.trim()) {
                        setErr("a publication reason is required");
                        return;
                      }
                      const r = await run(() =>
                        api.publishRevision(current.id, { reason }));
                      if (r) setOpenId(r.id);
                    }}>
              publish → new frame version
            </button>
          </div>
          <p className="hint">
            {current.status === "blocked"
              ? "Blocked: resolve the open items by correcting the geometry and re-validating. Review/publish are impossible until clean; nothing is computed silently."
              : current.status === "draft"
                ? "A clean draft must be reviewed before publication."
                : "Reviewed and clean — publish emits one atomic frame version (concurrent publish requests cannot both succeed)."}
          </p>
        </section>
      )}

      {compare && <RevisionCompare data={compare} />}
      {impact && <RevisionImpact data={impact} />}
    </div>
  );
}

function RevisionForm(props) {
  const {
    ringText, setRingText, declared, setDeclared, crs, setCrs,
    crsNote, setCrsNote, reason, setReason, disabled,
    onPropose, onRevalidate, proposal,
  } = props;
  return (
    <div className="revision-form">
      <label>Boundary ring [[x_m, y_m], ...]
        <textarea rows={3} value={ringText}
                  onChange={(e) => setRingText(e.target.value)} />
      </label>
      <div className="form-row">
        <label>declared area (ha)
          <input type="number" step="0.0001" value={declared}
                 onChange={(e) => setDeclared(e.target.value)} /></label>
        <label>CRS EPSG
          <input type="number" value={crs}
                 onChange={(e) => setCrs(e.target.value)} /></label>
      </div>
      <label>CRS note (datum/zone/source survey)
        <input value={crsNote}
               onChange={(e) => setCrsNote(e.target.value)} /></label>
      <label>reason / publication note
        <input value={reason} placeholder="why the boundary is revised"
               onChange={(e) => setReason(e.target.value)} /></label>
      {proposal
        ? <button disabled={disabled} onClick={onPropose}>propose draft</button>
        : null}
    </div>
  );
}

function RevisionCompare({ data }) {
  const W = 640, H = 380, PAD = 40;
  const allPts = useMemo(
    () => [...data.original.boundary, ...data.revised.boundary],
    [data]);
  const xs = allPts.map(([x]) => x), ys = allPts.map(([, y]) => y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const sx = (x) => PAD + (x - minX) / (maxX - minX) * (W - 2 * PAD);
  const sy = (y) => H - PAD - (y - minY) / (maxY - minY) * (H - 2 * PAD);
  const pts = (ring) => ring.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ");

  const effectColor = {
    retained: "#2e7d32", excluded: "#b71c1c", newly_included: "#1565c0" };

  return (
    <section className="compare">
      <h3>Old vs new boundary — impact</h3>
      <svg viewBox={`0 0 ${W} ${H}`} className="plot-map">
        <polygon points={pts(data.original.boundary)}
                 className="boundary-original" />
        <polygon points={pts(data.revised.boundary)}
                 className="boundary-revised" />
        {data.stems.map((s) => (
          <circle key={s.measurement_id} cx={sx(s.x_m)} cy={sy(s.y_m)}
                  r={6} fill={effectColor[s.effect]} stroke="#fff">
            <title>{`${data.plot}/${s.field_number} (${s.campaign}): ${s.effect}`}</title>
          </circle>
        ))}
      </svg>
      <div className="legend">
        <span className="legend-item"><span className="line-swatch original" />
          original {data.original.polygon_area_ha.toFixed(4)} ha</span>
        <span className="legend-item"><span className="line-swatch revised" />
          revised {data.revised.polygon_area_ha.toFixed(4)} ha</span>
        <span className="legend-item">
          <span className="dot" style={{ background: "#b71c1c" }} />
          {data.excluded_stems.length} excluded (historical, retained)</span>
        <span className="legend-item">
          <span className="dot" style={{ background: "#1565c0" }} />
          {data.newly_included_stems.length} newly included</span>
      </div>
      <p className="hint">
        area Δ {data.area_delta_ha >= 0 ? "+" : ""}
        {data.area_delta_ha.toFixed(4)} ha · per-hectare expansion of kg/ha for
        the new boundary vs old: ×{data.per_hectare_factor} — this changes ONLY
        new draft estimates bound to the emitted frame; confirmed estimates
        keep their frozen area.
      </p>
    </section>
  );
}

function RevisionImpact({ data }) {
  return (
    <section className="impact">
      <h3>Affected individuals &amp; estimate binding</h3>
      {data.emitted_frame_version
        ? <p>Emitted frame <strong>v{data.emitted_frame_version}</strong>.</p>
        : <p>Not published — no frame emitted; estimates cannot use it.</p>}
      <div className="two-col">
        <div>
          <h4>Affected measurements ({data.affected_measurements.length})</h4>
          <ul className="stem-list">
            {data.affected_measurements.map((m) => (
              <li key={m.measurement_id} className="bad-text">
                {data.plot}/{m.field_number} · {m.campaign} —
                outside revised boundary; still attributed to the original
                boundary it was collected under
              </li>
            ))}
            {data.affected_measurements.length === 0 &&
              <li className="ok-text">none — no historical stem excluded</li>}
          </ul>
        </div>
        <div>
          <h4>Estimate versions</h4>
          <table className="tree-table">
            <thead><tr><th>edition</th><th>status</th><th>frame</th>
              <th>affected</th></tr></thead>
            <tbody>
              {data.estimate_versions.map((v) => (
                <tr key={v.estimate_id}>
                  <td>#{v.estimate_id} {v.label}</td>
                  <td>{v.status}</td>
                  <td>v{v.frame_version ?? "–"}</td>
                  <td>{v.affected_by_this_revision ? "YES" : "no — frozen on own frame"}</td>
                </tr>
              ))}
              {data.estimate_versions.length === 0 &&
                <tr><td colSpan={4}>no estimate runs yet</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      {data.open_issues.length > 0 && (
        <div className="issues">
          <h4>Blocking items</h4>
          {data.open_issues.map((i, k) => (
            <div key={k} className="issue open">
              <strong>{i.kind.replace("_", " ")}</strong>: {i.detail}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
