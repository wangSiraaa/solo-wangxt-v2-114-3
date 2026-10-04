import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import BoundaryViewSwitch, { useFrameImpact } from "./FrameControls.jsx";

const ISSUE_LABEL = {
  excluded_tree: "excludes an existing stem",
  area_mismatch: "declared vs polygon area beyond tolerance",
  overlap: "overlaps same-stratum plot",
  crs_mismatch: "CRS statement differs from station CRS",
};

/**
 * Plot-level frame revision workbench:
 *   * list editions draft -> reviewed -> published/superseded;
 *   * upload a resurveyed boundary (ring textarea + area + CRS);
 *   * inspect blocking issues and resolve them with documented notes;
 *   * submit for review and publish (reason mandatory);
 *   * old/new comparison and affected historical individuals.
 */
export default function FrameRevisionWorkbench({ plot, revisions,
                                                 boundaryView, setBoundaryView,
                                                 onChanged }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");
  const [openId, setOpenId] = useState(null);
  const [form, setForm] = useState({
    boundary: "", declared_area_ha: "", crs_epsg: plot.crs_epsg,
    crs_note: "", reason: "",
  });
  const [reviewNote, setReviewNote] = useState("");
  const [publishReason, setPublishReason] = useState("");
  const [resolveNotes, setResolveNotes] = useState({});

  const plotRevisions = revisions
    .filter((r) => r.plot === plot.id || r.plot_code === plot.code)
    .sort((a, b) => b.revision_no - a.revision_no);
  const selected = plotRevisions.find((r) => r.id === openId) || null;
  const { impact } = useFrameImpact(openId);

  useEffect(() => {
    if (!openId && plotRevisions[0]) setOpenId(plotRevisions[0].id);
  }, [revisions.length]);

  async function run(fn, okMsg) {
    setBusy(true); setErr(""); setInfo("");
    try {
      await fn();
      if (okMsg) setInfo(okMsg);
      await onChanged();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  function upload() {
    let boundary;
    try {
      boundary = JSON.parse(form.boundary);
    } catch {
      setErr("boundary must be a JSON ring of [x, y] pairs");
      return;
    }
    return run(() => api.createFrameRevision({
      plot: plot.code, boundary,
      declared_area_ha: Number(form.declared_area_ha),
      crs_epsg: Number(form.crs_epsg),
      crs_note: form.crs_note, reason: form.reason,
    }).then((d) => setOpenId(d.id)), "revision uploaded");
  }

  const issues = selected?.issues || [];
  const openIssues = issues.filter((i) => i.status === "open");

  return (
    <section className="frame-workbench">
      <h3>Sampling-frame revisions
        <BoundaryViewSwitch view={boundaryView} setView={setBoundaryView}
                            hasProposed={plotRevisions.some((r) =>
                              r.status === "draft" || r.status === "reviewed")} />
      </h3>
      <p className="hint">
        A resurvey is a new immutable edition (draft → reviewed → published).
        The old published boundary and estimates bound to it are never
        rewritten. Editions that exclude stems, fail the area check or
        overlap a same-stratum plot carry blocking issues.
      </p>
      {err && <div className="error">{err}</div>}
      {info && <div className="ok">{info}</div>}

      <div className="frame-cols">
        <div>
          <h4>Editions</h4>
          <table className="frame-table">
            <tbody>
              {plotRevisions.map((r) => (
                <tr key={r.id}
                    className={r.id === openId ? "active" : ""}
                    onClick={() => setOpenId(r.id)}>
                  <td>v{r.revision_no}</td>
                  <td className={`status-${r.status}`}>{r.status}</td>
                  <td>{r.declared_area_ha} ha</td>
                  <td>EPSG:{r.crs_epsg}</td>
                  <td>{r.open_issue_count > 0
                    ? <span className="warn-chip">
                        {r.open_issue_count} blocking</span> : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>Upload resurveyed boundary</h4>
          <div className="frame-form">
            <textarea rows={3} placeholder='[[x,y],[x,y],...]'
                      value={form.boundary}
                      onChange={(e) => setForm(
                        { ...form, boundary: e.target.value })} />
            <div className="frame-form-row">
              <label>declared area (ha)
                <input type="number" step="0.0001"
                       value={form.declared_area_ha}
                       onChange={(e) => setForm({ ...form,
                         declared_area_ha: e.target.value })} /></label>
              <label>CRS EPSG
                <input type="number" value={form.crs_epsg}
                       onChange={(e) => setForm(
                         { ...form, crs_epsg: e.target.value })} /></label>
            </div>
            <input placeholder="CRS / surveyor note"
                   value={form.crs_note}
                   onChange={(e) => setForm(
                     { ...form, crs_note: e.target.value })} />
            <input placeholder="reason for the revision"
                   value={form.reason}
                   onChange={(e) => setForm(
                     { ...form, reason: e.target.value })} />
            <button disabled={busy} onClick={upload}>
              create / find draft (idempotent)
            </button>
          </div>
        </div>

        {selected && (
          <div className="frame-detail">
            <h4>v{selected.revision_no} — {selected.status}</h4>
            <p className="hint">
              polygon {Number(selected.polygon_area_ha).toFixed(4)} ha vs
              declared {selected.declared_area_ha} ha ·
              area check {selected.area_check?.passed ? "PASS" : "FAIL"}
              {selected.publication_reason &&
                <> · published: {selected.publication_reason}</>}
              {selected.review_note &&
                <> · review: {selected.review_note}</>}
            </p>

            {issues.length > 0 && (
              <table className="issue-table">
                <thead><tr><th>blocking issue</th><th>status / resolution</th>
                </tr></thead>
                <tbody>
                  {issues.map((i) => (
                    <tr key={i.id} className={`issue-${i.status}`}>
                      <td>
                        <strong>{ISSUE_LABEL[i.kind] || i.kind}</strong>
                        <div><small>{i.summary}</small></div>
                      </td>
                      <td>
                        {i.status === "resolved"
                          ? <span className="ok">resolved:
                              {" "}{i.resolution_note}</span>
                          : <>
                              <input placeholder="documented decision"
                                     value={resolveNotes[i.id] || ""}
                                     onChange={(e) => setResolveNotes(
                                       { ...resolveNotes,
                                         [i.id]: e.target.value })} />
                              <button disabled={busy}
                                onClick={() => run(() =>
                                  api.resolveFrameIssue(
                                    i.id, resolveNotes[i.id] || ""),
                                  "issue resolved")}>
                                resolve
                              </button>
                            </>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {selected.status === "draft" && (
              <div className="frame-action">
                <input placeholder="QA review note (required)"
                       value={reviewNote}
                       onChange={(e) => setReviewNote(e.target.value)} />
                <button disabled={busy}
                  onClick={() => run(() =>
                    api.submitFrameReview(selected.id, reviewNote),
                    "submitted for review")}>
                  submit for review
                </button>
              </div>
            )}
            {selected.status === "reviewed" && (
              <div className="frame-action">
                <input placeholder="publication reason (mandatory)"
                       value={publishReason}
                       onChange={(e) => setPublishReason(e.target.value)} />
                <button disabled={busy || openIssues.length > 0}
                  title={openIssues.length
                    ? "open blocking issues must be resolved" : ""}
                  onClick={() => run(() =>
                    api.publishFrame(selected.id, publishReason),
                    "frame published; draft estimates refreshed")}>
                  publish{openIssues.length
                    ? ` (${openIssues.length} open blocking)` : ""}
                </button>
                {openIssues.length > 0 && (
                  <div className="error">
                    {openIssues.length} open blocking issue(s): publication
                    refused, not silently recalculated.
                  </div>)}
              </div>
            )}

            {impact && (
              <div className="frame-impact">
                <h4>Affected individuals (historical data is read-only)</h4>
                <p>excluded by new boundary:
                  {" "}{impact.affected.excluded_count} ·
                  newly inside: {impact.affected.newly_inside_count}</p>
                {impact.affected.excluded.length > 0 && (
                  <ul>
                    {impact.affected.excluded.map((e) => (
                      <li key={e.measurement_id} className="excluded">
                        {e.tree} @ {e.campaign} — collected on frame
                        {" "}v{e.collected_frame_revision ?? "?"}; stays on
                        that original boundary
                      </li>
                    ))}
                  </ul>
                )}
                {impact.affected.newly_inside.length > 0 && (
                  <ul>
                    {impact.affected.newly_inside.map((e) => (
                      <li key={e.measurement_id} className="new-in">
                        {e.tree} @ {e.campaign} — inside only the new ring
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
