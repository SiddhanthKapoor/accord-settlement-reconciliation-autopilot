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

export const verifyChain = () => req(`${API}/audit/verify`);

export const adminReset = () => req(`${API}/admin/reset`, { method: "POST" });

export function streamAudit(onEvent) {
  const es = new EventSource(`${API}/audit/stream`);
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* ignore malformed keep-alive frames */
    }
  };
  return () => es.close();
}
