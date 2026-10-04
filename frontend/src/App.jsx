import React, { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import PlotMap from "./components/PlotMap.jsx";
import PlotDetail from "./components/PlotDetail.jsx";
import ConflictsWorkbench from "./components/ConflictsWorkbench.jsx";
import EstimatePanel from "./components/EstimatePanel.jsx";

const TABS = ["map", "conflicts", "estimates"];

export default function App() {
  const [tab, setTab] = useState("map");
  const [plots, setPlots] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [t1, setT1] = useState(null);
  const [t2, setT2] = useState(null);
  const [m1, setM1] = useState([]);
  const [m2, setM2] = useState([]);
  const [selectedPlot, setSelectedPlot] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [revisions, setRevisions] = useState([]);
  const [boundaryView, setBoundaryView] = useState("current");
  const [error, setError] = useState("");

  async function refreshConflicts() {
    setConflicts(await api.conflicts("open"));
  }
  async function refreshRevisions() {
    setRevisions(await api.frameRevisions());
  }

  useEffect(() => {
    (async () => {
      try {
        const [ps, cs, rs] = await Promise.all([
          api.plots(), api.campaigns(), api.frameRevisions()]);
        setPlots(ps);
        setCampaigns(cs);
        setRevisions(rs);
        const ordered = [...cs].sort((a, b) =>
          a.measured_on.localeCompare(b.measured_on));
        if (ordered.length >= 2) {
          setT1(ordered[0].code);
          setT2(ordered[ordered.length - 1].code);
        }
        setConflicts(await api.conflicts("open"));
      } catch (e) {
        setError(e.message);
      }
    })();
  }, []);

  useEffect(() => {
    if (!t1 || !t2) return;
    (async () => {
      const [a, b] = await Promise.all([
        api.measurements(t1), api.measurements(t2)]);
      setM1(a);
      setM2(b);
    })();
  }, [t1, t2]);

  const ctx = useMemo(() => ({
    plots, campaigns, t1, t2, m1, m2, conflicts,
    revisions, boundaryView, setBoundaryView,
    setSelectedPlot, refreshConflicts, refreshRevisions,
  }), [plots, campaigns, t1, t2, m1, m2, conflicts, revisions,
       boundaryView]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Fixed-plot remeasurement station</h1>
        <div className="meta">
          {campaigns.map((c) => (
            <span key={c.code} className="chip">
              {c.code} · {c.measured_on}
            </span>
          ))}
          <span className="chip warn-chip">
            {conflicts.length} open identity conflict
            {conflicts.length === 1 ? "" : "s"}
          </span>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <nav className="tabs">
        {TABS.map((t) => (
          <button key={t} className={tab === t ? "tab active" : "tab"}
                  onClick={() => setTab(t)}>
            {t === "map" ? "Plots & individuals"
              : t === "conflicts" ? `Identity conflicts (${conflicts.length})`
              : "Estimates"}
          </button>
        ))}
      </nav>

      <main>
        {tab === "map" && (
          selectedPlot
            ? <PlotDetail plotCode={selectedPlot} ctx={ctx}
                          onBack={() => setSelectedPlot(null)} />
            : <PlotMap ctx={ctx} onSelect={setSelectedPlot} />
        )}
        {tab === "conflicts" && (
          <ConflictsWorkbench ctx={ctx}
                              onChanged={async () => {
                                setConflicts(await api.conflicts("open"));
                              }} />
        )}
        {tab === "estimates" && <EstimatePanel ctx={ctx} />}
      </main>

      <footer>
        Fictional demonstration data · coordinates EPSG:{plots[0]?.crs_epsg}
        {" "}· dbh cm (raw unit retained) · height m · areas in hectares
      </footer>
    </div>
  );
}
