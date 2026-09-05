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

// `since` matters more than it looks: without it the API returns the OLDEST
// events, so on a ledger with 20k events the audit view showed ancient
// history and a human action written seconds earlier never appeared.
export const getAuditLog = (limit = 200, since = 0) =>
  req(`${API}/audit/log?limit=${limit}&since=${since}`);

/**
 * The review queue.
 *
 * `limit` defaults high enough that a normal run's whole open queue comes
 * back in one request. The summary counts every open record, so a page
 * shorter than the summary is two true numbers that read as a
 * contradiction; the response carries `total`, `returned` and `limit` so
 * the screen can state its scope instead of leaving the reader to guess.
 */
export const getReviewQueue = ({ batchId, state = "OPEN", limit = 200, offset = 0 } = {}) => {
  const params = new URLSearchParams({ state, limit, offset });
  if (batchId) params.set("batch_id", batchId);
  return req(`${API}/review/queue?${params}`);
};

/** The whole queue in that state as a downloadable spreadsheet. */
export const reviewQueueExportUrl = ({ batchId, state = "OPEN", format = "csv" } = {}) => {
  const params = new URLSearchParams({ state, format });
  if (batchId) params.set("batch_id", batchId);
  return `${API}/review/queue/export?${params}`;
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

/**
 * Download the run's results.
 *
 * `outcome` narrows the file to the filter on screen, so the download
 * matches what the operator is looking at. `format` is "csv" or "xlsx";
 * both carry identical columns, evidence included.
 */
export const exportRunUrl = (runId, outcome, format = "csv") => {
  const params = new URLSearchParams({ format });
  if (outcome) params.set("outcome", outcome);
  return `${API}/runs/${runId}/export?${params}`;
};

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
    const errors = normaliseUploadErrors(body.errors);
    // Accepted + rejected has to account for every file that was sent.
    // Previously this required `sources.length === list.length`, so a
    // batch where one file was rejected fell through to the sequential
    // path and re-uploaded the files that had ALREADY been stored — one
    // junk file in a drop silently duplicated every good file beside it.
    if (Array.isArray(sources) && sources.length + errors.length === list.length) {
      return { sources, errors };
    }
  } else if (res.status === 400 && list.length === 1) {
    // One file, refused, with the reason already in `detail`. Retrying it
    // twice down the sequential path only produces the same refusal twice
    // more in the network log.
    const body = await res.json().catch(() => ({}));
    if (body.detail) {
      return { sources: [], errors: [{ filename: list[0].name, detail: body.detail, status: 400 }] };
    }
  }
  return uploadSourcesSequentially(runId, list);
}

/**
 * One shape for a rejected file, whatever reported it.
 *
 * The multi-file endpoint reports a rejection as `error`; the
 * single-file path throws a message. Both are carried as `detail` so the
 * inventory can always say what was wrong with the file — an upload that
 * failed for a reason the screen renders as "undefined" is worse than no
 * message at all.
 */
function normaliseUploadErrors(errors) {
  if (!Array.isArray(errors)) return [];
  return errors.map((e) => ({
    ...e,
    filename: e.filename || "file",
    detail: e.detail || e.error || e.message || "could not be read",
  }));
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
      errors.push({ filename: file.name, detail: e.message, status: e.status });
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

// ---- sample workspace, live run progress, provider status ------------

/** One click: a run pre-loaded with every file in the demo workspace,
 *  ingested through the same path an upload takes. */
export const createSampleRun = () => req(`${API}/runs/sample`, { method: "POST" });

/** Real pipeline state, derived from execution — never simulated. A count
 *  of `null` means the backend does not know it yet, and the UI must render
 *  nothing rather than a placeholder. */
export const getRunProgress = (runId) => req(`${API}/runs/${encodeURIComponent(runId)}/progress`);

/** Product-facing provider status: primary, fallback, last success. No keys,
 *  no model ids in the primary surface. */
export const getAiStatus = () => req(`${API}/ai/status`);
