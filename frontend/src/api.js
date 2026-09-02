const API = "/api";
const CATALOG = "/catalog";

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

export const createIntent = (body) => req(`${API}/intents`, { method: "POST", body: JSON.stringify(body) });

export const getIntent = (intentId) => req(`${API}/intents/${intentId}`);

export const recordEvidence = (intentId, body) =>
  req(`${API}/intents/${intentId}/evidence`, { method: "POST", body: JSON.stringify(body) });

export const createCommitment = (intentId, body) =>
  req(`${API}/intents/${intentId}/commitments`, { method: "POST", body: JSON.stringify(body) });

export const verifyPayment = (intentId, commitmentId, body) =>
  req(`${API}/intents/${intentId}/commitments/${commitmentId}/verify`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const executePayment = (intentId, commitmentId, body) =>
  req(`${API}/intents/${intentId}/commitments/${commitmentId}/execute`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const getAudit = (transactionId) => req(`${API}/transactions/${transactionId}/audit`);

export const getStats = () => req(`${API}/stats`);

export const verifyChain = () => req(`${API}/audit/verify`);

export const adminReset = () => req(`${API}/admin/reset`, { method: "POST" });

export const catalogReset = () => fetch(`${CATALOG}/admin/reset`, { method: "POST" });

export const catalogPatch = (merchantId, productId, fields) =>
  fetch(`${CATALOG}/admin/merchants/${merchantId}/products/${productId}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(fields),
  });

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

let counter = 0;
export function newRequestId(prefix = "req") {
  counter += 1;
  return `${prefix}-${Date.now()}-${counter}`;
}
