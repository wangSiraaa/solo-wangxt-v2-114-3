import React, { useEffect, useState } from "react";
import { api } from "../api.js";

function Mg(kg) { return (kg / 1000).toFixed(2); }

export default function EstimatePanel({ ctx }) {
  const { t1, t2 } = ctx;
  const [equations, setEquations] = useState([]);
  const [selected, setSelected] = useState([]);
  const [versions, setVersions] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function loadVersions() {
    setVersions(await api.estimates());
  }
  useEffect(() => {
    api.equations().then((eq) => {
      setEquations(eq);
      setSelected(eq.map((e) => e.id));
    });
    loadVersions();
  }, []);

  useEffect(() => {
    if (openId == null) return;
    api.estimate(openId).then(setDetail).catch((e) => setErr(e.message));
  }, [openId]);

  async function runDraft() {
    setBusy(true); setErr("");
    try {
      const d = await api.createEstimate({
        label: `Draft ${new Date().toISOString().slice(0, 16)}`,
        t1_campaign: t1, t2_campaign: t2,
        equation_ids: selected, fpc: true,
      });
      await loadVersions();
      setOpenId(d.id);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function confirm(id) {
    setBusy(true); setErr("");
    try {
      await api.confirmEstimate(id);
      await loadVersions();
      setDetail(await api.estimate(id));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h2>Population estimate — stratified expansion</h2>
      <p className="hint">
        Per-plot component ÷ each plot's own area → stratum per-hectare mean
        → scaled by known stratum land area. Trees are never pooled, averaged
        and multiplied by area. Δstock = survivor growth − mortality +
        ingrowth, same allometric equation on both dates.
      </p>
      {err && <div className="error">{err}</div>}

      <section className="eq-picker">
        <h3>Allometric equations (applicability explicit)</h3>
        {equations.map((e) => (
          <label key={e.id} className="eq-row">
            <input type="checkbox" checked={selected.includes(e.id)}
                   disabled={e.status === "confirmed-locked"}
                   onChange={() => setSelected((s) =>
                     s.includes(e.id) ? s.filter((x) => x !== e.id)
                                     : [...s, e.id])} />
            <span>
              <strong>{e.code} v{e.version}</strong> [{e.status}]
              {" "}for {e.species_codes.join(", ")} · {e.form}
              <br /><small>
                dbh {e.dbh_min_cm}–{e.dbh_max_cm} cm ·
                residual σ(ln AGB)={e.residual_sigma} · {e.citation}
              </small>
            </span>
          </label>
        ))}
        <button disabled={busy || !selected.length} onClick={runDraft}>
          Run draft estimate ({t1} → {t2})
        </button>
      </section>

      <section>
        <h3>Editions</h3>
        <table className="version-table">
          <tbody>
            {versions.map((v) => (
              <tr key={v.id} className={v.status}
                  onClick={() => setOpenId(v.id)}>
                <td>#{v.id}</td><td>{v.label}</td>
                <td className={`status-${v.status}`}>{v.status}</td>
                <td>{v.confirmed_at
                  ? new Date(v.confirmed_at).toLocaleString() : ""}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {v.status === "draft" &&
                    <button disabled={busy}
                            onClick={() => confirm(v.id)}>
                      confirm &amp; freeze forever
                    </button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && <EditionDetail v={detail} />}
    </div>
  );
}

function EditionDetail({ v }) {
  const r = v.result_payload;
  if (!r) return null;
  const frozen = v.status === "confirmed";
  return (
    <section className={`edition ${v.status}`}>
      <h3>Edition #{v.id} — {v.label}
        <span className={`badge status-${v.status}`}>{v.status}</span>
      </h3>
      {frozen && <p className="ok">
        Confirmed edition: result and equations are locked. A new equation
        can only produce a NEW edition; this one never changes silently.
        Checksum <code>{v.equation_checksum.slice(0, 16)}…</code>.
      </p>}

      <table className="component-table">
        <thead>
          <tr><th>component</th><th>total Mg</th>
          <th>SE sampling Mg</th><th>SE measurement Mg</th>
          <th>SE equation Mg</th><th>95% CI Mg (t, df)</th></tr>
        </thead>
        <tbody>
          {Object.entries(r.components).map(([k, c]) => (
            <tr key={k}>
              <td>{k.replace("_", " ")}</td>
              <td>{c.total_mg.toFixed(2)}</td>
              <td>{Mg(c.se_sampling_kg)}</td>
              <td>{Mg(c.se_measurement_kg)}</td>
              <td>{Mg(c.se_equation_residual_kg)}</td>
              <td>[{Mg(c.ci95_kg[0])}, {Mg(c.ci95_kg[1])}]
                {" "}(df {c.df.toFixed(1)})</td>
            </tr>
          ))}
          <tr className="net">
            <td>NET CHANGE</td>
            <td>{r.net_change.total_mg.toFixed(2)}</td>
            <td>{Mg(r.net_change.se_sampling_kg)}</td>
            <td>—</td>
            <td>{Mg(r.net_change.se_equation_residual_kg)}</td>
            <td>identity: survivor − mortality + ingrowth</td>
          </tr>
        </tbody>
      </table>

      <div className="two-col">
        <div>
          <h4>Stock reconciliation (Mg AGB)</h4>
          <ul>
            <li>{r.occasions.t1}: {r.stocks.t1_mg.toFixed(2)}</li>
            <li>{r.occasions.t2}: {r.stocks.t2_mg.toFixed(2)}</li>
            <li>interval: {r.occasions.interval_years} years</li>
          </ul>
          <h4>Design snapshot</h4>
          <pre>{JSON.stringify({
            estimator: r.design.estimator,
            fpc_used: r.design.fpc_used,
            recruitment_dbh_cm: r.design.recruitment_dbh_cm,
            zero_growth_tolerance_cm: r.design.zero_growth_tolerance_cm,
            strata: Object.fromEntries(
              Object.entries(r.design.strata).map(([k, s]) =>
                [k, { area_ha: s.area_ha, plots: s.plot_codes }]))
          }, null, 2)}</pre>
          <h4>Sampling-frame edition this estimate is bound to</h4>
          <p className="hint">
            Per-hectare expansion uses the area of these explicit frame
            editions. A newer boundary publication refreshes drafts but can
            never change a confirmed edition's numbers.
          </p>
          <table className="frame-table">
            <thead><tr><th>plot</th><th>frame edition</th><th>status</th>
            <th>expansion area (ha)</th></tr></thead>
            <tbody>
              {(v.frames || []).map((f) => (
                <tr key={f.id}>
                  <td>{f.plot_code}</td>
                  <td>v{f.revision_no}</td>
                  <td className={`status-${f.status}`}>{f.status}</td>
                  <td>{f.declared_area_ha}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(r.provenance.plots || []).some(
              (p) => (p.historical_observations_on_older_frame || []).length)
            && (
            <div className="frame-impact">
              <strong>Observations collected under older frame editions:</strong>
              {r.provenance.plots.filter(
                (p) => (p.historical_observations_on_older_frame || []).length
              ).map((p) => (
                <div key={p.plot}>
                  {p.plot}: expanded against v{p.frame_revision_no};{" "}
                  {p.historical_observations_on_older_frame.map(
                    (o) => `${o.tree} (v${o.collected_frame_revision})`
                  ).join(", ")}
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h4>Provenance / data quality</h4>
          <p>same-number pairs: {r.provenance.pairs_same_number} ·
            verified renumbers: {r.provenance.pairs_verified_renumber} ·
            open conflicts excluded:
            {" "}{r.provenance.open_conflicts.length}</p>
          {r.provenance.open_conflicts.map((c, i) => (
            <span key={i} className="conflict-chip">
              {c.plot}/{c.field_number} ({c.hint}, {c.distance_m}m)
            </span>
          ))}
          <details open>
            <summary>per-plot sources</summary>
            {r.provenance.plots.map((p) => (
              <div key={p.plot} className="plot-prov">
                <strong>{p.plot}</strong> ({p.area_ha} ha, {p.stratum},
                {" "}frame v{p.frame_revision_no ?? "?"}):
                {" "}growth {p.kg.survivor_growth} kg ·
                mortality {p.mortality.map((m) => m.tree).join(", ") || "—"} ·
                ingrowth {p.ingrowth.map((m) => m.tree).join(", ") || "—"}
                {p.verified_zero_growth.length > 0 &&
                  ` · verified zero: ${p.verified_zero_growth.map(z => z.tree).join(", ")}`}
                {p.alive_not_measured.length > 0 &&
                  ` · NOT measured: ${p.alive_not_measured.map(m => m.tree).join(", ")}`}
                {p.below_recruitment.length > 0 &&
                  ` · below recruitment: ${p.below_recruitment.map(m => m.tree).join(", ")}`}
                {p.not_located.length > 0 &&
                  ` · not located: ${p.not_located.map(m => m.tree).join(", ")}`}
                {p.equation_range_extrapolations.length > 0 &&
                  ` · EXTRAPOLATION: ${p.equation_range_extrapolations.map(e => e.tree).join(", ")}`}
              </div>
            ))}
          </details>
          <h4>Uncertainty assumptions</h4>
          <ol className="assumptions">
            {r.uncertainty_assumptions.map((a, i) => <li key={i}>{a}</li>)}
          </ol>
          <h4>Units &amp; equations recorded</h4>
          <pre>{JSON.stringify({ units: r.units,
            equations: Object.fromEntries(Object.entries(r.equations_used)
              .map(([sp, e]) => [sp, `${e.code}@${e.version}`])) },
            null, 2)}</pre>
        </div>
      </div>
    </section>
  );
}
