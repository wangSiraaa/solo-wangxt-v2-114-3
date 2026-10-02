import React, { useEffect, useState } from "react";
import { api } from "../api.js";

/**
 * Human-in-the-loop identity workbench.
 * A same number with contradictory positions is NEVER auto-merged: it must
 * be verified here as either "renumber" (same individual, new tag) or
 * "distinct" (different individuals -> t1 removal + t2 ingrowth).
 */
export default function ConflictsWorkbench({ ctx, onChanged }) {
  const [all, setAll] = useState([]);
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    setAll(await api.conflicts());
  }
  useEffect(() => { load(); }, []);

  async function resolve(c, decision) {
    setBusy(c.id);
    setErr("");
    try {
      await api.resolveConflict(c.id, { status: decision, note });
      setNote(`#${c.id} (${c.field_number}) recorded as ${decision}`);
      await load();
      onChanged?.();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <h2>Identity verification workbench</h2>
      <p className="hint">
        Same field number with contradictory positions, or a new number at a
        familiar position. Nothing here is treated as the same individual
        until verified.
      </p>
      {note && <div className="ok">{note}</div>}
      {err && <div className="error">{err}</div>}
      <table className="conflict-table">
        <thead>
          <tr><th>plot</th><th>number</th><th>distance</th>
          <th>state</th><th>verification</th></tr>
        </thead>
        <tbody>
          {all.map((c) => (
            <tr key={c.id} className={c.status === "open" ? "open" : "closed"}>
              <td>{c.plot}</td>
              <td>{c.field_number}</td>
              <td>{c.distance_m?.toFixed(2)} m</td>
              <td>{c.status}{c.resolution_note
                  ? ` — ${c.resolution_note}` : ""}</td>
              <td>
                {c.status === "open" ? (
                  <>
                    <button disabled={busy === c.id}
                            onClick={() => resolve(c, "renumber")}>
                      same tree, renumbered
                    </button>
                    <button className="danger" disabled={busy === c.id}
                            onClick={() => resolve(c, "distinct")}>
                      different trees
                    </button>
                  </>
                ) : <span className="locked">verified {c.status}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
