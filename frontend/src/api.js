const BASE = "/api";

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path}: ${res.status}`);
  return data;
}

export const api = {
  plots: () => get("/plots/"),
  measurements: (campaign) =>
    get(`/measurements/?campaign=${encodeURIComponent(campaign)}`),
  campaigns: () => get("/campaigns/"),
  equations: () => get("/equations/"),
  conflicts: (status) =>
    get(`/conflicts/${status ? `?status=${status}` : ""}`),
  resolveConflict: (id, payload) =>
    post(`/conflicts/${id}/resolve/`, payload),
  estimates: () => get("/estimates/"),
  estimate: (id) => get(`/estimates/${id}/`),
  createEstimate: (payload) => post("/estimates/", payload),
  confirmEstimate: (id) => post(`/estimates/${id}/confirm/`),

  // Sampling-frame revisions
  frames: () => get("/frames/"),
  latestFrame: () => get("/frames/latest/"),
  revisions: (plotCode) =>
    get(`/plot-revisions/${plotCode ? `?plot=${encodeURIComponent(plotCode)}` : ""}`),
  revision: (id) => get(`/plot-revisions/${id}/`),
  createRevision: (payload) => post("/plot-revisions/", payload),
  revalidateRevision: (id, payload) =>
    post(`/plot-revisions/${id}/revalidate/`, payload),
  reviewRevision: (id, payload) =>
    post(`/plot-revisions/${id}/review/`, payload),
  publishRevision: (id, payload) =>
    post(`/plot-revisions/${id}/publish/`, payload),
  compareRevision: (id) => get(`/plot-revisions/${id}/compare/`),
  impactRevision: (id) => get(`/plot-revisions/${id}/impact/`),
};
