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

  // sampling-frame revisions
  frameRevisions: (plot) =>
    get(`/frame-revisions/${plot ? `?plot=${encodeURIComponent(plot)}` : ""}`),
  frameRevision: (id) => get(`/frame-revisions/${id}/`),
  createFrameRevision: (payload) => post("/frame-revisions/", payload),
  revalidateFrame: (id) => post(`/frame-revisions/${id}/revalidate/`),
  submitFrameReview: (id, review_note) =>
    post(`/frame-revisions/${id}/submit_review/`, { review_note }),
  publishFrame: (id, publication_reason) =>
    post(`/frame-revisions/${id}/publish/`, { publication_reason }),
  compareFrame: (id) => get(`/frame-revisions/${id}/compare/`),
  impactFrame: (id) => get(`/frame-revisions/${id}/impact/`),
  resolveFrameIssue: (id, resolution_note) =>
    post(`/frame-issues/${id}/resolve/`, { resolution_note }),
};
