// 首页：按任务进入核心流程，诊断指标只做摘要。
import { h, asObject } from "../dom.js";
import { runAction } from "../api.js";
import { makeRun, actionButton, formatDateTime } from "../ui.js";
import { showToast } from "../toast.js";

export function mount(root, store) {
  const run = makeRun(store);
  const appUpdateWrap = h("div", { class: "section" });
  const versionWrap = h("div", { class: "section" });
  const healthWrap = h("div", { class: "section" });
  const statusWrap = h("div", { class: "section" });

  root.replaceChildren(
    h(
      "div",
      { class: "task-home" },
      heroCard(store),
      taskCard("01", "对话与备份", "Windows / WSL 的完整对话备份、导入迁移、渠道合并与对话浏览。", "进入管理", "#/conversations"),
      taskCard("02", "项目备份", "打包并上传当前项目代码快照到同步服务器，支持补丁包格式。", "备份项目", "#/project")
    ),
    appUpdateWrap,
    versionWrap,
    healthWrap,
    statusWrap
  );

  let appUpdate = null;
  let appUpdateBusy = false;
  const renderAppUpdate = () => {
    appUpdateWrap.replaceChildren(buildAppUpdate({ appUpdate, busy: appUpdateBusy, refresh: refreshAppUpdate, run }));
  };
  async function refreshAppUpdate(force = false) {
    appUpdateBusy = true;
    renderAppUpdate();
    try {
      appUpdate = await runAction("app-update-check", { force });
      if (appUpdate.update_available) {
        const key = `codex-sync-update-${appUpdate.latest_version || "latest"}`;
        if (!sessionStorage.getItem(key)) {
          showToast(`发现 Codex Sync ${appUpdate.latest_version}，可下载新版安装包`, "warning");
          sessionStorage.setItem(key, "1");
        }
      }
    } catch (e) {
      appUpdate = { success: false, error: String(e.message || e) };
    }
    appUpdateBusy = false;
    renderAppUpdate();
  }

  let compatibility = null;
  let compatibilityBusy = false;
  const renderCompatibility = () => {
    versionWrap.replaceChildren(buildCompatibility({ compatibility, busy: compatibilityBusy, refresh: refreshCompatibility }));
  };
  async function refreshCompatibility() {
    compatibilityBusy = true;
    renderCompatibility();
    try {
      compatibility = await runAction("server-compatibility");
    } catch (e) {
      compatibility = { success: false, compatible: false, error: String(e.message || e) };
    }
    compatibilityBusy = false;
    renderCompatibility();
  }

  let health = null;
  let healthBusy = false;
  let healthExpanded = false;
  const renderHealth = () => {
    healthWrap.replaceChildren(buildHealth({
      health,
      busy: healthBusy,
      expanded: healthExpanded,
      toggle: () => {
        healthExpanded = !healthExpanded;
        renderHealth();
      },
      store,
      run,
      refresh: refreshHealth,
    }));
  };
  async function refreshHealth() {
    healthBusy = true;
    renderHealth();
    try {
      health = await runAction("sync-health");
    } catch (e) {
      health = { success: false, error: String(e.message || e) };
    }
    healthBusy = false;
    renderHealth();
  }

  const renderStatus = () => {
    const { status, statusError } = store.getState();
    statusWrap.replaceChildren(buildStatus(status, statusError));
  };

  renderCompatibility();
  renderHealth();
  renderStatus();
  refreshAppUpdate(false);
  refreshCompatibility();
  refreshHealth();

  const u1 = store.select((s) => s.status, renderStatus);
  const u2 = store.select((s) => s.statusError, renderStatus);
  return () => {
    u1();
    u2();
  };
}

function buildAppUpdate({ appUpdate, busy, refresh, run }) {
  if (busy && !appUpdate) {
    return h("div", {});
  }
  if (!appUpdate || appUpdate.success === false || !appUpdate.update_available) {
    return h("div", {});
  }
  const asset = asObject(appUpdate.windows_asset);
  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "本地应用有新版本"), h("span", { class: "badge warn" }, appUpdate.latest_version || "新版本")),
    h(
      "p",
      { class: "card-desc" },
      `当前版本 ${appUpdate.current_version || "-"}，GitHub 最新版本 ${appUpdate.latest_version || "-"}。${asset.name ? `可下载 ${asset.name}。` : "发布页暂未识别到 Windows 包。"}`
    ),
    h(
      "div",
      { class: "card-actions" },
      actionButton("下载新版", "btn-primary", async () => {
        await run("app-update-download", {
          payload: { force: false },
          okMsg: "新版安装包已下载",
          errMsg: "下载失败",
          label: "应用更新",
          refresh: false,
        });
      }),
      actionButton("打开发布页", "btn-ghost", async () => {
        await run("app-update-open", {
          payload: { force: false },
          okMsg: "已打开 GitHub 发布页",
          errMsg: "打开失败",
          label: "应用更新",
          refresh: false,
        });
      }),
      actionButton(busy ? "检查中…" : "重新检查", "btn-ghost", () => refresh(true))
    )
  );
}

function buildCompatibility({ compatibility, busy, refresh }) {
  if (busy && !compatibility) {
    return h("div", { class: "card" }, h("div", { class: "card-title" }, "服务器版本检查"), h("p", { class: "card-desc" }, "正在检查远程服务器 API 兼容性…"));
  }
  if (!compatibility || compatibility.compatible) {
    return h("div", {});
  }
  const server = asObject(compatibility.server);
  const expected = asObject(compatibility.expected);
  const missing = Array.isArray(compatibility.missing_features) ? compatibility.missing_features : [];
  return h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "服务器端需要更新"), h("span", { class: "badge warn" }, "新版本可用")),
    h(
      "p",
      { class: "card-desc" },
      compatibility.needs_update
        ? `远程 API ${server.api_version || "旧版本"}，本地需要 ${expected.api_version || "-"}。${missing.length ? `缺少功能：${missing.join(", ")}` : "请更新远程 sync_server.py。"}`
        : compatibility.error || "无法确认远程服务器版本。"
    ),
    h(
      "div",
      { class: "card-actions" },
      actionButton("去更新服务器", "btn-primary", async () => { window.location.hash = "#/settings"; }),
      actionButton("重新检查", "btn-ghost", refresh)
    )
  );
}

function heroCard(store) {
  const cfg = asObject(store.getState().status?.config);
  return h(
    "div",
    { class: "task-home-hero" },
    h("span", { class: "task-kicker" }, "Codex Sync 控制台"),
    h("h2", {}, "先选任务，再看细节"),
    h("p", {}, "所有对话备份、导入和渠道重组都已合并入“对话与备份”；后台任务、定时同步和高级诊断已收拢至“系统设置”。"),
    h(
      "div",
      { class: "concept-meta" },
      chip(cfg.server_url ? "服务器已配置" : "服务器未配置"),
      chip(cfg.device_id || "未加载设备"),
      chip("Windows / WSL")
    )
  );
}

function taskCard(index, title, desc, action, href) {
  return h(
    "a",
    { class: "task-home-card", href },
    h("span", { class: "task-home-index" }, index),
    h("strong", {}, title),
    h("p", {}, desc),
    h("b", {}, action)
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
      { class: "inline-actions" },
      h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: busy, onClick: refresh }, busy ? "检查中…" : "刷新"),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: toggle }, expanded ? "收起详情" : "展开详情")
    )
  );

  if (!health) {
    card.replaceChildren(title, h("p", { class: "card-desc" }, "正在检查服务器与其它设备状态…"));
    return card;
  }

  const local = asObject(health.local);
  const pending = Array.isArray(health.pending_devices) ? health.pending_devices : [];
  const needs = Boolean(local.needs_upload);
  const summary = asObject(health.summary);
  const autoScan = asObject(health.auto_scan);
  const items = Array.isArray(health.status_items) ? health.status_items : legacyHealthItems(health);
  const summaryText = summary.text || (health.success ? "状态已检查" : `服务器暂不可用或未配置：${health.error || ""}`);
  const summaryTone = summary.tone || (!health.success ? "danger" : needs || pending.length ? "warn" : "ok");
  const nodes = [
    title,
    h("p", { class: "card-desc" }, "这里展示的是对话备份、WSL、项目备份、轻量接力快照、服务器连接和自动任务的当前状态。"),
    h(
      "button",
      { class: `sync-summary ${summaryTone}`, type: "button", onClick: toggle },
      h("span", { class: "sync-summary-main" }, summaryText),
      h("span", { class: `badge ${summaryTone}` }, expanded ? "详情已展开" : "点击查看详情")
    ),
    expanded ? buildHealthBoard({ items, autoScan, store, run, refresh }) : null,
    needs
      ? h(
          "div",
          { class: "card-actions" },
          actionButton("立即补做并上传", "btn-primary", async () => {
            const cfg = asObject(store.getState().status?.config);
            await run("full-backup-now", {
              payload: { upload: true, allow_plaintext_upload: !!cfg.full_backup_allow_plaintext_upload },
              okMsg: "已补做并上传",
              errMsg: "补做失败",
              label: "补做备份",
            });
            await refresh();
          })
        )
      : null
  ];
  card.replaceChildren(...nodes.filter(Boolean));
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
      detail: pending.length ? "有其它设备仍处于 dirty/diverged 状态。" : "其它设备正常。",
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
      h("strong", {}, "详细数据板"),
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
          h(
            "tr",
            {},
            h("th", {}, "模块"),
            h("th", {}, "对象"),
            h("th", {}, "状态"),
            h("th", {}, "最近检查"),
            h("th", {}, "最近上传"),
            h("th", {}, "下次处理"),
            h("th", {}, "操作")
          )
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
    h(
      "td",
      {},
      h("div", { class: "sync-object" }, h("strong", {}, row.name || "-"), h("span", { title: row.scope || "" }, row.detail || row.scope || ""))
    ),
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
  if (!label) return h("span", { class: "muted" }, "-");
  return h(
    "button",
    {
      class: "btn btn-ghost btn-sm",
      type: "button",
      onClick: async () => {
        if (row.action) {
          const cfg = asObject(store.getState().status?.config);
          const payload = { ...asObject(row.payload) };
          if (row.action === "full-backup-now") {
            payload.upload = true;
            payload.allow_plaintext_upload = !!cfg.full_backup_allow_plaintext_upload;
          }
          if (row.action === "wsl-full-backup") {
            payload.upload = true;
            payload.allow_plaintext_upload = !!cfg.full_backup_allow_plaintext_upload;
          }
          await run(row.action, {
            payload,
            okMsg: "状态项已处理",
            errMsg: "处理失败",
            label: row.name || "同步状态",
            refresh: false,
          });
          await refresh();
          return;
        }
        if (row.href) window.location.hash = row.href;
      },
    },
    label
  );
}

function shortTime(value) {
  if (!value) return "-";
  return formatDateTime(value);
}

function nextRunText(item, autoScan) {
  const row = asObject(item);
  if (row.next_run_in_seconds != null) return formatCountdown(row.next_run_in_seconds) || row.next_run_at || "-";
  if (row.next_run_at) return shortTime(row.next_run_at);
  if (["windows-conversations", "project-backup", "resume-snapshot"].includes(row.id) && autoScan.enabled) {
    return formatCountdown(autoScan.next_run_in_seconds) || shortTime(autoScan.next_run);
  }
  if (String(row.id || "").startsWith("wsl-")) return "手动";
  return "-";
}

function formatCountdown(value) {
  if (value == null || value === "") return "";
  const seconds = Math.max(0, Number(value) || 0);
  if (seconds < 60) return "1 分钟内";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${minutes} 分钟后`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `约 ${hours} 小时 ${rest} 分钟后` : `约 ${hours} 小时后`;
}

function buildStatus(status, statusError) {
  if (!status) {
    return h("div", { class: "empty" }, statusError ? `状态加载失败：${statusError}` : "加载中…");
  }
  const cfg = asObject(status.config);
  const hooks = asObject(status.hooks);
  const hookCount = Array.isArray(hooks.events) ? hooks.events.length : 0;
  const outbox = status.outbox_count ?? 0;
  return h(
    "div",
    { class: "grid grid-auto" },
    metric("同步服务器", cfg.server_url || "未配置", cfg.server_url ? "ok" : "warn"),
    metric("Hooks", hookCount ? `${hookCount} 个事件` : "未安装", hookCount ? "ok" : "warn"),
    metric("Outbox", String(outbox), outbox > 0 ? "warn" : "ok"),
    metric("守护进程", status.daemon_running ? "运行中" : "已停止", status.daemon_running ? "ok" : "")
  );
}

function metric(label, value, tone) {
  return h(
    "div",
    { class: "metric" },
    h("div", { class: "metric-label" }, label),
    h("div", { class: `metric-value ${tone || ""}` }, value)
  );
}

function chip(text) {
  return h("span", { class: "chip" }, text);
}
