// 共享 UI：结构化操作结果 + 统一动作执行器。供各视图复用。
import { h } from "./dom.js";
import { runAction } from "./api.js";
import { refreshStatus } from "./poller.js";
import { showToast } from "./toast.js";

function summarize(value) {
  if (value == null) return "尚无操作记录";
  if (typeof value === "string") return value || "操作完成";
  if (value.loading) return value.message || "处理中…";
  if (value.error) return `失败：${value.error}`;
  if (value.success === false) return `未完成：${value.reason || value.message || "请展开查看详情"}`;
  if (value.skipped) return value.reason || "无变化，已跳过";
  if (value.success === true || value.sent || value.created || value.saved || value.imported != null || value.moved != null || value.restored != null) {
    return "操作已完成";
  }
  return "最近操作已记录";
}

function formatOutput(value) {
  return value == null ? "尚无详情" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function labelFor(key) {
  const labels = {
    archive: "归档文件",
    archive_bytes: "归档大小",
    backup_id: "备份 ID",
    branch_id: "分支",
    changed: "内容变化",
    code: "退出码",
    content_digest: "内容指纹",
    created: "已创建",
    db_backup: "数据库备份",
    device_id: "设备",
    dirty: "未同步",
    downloaded: "已下载",
    error: "错误",
    imported: "导入数量",
    included_bytes: "包含大小",
    included_count: "包含文件",
    last_backup_at: "最近备份",
    last_change_at: "最近变化",
    message: "消息",
    moved: "并入数量",
    needs_upload: "需要上传",
    package_deferred: "等待打包",
    path: "路径",
    queued: "已入队",
    reason: "原因",
    remaining: "剩余",
    restored: "还原数量",
    sent: "已发送",
    skipped: "已跳过",
    snapshot_id: "快照 ID",
    status: "状态",
    sync_state: "同步状态",
    target: "目标渠道",
    updated: "已更新",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function valueText(value) {
  if (value == null || value === "") return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return `${value.length} 项`;
  return "查看详情";
}

function toneFor(value) {
  if (!isObject(value)) return "";
  if (value.error || value.success === false) return "danger";
  if (value.loading) return "warn";
  if (value.needs_close || value.needs_upload || value.dirty || value.package_deferred) return "warn";
  if (value.skipped) return "warn";
  if (value.success === true || value.sent || value.created || value.imported != null || value.moved != null || value.restored != null) return "ok";
  return "";
}

function statusText(value) {
  if (!isObject(value)) return "记录";
  if (value.error) return "失败";
  if (value.loading) return "处理中";
  if (value.success === false) return "未完成";
  if (value.skipped) return "已跳过";
  if (value.queued && !value.sent) return "已排队";
  if (value.needs_upload || value.dirty || value.package_deferred) return "待处理";
  if (value.success === true || value.sent || value.created || value.imported != null || value.moved != null || value.restored != null) return "完成";
  return "记录";
}

function sectionTitle(key) {
  const titles = {
    sync: "轻量同步",
    flush_outbox: "Outbox",
    full_backup_scan: "完整备份扫描",
    device_state: "设备状态",
    package: "备份包",
    upload: "上传",
    local: "本机",
    remote: "远端",
  };
  return titles[key] || labelFor(key);
}

function importantEntries(value) {
  if (!isObject(value)) return [];
  const skip = new Set([
    "files",
    "items",
    "stdout",
    "stderr",
    "response",
    "preflight_backup",
    "db_backup",
    "manifest",
    "metadata",
    "remote",
    "device_state",
    "full_backups",
    "snapshots",
    "conversations",
  ]);
  return Object.entries(value)
    .filter(([key, item]) => !skip.has(key) && !isObject(item) && !Array.isArray(item))
    .slice(0, 8);
}

function renderSection(key, value) {
  if (!isObject(value)) {
    return h(
      "div",
      { class: "result-section" },
      h("div", { class: "result-section-head" }, h("strong", {}, sectionTitle(key))),
      h("div", { class: "result-note" }, valueText(value))
    );
  }
  const tone = toneFor(value);
  const entries = importantEntries(value);
  const childSections = Object.entries(value)
    .filter(([, item]) => isObject(item))
    .slice(0, 3)
    .map(([childKey, item]) => renderSection(childKey, item));
  const arrays = Object.entries(value)
    .filter(([, item]) => Array.isArray(item) && item.length)
    .slice(0, 2)
    .map(([arrayKey, item]) =>
      h("div", { class: "result-note" }, `${labelFor(arrayKey)}：${item.length} 项`)
    );

  return h(
    "div",
    { class: "result-section" },
    h(
      "div",
      { class: "result-section-head" },
      h("strong", {}, sectionTitle(key)),
      h("span", { class: `badge ${tone}` }, statusText(value))
    ),
    entries.length
      ? h(
          "div",
          { class: "result-facts" },
          ...entries.map(([entryKey, item]) =>
            h("div", { class: "result-fact" }, h("span", {}, labelFor(entryKey)), h("b", {}, valueText(item)))
          )
        )
      : h("div", { class: "result-note" }, summarize(value)),
    ...arrays,
    ...childSections
  );
}

function renderStructured(value) {
  if (value == null) {
    return h("div", { class: "empty compact" }, "还没有操作结果");
  }
  if (!isObject(value)) {
    return h("div", { class: "result-note" }, valueText(value));
  }
  if (value.loading) {
    return h("div", { class: "spinner-wrap" }, h("span", { class: "spinner" }), h("span", {}, value.message || "处理中…"));
  }
  const entries = Object.entries(value);
  const compoundKeys = new Set(["sync", "flush_outbox", "full_backup_scan", "device_state", "package", "upload", "local", "remote"]);
  const objectEntries = entries.filter(([key, item]) => compoundKeys.has(key) && isObject(item));
  if (objectEntries.length) {
    return h("div", { class: "result-stack" }, ...objectEntries.map(([key, item]) => renderSection(key, item)));
  }
  return renderSection("结果", value);
}

// 兼容旧调用名：返回 { card, setOutput }。主区域渲染结构化结果，原始 JSON 只作为诊断详情。
export function consoleCard(title = "输出") {
  const summary = h("span", { class: "result-summary" }, "尚无操作记录");
  const body = h("div", { class: "result-body" }, renderStructured(null));
  const pre = h("pre", {}, "尚无详情");

  function setOutput(value) {
    summary.textContent = summarize(value);
    body.replaceChildren(renderStructured(value));
    pre.textContent = formatOutput(value);
  }

  const copyBtn = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "复制");
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pre.textContent);
      showToast("已复制到剪贴板", "success");
    } catch (e) {
      showToast("复制失败：" + (e.message || e), "error");
    }
  });
  const clearBtn = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "清空");
  clearBtn.addEventListener("click", () => setOutput(null));

  const raw = h(
    "details",
    { class: "collapse diagnostic-raw" },
    h("summary", {}, "原始详情"),
    h("div", { class: "collapse-body" }, h("div", { class: "card-actions" }, copyBtn, clearBtn), h("div", { class: "console-body" }, pre))
  );

  const card = h(
    "div",
    { class: "card result-card" },
    h("div", { class: "card-title" }, h("span", {}, title), summary),
    body,
    raw
  );

  return { card, setOutput };
}

// 统一动作执行器：runAction → 写 store.console → toast → 可选 refreshStatus。
export function makeRun(store) {
  return async function run(name, opts = {}) {
    const { payload = {}, okMsg, errMsg, refresh = true, label } = opts;
    try {
      const result = await runAction(name, payload);
      store.setState({ console: result });
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
      store.setState({ console: { error: String(e.message || e) } });
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
