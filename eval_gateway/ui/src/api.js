const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:9001";

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parseResponse(resp) {
  if (!resp.ok) {
    let text = "";
    try {
      text = await resp.text();
    } catch {
      text = resp.statusText;
    }
    throw new Error(text || `Request failed (${resp.status})`);
  }
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) return resp.json();
  return resp.text();
}

export async function login(username, password) {
  const resp = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return parseResponse(resp);
}

export async function getProtocols(token) {
  const resp = await fetch(`${API_BASE}/protocols`, {
    headers: authHeaders(token),
  });
  return parseResponse(resp);
}

export async function createRun(studyId, token) {
  const resp = await fetch(`${API_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify({ study_id: studyId }),
  });
  return parseResponse(resp);
}

export async function getRuns(token, studyId = "") {
  const q = studyId ? `?study_id=${encodeURIComponent(studyId)}` : "";
  const resp = await fetch(`${API_BASE}/runs${q}`, {
    headers: authHeaders(token),
  });
  return parseResponse(resp);
}

export async function getRun(runId, token) {
  const resp = await fetch(`${API_BASE}/runs/${runId}`, {
    headers: authHeaders(token),
  });
  return parseResponse(resp);
}

export async function downloadArtifact(runId, artifactId, filename, token) {
  const resp = await fetch(`${API_BASE}/runs/${runId}/artifact/${artifactId}`, {
    headers: authHeaders(token),
  });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt || "Download failed");
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || `artifact_${artifactId}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export async function getRunLog(runId, token) {
  const resp = await fetch(`${API_BASE}/runs/${runId}/log`, {
    headers: authHeaders(token),
  });
  return parseResponse(resp);
}

export { API_BASE };
