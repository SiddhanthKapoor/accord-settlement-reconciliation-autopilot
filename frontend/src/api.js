const API = "/api";

async function req(url, options = {}) {
  const res = await fetch(url, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const runBatch = (dataset, limit) =>
  req(`${API}/batch/run`, { method: "POST", body: JSON.stringify({ dataset, limit }) });

export const getBatch = (batchId) => req(`${API}/batch/${batchId}`);

export const getLatestBatch = () => req(`${API}/batch/latest`);

export const listBatchRecords = (batchId, { outcome, limit = 200, offset = 0 } = {}) => {
  const params = new URLSearchParams({ limit, offset });
  if (outcome) params.set("outcome", outcome);
  return req(`${API}/batch/${batchId}/records?${params}`);
};

// batchId matters: the same order can be processed in several batches,
// and opening a record from a batch listing should show that batch's
// decision, not whichever ran most recently.
export const getRecord = (recordId, batchId) =>
  req(`${API}/records/${recordId}${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`);

export const getLatestEvaluation = (dataset = "holdout") => req(`${API}/evaluation/latest?dataset=${dataset}`);

export const getDataSources = () => req(`${API}/data-sources`);

export const getAuditLog = (limit = 200) => req(`${API}/audit/log?limit=${limit}`);

export const getReviewQueue = ({ batchId, state = "OPEN", limit = 50 } = {}) => {
  const params = new URLSearchParams({ state, limit });
  if (batchId) params.set("batch_id", batchId);
  return req(`${API}/review/queue?${params}`);
};

export const submitReviewAction = (recordId, { batchId, action, note }) =>
  req(`${API}/review/${encodeURIComponent(recordId)}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch_id: batchId, action, note }),
  });

// ---- runs over uploaded data ----------------------------------------
export const createRun = (label) =>
  req(`${API}/runs`, { method: "POST", body: JSON.stringify({ label }) });

export const listRuns = () => req(`${API}/runs`);

export const getRun = (runId) => req(`${API}/runs/${runId}`);

export async function uploadSource(runId, file, sourceType) {
  // FormData sets its own multipart boundary, so the JSON content-type
  // default must not be applied here.
  const form = new FormData();
  form.append("file", file);
  form.append("source_type", sourceType);
  const res = await fetch(`${API}/runs/${runId}/sources`, { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

export const updateMapping = (runId, sourceId, payload) =>
  req(`${API}/runs/${runId}/sources/${sourceId}/mapping`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

export const removeSource = (runId, sourceId) =>
  req(`${API}/runs/${runId}/sources/${sourceId}`, { method: "DELETE" });

export const executeRun = (runId, label) =>
  req(`${API}/runs/${runId}/execute`, { method: "POST", body: JSON.stringify({ label }) });

export const exportRunUrl = (runId, outcome) =>
  `${API}/runs/${runId}/export${outcome ? `?outcome=${outcome}` : ""}`;

export const verifyChain = () => req(`${API}/audit/verify`);

export const adminReset = () => req(`${API}/admin/reset`, { method: "POST" });

export function streamAudit(onEvent, since) {
  const url = since === undefined ? `${API}/audit/stream` : `${API}/audit/stream?since=${since}`;
  const es = new EventSource(url);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* ignore malformed keep-alive frames */
    }
  };
  return () => es.close();
}

// ---- multi-source ingestion -----------------------------------------
/**
 * Upload N files against ONE run.
 *
 * The backend classifies each file after upload — the operator is never
 * asked to declare what a file is before it has been read. Two shapes are
 * tolerated because the ingestion endpoint is being widened while this
 * screen is in use: the multi-file form (`files`, one request) and the
 * older single-file form (`file`, one request each). Nothing here invents
 * a classification: when the backend cannot classify, the file comes back
 * flagged for the user to confirm, which is the honest fallback.
 */
export async function uploadSources(runId, files) {
  const list = Array.from(files || []);
  if (list.length === 0) return { sources: [], errors: [] };

  const multi = new FormData();
  for (const f of list) multi.append("files", f);
  multi.append("source_type", "AUTO");

  const res = await fetch(`${API}/runs/${runId}/sources`, { method: "POST", body: multi });
  if (res.ok) {
    const body = await res.json().catch(() => ({}));
    const sources = Array.isArray(body) ? body : body.sources;
    // A single object back means the endpoint read only one of the files.
    if (Array.isArray(sources) && sources.length === list.length) {
      return { sources, errors: body.errors || [] };
    }
  }
  return uploadSourcesSequentially(runId, list);
}

async function uploadOne(runId, file, sourceType) {
  const form = new FormData();
  form.append("file", file);
  if (sourceType) form.append("source_type", sourceType);
  const res = await fetch(`${API}/runs/${runId}/sources`, { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

async function uploadSourcesSequentially(runId, list) {
  const sources = [];
  const errors = [];
  // Once the backend has said it does not know "AUTO", asking again for
  // every remaining file just produces a row of 400s in the console.
  let autoSupported = true;
  for (const file of list) {
    if (autoSupported) {
      try {
        sources.push(await uploadOne(runId, file, "AUTO"));
        continue;
      } catch {
        autoSupported = false;
      }
    }
    // No auto-classifier on this build. Upload the file unclassified and
    // let the inventory ask the operator, rather than picking for them.
    try {
      const s = await uploadOne(runId, file, undefined);
      sources.push({ ...s, detected_source_type: null, detection_confidence: null, needs_confirmation: true });
    } catch (e) {
      errors.push({ filename: file.name, detail: e.message });
    }
  }
  return { sources, errors };
}

export const getRunPlan = (runId) => req(`${API}/runs/${runId}/plan`);

/**
 * Confirm or correct the plan.
 *
 * `sources` answers "what is this file, and which side is it on?" and is
 * the gate the classifier defers to: a file it was unsure about cannot be
 * reconciled on until this has been answered. `relationships` confirms
 * proposed pairs. Both default to empty so a bare confirmation is legal.
 */
export const putRunPlan = (runId, { sources = [], relationships = [], confirmed } = {}) =>
  req(`${API}/runs/${runId}/plan`, {
    method: "PUT",
    body: JSON.stringify({ sources, relationships, ...(confirmed == null ? {} : { confirmed }) }),
  });

export const getBreakpoints = (batchId) => req(`${API}/batch/${batchId}/breakpoints`);

export const investigateRecord = (recordId, batchId) =>
  req(
    `${API}/records/${encodeURIComponent(recordId)}/investigate${
      batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""
    }`,
    { method: "POST", body: JSON.stringify({}) }
  );

export const getAiHealth = () => req(`${API}/ai/health`);
