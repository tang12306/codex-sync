// 共享 UI：统一动作执行器与按钮包装。供各视图复用。
import { h } from "./dom.js";
import { runAction } from "./api.js";
import { refreshStatus } from "./poller.js";
import { showToast } from "./toast.js";

export function formatDateTime(value) {
  if (value == null || value === "") return "-";
  const raw = String(value).trim();
  if (!raw) return "-";
  const normalized = /^\d{4}-\d{2}-\d{2}T/.test(raw) && !/(Z|[+-]\d{2}:\d{2})$/.test(raw) ? `${raw}Z` : raw;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString();
}

// 统一动作执行器：runAction → toast → 可选 refreshStatus。
export function makeRun(store) {
  return async function run(name, opts = {}) {
    const { payload = {}, okMsg, errMsg, refresh = true, label } = opts;
    try {
      const result = await runAction(name, payload);
      const failed = Boolean(result && (result.error || result.success === false));
      const failureReason = result?.error || result?.reason || result?.message || result?.upload?.error || result?.package?.error || "未完成";
      showToast(
        failed
          ? `${errMsg || `失败：${label || name}`}：${failureReason}`
          : okMsg || `已执行：${label || name}`,
        failed ? "error" : "success"
      );
      if (refresh) await refreshStatus(store);
      return result;
    } catch (e) {
      showToast(String(e.message || e), "error");
      throw e;
    }
  };
}

// 包一层按钮：点击期间禁用，吞掉异常（已由 run 提示）。
export function actionButton(label, cls, handler) {
  const b = h("button", { class: `btn ${cls}`, type: "button" }, label);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      await handler();
    } catch {
      /* 已提示 */
    } finally {
      b.disabled = false;
    }
  });
  return b;
}
