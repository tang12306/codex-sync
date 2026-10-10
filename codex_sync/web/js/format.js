// 格式化与小部件：各视图共用，避免每个文件各写一份不一致的版本。
import { h } from "./dom.js";

// 后端时间多为不带时区的 UTC ISO 字符串，补上 Z 再按本地时区显示。
export function formatDateTime(value) {
  if (value == null || value === "") return "-";
  const raw = String(value).trim();
  if (!raw) return "-";
  const normalized = /^\d{4}-\d{2}-\d{2}T/.test(raw) && !/(Z|[+-]\d{2}:\d{2})$/.test(raw) ? `${raw}Z` : raw;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString();
}

// Unix 秒时间戳 → 本地时间。
export function formatEpoch(seconds) {
  if (!seconds) return "";
  const date = new Date(Number(seconds) * 1000);
  return Number.isNaN(date.getTime()) ? String(seconds) : date.toLocaleString();
}

export function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = n / 1024;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[i]}`;
}

export function formatCountdown(value) {
  if (value == null || value === "") return "";
  const seconds = Math.max(0, Number(value) || 0);
  if (seconds < 60) return "1 分钟内";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟后`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `约 ${hours} 小时 ${rest} 分钟后` : `约 ${hours} 小时后`;
}

// 路径只取最后一段（项目名），完整路径放 title 里。
export function shortCwd(path) {
  if (!path) return "-";
  const parts = String(path).replace(/^\\\\\?\\/, "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path);
}

export function fact(label, value) {
  const text = value == null || value === "" ? "-" : String(value);
  return h("div", { class: "result-fact" }, h("span", {}, label), h("b", { title: text }, text));
}

export function facts(items) {
  return h("div", { class: "result-facts" }, ...items.filter(Boolean).map(([label, value]) => fact(label, value)));
}

export function loadingSpinner(text = "正在加载…", { compact = false } = {}) {
  return h(
    "div",
    { class: `spinner-wrap${compact ? " compact" : ""}`, role: "status" },
    h("span", { class: "spinner", "aria-hidden": "true" }),
    h("span", {}, text)
  );
}

export function statusLine(text, tone = "") {
  return h("div", { class: `status-line ${tone}`.trim(), role: tone === "error" ? "alert" : null }, text);
}

export const ROLE_LABEL = {
  user: "用户",
  assistant: "助手",
  developer: "系统注入",
  system: "系统",
  reasoning: "思考",
  tool_call: "工具调用",
  tool_output: "工具输出",
};

export const CODEX_RUNNING_CONFIRM =
  "检测到 Codex 正在运行。\n\n这一步会修改 Codex 的对话数据库，需要先关闭它的后台进程（codex.exe）。" +
  "下次使用 Codex 时它会自动重启，不影响编辑器和本页面。";

export const WSL_RUNNING_CONFIRM =
  "检测到目标 WSL 里的 Codex 正在运行。\n\n导入会写入该发行版的 ~/.codex 状态库，需要先关闭它的 Codex 后台进程。";

// 复制到剪贴板：优先 Clipboard API，WebView 不允许时退回 execCommand。
export async function copyText(text) {
  const value = String(text ?? "");
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* 退回下面的兼容写法 */
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

// 后端失败结果里常见的原因字段，按优先级取第一个。
export function failureReason(result, fallback = "未完成") {
  const r = result && typeof result === "object" ? result : {};
  return r.error || r.reason || r.message || r.upload?.error || r.package?.error || fallback;
}

export function isFailed(result) {
  return Boolean(result && (result.error || result.success === false));
}
