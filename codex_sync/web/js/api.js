// 后端 API 封装：统一 fetch、错误解析、结构防御。
// 契约（不可变）：GET /api/status、POST /api/config、POST /api/action。

function sessionHeaders() {
  const token = document.querySelector('meta[name="codex-sync-desktop-token"]')?.content || "";
  return {
    "Content-Type": "application/json",
    ...(token ? { "X-Codex-Sync-Desktop-Token": token } : {}),
  };
}

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...sessionHeaders(), ...(options.headers || {}) } });
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    throw new Error(data.error || response.statusText || `HTTP ${response.status}`);
  }
  return data && typeof data === "object" ? data : { raw: data };
}

export function getStatus() {
  return request("/api/status");
}

export function saveConfig(payload) {
  return request("/api/config", { method: "POST", body: JSON.stringify(payload || {}) });
}

export function runAction(name, payload = {}) {
  return request("/api/action", {
    method: "POST",
    body: JSON.stringify({ name, ...payload }),
  });
}
