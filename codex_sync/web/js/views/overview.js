// 首页：按任务进入核心流程，诊断指标只做摘要。
import { h, asObject } from "../dom.js";
import { runAction } from "../api.js";
import { makeRun, actionButton } from "../ui.js";
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
  const renderHealth = () => {
    healthWrap.replaceChildren(buildHealth({ health, busy: healthBusy, store, run, refresh: refreshHealth }));
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

function buildHealth({ health, busy, store, run, refresh }) {
  const card = h("div", { class: "card" });
  const title = h(
    "div",
    { class: "card-title" },
    h("span", {}, "同步安全摘要"),
    h("button", { class: "btn btn-ghost btn-sm", type: "button", disabled: busy, onClick: refresh }, busy ? "检查中…" : "刷新")
  );

  if (!health) {
    card.replaceChildren(title, h("p", { class: "card-desc" }, "正在检查服务器与其它设备状态…"));
    return card;
  }
  if (!health.success) {
    card.replaceChildren(title, h("p", { class: "card-desc" }, "服务器暂不可用或未配置：" + (health.error || "")));
    return card;
  }

  const local = asObject(health.local);
  const pending = Array.isArray(health.pending_devices) ? health.pending_devices : [];
  const needs = Boolean(local.needs_upload);
  const nodes = [
    title,
    h(
      "div",
      { class: "grid grid-3" },
      metric("本机状态", needs ? "有未上传变更" : "已同步", needs ? "warn" : "ok"),
      metric("其它设备", pending.length ? `${pending.length} 台待处理` : "正常", pending.length ? "warn" : "ok"),
      metric("当前设备", health.this_device_id || "-", "")
    ),
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
