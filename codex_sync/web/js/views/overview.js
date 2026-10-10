// 首页：先给出需要处理的提醒，再展示同步状态总览和关键指标，最后是各任务入口。
import { h, asObject, fill } from "../dom.js";
import { runAction } from "../api.js";
import { makeRun, actionButton } from "../ui.js";
import { showToast } from "../toast.js";
import { navigate } from "../router.js";
import { checkConnection } from "../connection.js";
import { formatCountdown, formatDateTime } from "../format.js";

const HEALTH_CACHE_MS = 60 * 1000;
const APP_UPDATE_CACHE_MS = 60 * 60 * 1000;

const ICONS = {
  conversations: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  project: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  backups: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M1 12h3M20 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>',
};

function icon(name) {
  return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
}

export function mount(root, store) {
  const run = makeRun(store);
  const alertWrap = h("div", { class: "alert-stack" });
  const appUpdateWrap = h("div", { class: "contents" });
  const connectionWrap = h("div", { class: "contents" });
  const healthWrap = h("div", { class: "section" });
  const statusWrap = h("div", { class: "section" });
  alertWrap.append(appUpdateWrap, connectionWrap);

  fill(root,
    alertWrap,
    healthWrap,
    statusWrap,
    h("div", { class: "section-title" }, "常用任务"),
    h(
      "div",
      { class: "task-home" },
      taskCard("conversations", "对话与渠道", "浏览、搜索和导出对话，把不同渠道的对话整理到一起。", "打开对话"),
      taskCard("project", "项目备份", "把项目代码打包上传到同步服务器，按版本预览和恢复。", "备份项目"),
      taskCard("backups", "对话备份", "Windows / WSL 完整对话备份、云端备份管理和导入恢复。", "管理备份"),
      taskCard("settings", "系统设置", "服务器连接、备份策略、后台任务、开机自启与加密密钥。", "打开设置")
    )
  );

  const cachedOverview = asObject(store.getState().overview);
  const saveOverview = (patch) => {
    store.setState((state) => ({ overview: { ...asObject(state.overview), ...patch } }));
  };

  // ---- 应用更新 ----
  let appUpdate = cachedOverview.appUpdate || null;
  let appUpdateLoadedAt = Number(cachedOverview.appUpdateLoadedAt || 0);
  const renderAppUpdate = () => {
    fill(appUpdateWrap, buildAppUpdate({ appUpdate, refresh: refreshAppUpdate, run }));
  };
  async function refreshAppUpdate(force = false) {
    if (!force && appUpdateLoadedAt && Date.now() - appUpdateLoadedAt < APP_UPDATE_CACHE_MS) {
      renderAppUpdate();
      return;
    }
    try {
      appUpdate = await runAction("app-update-check", { force });
      if (appUpdate.update_available) {
        const key = `codex-sync-update-${appUpdate.latest_version || "latest"}`;
        try {
          if (!sessionStorage.getItem(key)) {
            showToast(`发现 Codex Sync ${appUpdate.latest_version}，可以下载新版安装包`, "info");
            sessionStorage.setItem(key, "1");
          }
        } catch {
          /* 存储不可用时不提示也无妨，首页卡片仍会显示 */
        }
      }
    } catch (e) {
      appUpdate = { success: false, error: String(e.message || e) };
    }
    appUpdateLoadedAt = Date.now();
    saveOverview({ appUpdate, appUpdateLoadedAt });
    renderAppUpdate();
  }

  // ---- 服务器连接（结果由 connection.js 统一维护） ----
  const renderConnection = () => {
    const { status, connection } = store.getState();
    fill(connectionWrap, buildConnectionAlert({ status, connection, store }));
  };

  // ---- 同步状态总览 ----
  let health = cachedOverview.health || null;
  let healthLoadedAt = Number(cachedOverview.healthLoadedAt || 0);
  let healthBusy = false;
  let healthExpanded = cachedOverview.healthExpanded !== false;
  const renderHealth = () => {
    fill(healthWrap,
      buildHealth({
        health,
        busy: healthBusy,
        expanded: healthExpanded,
        toggle: () => {
          healthExpanded = !healthExpanded;
          saveOverview({ healthExpanded });
          renderHealth();
        },
        store,
        run,
        refresh: () => refreshHealth(true),
      })
    );
  };
  async function refreshHealth(force = false) {
    if (!force && healthLoadedAt && Date.now() - healthLoadedAt < HEALTH_CACHE_MS) {
      renderHealth();
      return;
    }
    healthBusy = true;
    renderHealth();
    try {
      health = await runAction("sync-health", { force });
    } catch (e) {
      health = { success: false, error: String(e.message || e) };
    }
    healthLoadedAt = Date.now();
    saveOverview({ health, healthLoadedAt, healthExpanded });
    healthBusy = false;
    renderHealth();
  }

  const renderStatus = () => {
    const { status, statusError, connection } = store.getState();
    fill(statusWrap, buildStatus(status, statusError, connection));
  };

  renderAppUpdate();
  renderConnection();
  renderHealth();
  renderStatus();
  refreshAppUpdate(false);
  refreshHealth();
  checkConnection(store);

  const onGlobalRefresh = () => refreshHealth(true);
  window.addEventListener("codex-sync:refresh", onGlobalRefresh);
  const unsubs = [
    store.select((s) => s.status, () => {
      renderStatus();
      renderConnection();
    }),
    store.select((s) => s.statusError, renderStatus),
    store.select((s) => s.connection, () => {
      renderStatus();
      renderConnection();
    }),
  ];
  return () => {
    window.removeEventListener("codex-sync:refresh", onGlobalRefresh);
    unsubs.forEach((u) => u());
  };
}

function alertCard(tone, title, badge, desc, actions) {
  return h(
    "div",
    { class: `card tone-${tone}`, role: tone === "danger" ? "alert" : null },
    h("div", { class: "card-title" }, h("span", {}, title), badge ? h("span", { class: `badge ${tone === "info" ? "info" : tone}` }, badge) : null),
    h("p", { class: "card-desc" }, desc),
    actions && actions.length ? h("div", { class: "card-actions" }, ...actions) : null
  );
}

function buildAppUpdate({ appUpdate, refresh, run }) {
  if (!appUpdate || appUpdate.success === false || !appUpdate.update_available) return h("div", { hidden: true });
  const asset = asObject(appUpdate.windows_asset);
  return alertCard(
    "info",
    "Codex Sync 有新版本",
    appUpdate.latest_version || "新版本",
    `当前版本 ${appUpdate.current_version || "-"}，最新版本 ${appUpdate.latest_version || "-"}。${asset.name ? `安装包：${asset.name}。` : "发布页暂未识别到 Windows 安装包。"}`,
    [
      actionButton("下载新版", "btn-primary btn-sm", () =>
        run("app-update-download", { payload: { force: false }, okMsg: "新版安装包已下载", errMsg: "下载失败", refresh: false })
      ),
      actionButton("打开发布页", "btn-ghost btn-sm", () =>
        run("app-update-open", { payload: { force: false }, okMsg: "已打开发布页", errMsg: "打开失败", refresh: false })
      ),
      actionButton("重新检查", "btn-ghost btn-sm", () => refresh(true)),
    ]
  );
}

function buildConnectionAlert({ status, connection, store }) {
  if (!status) return h("div", { hidden: true });
  const cfg = asObject(status.config);
  const recheck = actionButton("重新检查", "btn-ghost btn-sm", () => checkConnection(store, { force: true }));
  if (!cfg.server_url) {
    return alertCard("info", "还没有配置同步服务器", null, "填写服务器地址和 API Token 后，才能上传备份并在多台设备间同步。也可以在系统设置里通过 SSH 一键部署服务器。", [
      h("button", { class: "btn btn-primary btn-sm", type: "button", onClick: () => navigate("settings", "server") }, "去配置服务器"),
    ]);
  }
  const conn = asObject(connection);
  const result = asObject(conn.result);
  if (conn.state === "warn") {
    const server = asObject(result.server);
    const expected = asObject(result.expected);
    const missing = Array.isArray(result.missing_features) ? result.missing_features : [];
    return alertCard(
      "warn",
      "同步服务器需要更新",
      "版本过旧",
      `服务器 API 版本 ${server.api_version || "未知"}，本机需要 ${expected.api_version || "-"}。${missing.length ? `缺少功能：${missing.join("、")}。` : ""}更新前部分备份和恢复功能可能不可用。`,
      [h("button", { class: "btn btn-primary btn-sm", type: "button", onClick: () => navigate("settings", "server") }, "去更新服务器"), recheck]
    );
  }
  if (conn.state === "down") {
    return alertCard("danger", "无法连接同步服务器", "连接失败", `${result.error || "服务器没有响应"}。请检查网络、服务器地址和 API Token。`, [
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: () => navigate("settings", "server") }, "检查服务器设置"),
      recheck,
    ]);
  }
  return h("div", { hidden: true });
}

function taskCard(route, title, desc, action) {
  return h(
    "a",
    { class: "task-home-card", href: `#/${route}` },
    h("span", { class: "task-home-icon", html: icon(route) }),
    h("strong", {}, title),
    h("p", {}, desc),
    h("b", {}, `${action} →`)
  );
}

function buildHealth({ health, busy, expanded, toggle, store, run, refresh }) {
  const card = h("div", { class: "card" });
  const title = h(
    "div",
    { class: "card-title" },
    h("span", {}, "同步状态总览"),
    h(
      "div",
      { class: "card-title-actions" },
      h("button", { class: `btn btn-ghost btn-sm${busy ? " is-busy" : ""}`, type: "button", disabled: busy, onClick: refresh }, busy ? "检查中" : "重新检查"),
      health ? h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-expanded": expanded ? "true" : "false", onClick: toggle }, expanded ? "收起明细" : "查看明细") : null
    )
  );

  if (!health) {
    fill(card, title, h("div", { class: "spinner-wrap compact" }, h("span", { class: "spinner" }), h("span", {}, "正在检查对话备份、WSL、项目备份和服务器状态…")));
    return card;
  }

  const local = asObject(health.local);
  const pending = Array.isArray(health.pending_devices) ? health.pending_devices : [];
  const needs = Boolean(local.needs_upload);
  const summary = asObject(health.summary);
  const autoScan = asObject(health.auto_scan);
  const items = Array.isArray(health.status_items) ? health.status_items : legacyHealthItems(health);
  const summaryText = summary.text || (health.success ? "状态已检查" : `服务器暂不可用或未配置：${health.error || "未知原因"}`);
  const summaryTone = summary.tone || (!health.success ? "danger" : needs || pending.length ? "warn" : "ok");
  const nodes = [
    title,
    h(
      "div",
      { class: `sync-summary ${summaryTone}` },
      h("span", { class: "sync-summary-main" }, summaryText),
      needs
        ? actionButton("立即补做并上传", "btn-primary btn-sm", async () => {
            const cfg = asObject(store.getState().status?.config);
            await run("full-backup-now", {
              payload: { upload: true, allow_plaintext_upload: !!cfg.full_backup_allow_plaintext_upload },
              okMsg: "已补做备份并上传",
              errMsg: "补做备份失败",
            });
            await refresh();
          })
        : null
    ),
    expanded ? buildHealthBoard({ items, autoScan, store, run, refresh }) : null,
  ];
  fill(card, ...nodes.filter(Boolean));
  return card;
}

function legacyHealthItems(health) {
  const local = asObject(health.local);
  const pending = Array.isArray(health.pending_devices) ? health.pending_devices : [];
  return [
    {
      id: "windows-conversations",
      group: "对话备份",
      name: "Windows 对话",
      status: local.needs_upload ? "有未上传变更" : "已同步",
      tone: local.needs_upload ? "warn" : "ok",
      detail: "Windows 完整对话备份状态。",
      last_uploaded_at: local.last_uploaded_at,
      action: "full-backup-now",
      action_label: "扫描并上传",
    },
    {
      id: "remote-devices",
      group: "远端状态",
      name: "其它设备",
      status: pending.length ? `${pending.length} 台待处理` : "正常",
      tone: pending.length ? "warn" : "ok",
      detail: pending.length ? "有其它设备还有未上传或冲突的变更。" : "其它设备正常。",
    },
  ];
}

function buildHealthBoard({ items, autoScan, store, run, refresh }) {
  return h(
    "div",
    { class: "sync-board" },
    h(
      "div",
      { class: "sync-board-head" },
      h("strong", {}, "明细"),
      h("span", {}, autoScan.enabled ? `自动扫描：${formatCountdown(autoScan.next_run_in_seconds) || "已开启"}` : "自动扫描未开启")
    ),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "table sync-table" },
        h(
          "thead",
          {},
          h("tr", {}, h("th", {}, "模块"), h("th", {}, "对象"), h("th", {}, "状态"), h("th", {}, "最近检查"), h("th", {}, "最近上传"), h("th", {}, "下次处理"), h("th", {}, h("span", { class: "sr-only" }, "操作")))
        ),
        h("tbody", {}, ...items.map((item) => healthRow(item, autoScan, store, run, refresh)))
      )
    )
  );
}

function healthRow(item, autoScan, store, run, refresh) {
  const row = asObject(item);
  const tone = row.tone || "";
  return h(
    "tr",
    {},
    h("td", {}, h("span", { class: "sync-group" }, row.group || "-")),
    h("td", {}, h("div", { class: "sync-object" }, h("strong", {}, row.name || "-"), h("span", { title: row.scope || "" }, row.detail || row.scope || ""))),
    h("td", {}, h("span", { class: `badge ${tone}` }, row.status || "-")),
    h("td", {}, shortTime(row.last_checked_at || row.checked_at)),
    h("td", {}, shortTime(row.last_uploaded_at)),
    h("td", {}, nextRunText(row, autoScan)),
    h("td", {}, healthAction(row, store, run, refresh))
  );
}

function healthAction(item, store, run, refresh) {
  const row = asObject(item);
  const label = row.action_label || (row.href ? "查看" : "");
  if (!label) return h("span", { class: "muted" }, "");
  if (!row.action && row.href) {
    return h("a", { class: "btn btn-ghost btn-sm", href: row.href }, label);
  }
  return actionButton(label, "btn-ghost btn-sm", async () => {
    const cfg = asObject(store.getState().status?.config);
    const payload = { ...asObject(row.payload) };
    if (row.action === "full-backup-now" || row.action === "wsl-full-backup") {
      payload.upload = true;
      payload.allow_plaintext_upload = !!cfg.full_backup_allow_plaintext_upload;
    }
    await run(row.action, { payload, okMsg: `${row.name || "状态项"}已处理`, errMsg: `${row.name || "状态项"}处理失败`, refresh: false });
    await refresh();
  });
}

function shortTime(value) {
  return value ? formatDateTime(value) : "-";
}

function nextRunText(item, autoScan) {
  const row = asObject(item);
  if (row.next_run_in_seconds != null) return formatCountdown(row.next_run_in_seconds) || row.next_run_at || "-";
  if (row.next_run_at) return shortTime(row.next_run_at);
  if (["windows-conversations", "project-backup"].includes(row.id) && autoScan.enabled) {
    return formatCountdown(autoScan.next_run_in_seconds) || shortTime(autoScan.next_run);
  }
  if (String(row.id || "").startsWith("wsl-")) return "手动";
  return "-";
}

function serverMetric(cfg, connection) {
  if (!cfg.server_url) return ["未配置", "warn"];
  let host = cfg.server_url;
  try {
    host = new URL(cfg.server_url).host || host;
  } catch {
    /* 非标准地址时原样显示 */
  }
  const tone = { ok: "ok", warn: "warn", down: "danger" }[asObject(connection).state] || "";
  return [host, tone];
}

function buildStatus(status, statusError, connection) {
  if (!status) {
    return statusError
      ? h("div", { class: "status-line error" }, `状态加载失败：${statusError}`)
      : h("div", { class: "spinner-wrap compact" }, h("span", { class: "spinner" }), h("span", {}, "正在读取本机状态…"));
  }
  const cfg = asObject(status.config);
  const hooks = asObject(status.hooks);
  const hookCount = Array.isArray(hooks.events) ? hooks.events.length : 0;
  const [serverText, serverTone] = serverMetric(cfg, connection);
  return h(
    "div",
    { class: "grid grid-auto" },
    metric("同步服务器", serverText, serverTone, cfg.server_url || ""),
    metric("Codex Hooks", hookCount ? `${hookCount} 个事件` : "未安装", hookCount ? "ok" : "warn"),
    metric("后台守护进程", status.daemon_running ? "运行中" : "未运行", status.daemon_running ? "ok" : ""),
    metric(
      "上次自动上传",
      status.last_full_backup_uploaded_at ? shortTime(status.last_full_backup_uploaded_at) : "尚无",
      status.last_full_backup_uploaded_at ? "ok" : "warn"
    )
  );
}

function metric(label, value, tone, title = "") {
  return h(
    "div",
    { class: "metric" },
    h("div", { class: "metric-label" }, label),
    h("div", { class: `metric-value ${tone || ""}`, title: title || String(value) }, value)
  );
}
