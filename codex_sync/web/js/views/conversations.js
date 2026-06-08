// 对话与备份综合管理页：备份管理、对话浏览与渠道重组、备份导入。
import { h, asObject, shortId } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { refreshStatus } from "../poller.js";
import { consoleCard, makeRun, actionButton, formatDateTime } from "../ui.js";

const ROLE_LABEL = { user: "用户", assistant: "助手", developer: "系统注入", reasoning: "思考", tool_call: "工具调用", tool_output: "工具输出" };
const ROW_STYLE = { display: "flex", gap: "10px", padding: "10px 0", borderBottom: "1px solid var(--border)" };

const CONFIRM_MSG =
  "检测到 Codex 正在运行。\n\n这步会修改 Codex 的对话数据库，需要先关闭它的后台进程（codex.exe）——" +
  "它会在你下次使用 Codex 时自动重启，不影响编辑器和本页面。\n\n确认关闭 Codex 并继续吗？";

const WSL_CONFIRM_MSG =
  "检测到目标 WSL 里 Codex 正在运行。\n\n导入会写入该发行版的 ~/.codex 状态库，需要先关闭它的 Codex 后台进程。\n\n确认关闭并导入吗？";

const browseCache = {
  homes: null,
  channelsByHome: new Map(),
};

function fmtTime(sec) {
  if (!sec) return "";
  try {
    return new Date(Number(sec) * 1000).toLocaleString();
  } catch {
    return String(sec);
  }
}

function shortCwd(p) {
  if (!p) return "-";
  const parts = String(p).replace(/^\\\\\?\\/, "").split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(p);
}

function loadingSpinner(text = "加载中…") {
  return h("div", { class: "spinner-wrap" }, h("span", { class: "spinner" }), h("span", {}, text));
}

function msgStyle(role) {
  const color = role === "user" ? "var(--accent)" : role === "assistant" ? "var(--ok)" : "var(--border-strong)";
  return { borderLeft: `3px solid ${color}`, padding: "6px 10px", margin: "8px 0", background: "rgba(255,255,255,0.02)", borderRadius: "var(--radius-sm)" };
}

export function mount(root, store) {
  const run = makeRun(store);
  const { card: outCard, setOutput } = consoleCard("控制台输出详情");

  let currentTab = "backups"; // backups | browse | import
  const tabContainer = h("div", { class: "sub-tabs" });
  const viewContainer = h("div", { class: "tab-view-root" });

  const tabs = [
    { id: "backups", label: "备份与灾备" },
    { id: "browse", label: "对话与渠道" },
    { id: "import", label: "备份导入" }
  ];

  let unmountCurrent = null;

  function switchTab(tabId) {
    currentTab = tabId;
    renderTabs();
    if (unmountCurrent) {
      try { unmountCurrent(); } catch(e) {}
      unmountCurrent = null;
    }
    viewContainer.replaceChildren();

    if (tabId === "backups") {
      unmountCurrent = mountBackups(viewContainer, run, store, setOutput);
    } else if (tabId === "browse") {
      unmountCurrent = mountBrowse(viewContainer, run, store, setOutput);
    } else if (tabId === "import") {
      unmountCurrent = mountImport(viewContainer, run, store, setOutput);
    }
  }

  function renderTabs() {
    tabContainer.replaceChildren(
      ...tabs.map(t => h("button", {
        class: `sub-tab-btn ${t.id === currentTab ? "active" : ""}`,
        type: "button",
        onClick: () => switchTab(t.id)
      }, t.label))
    );
  }

  root.replaceChildren(
    tabContainer,
    viewContainer,
    h("div", { class: "section", style: { marginTop: "20px" } }, outCard)
  );

  renderTabs();
  switchTab(currentTab);

  const uCon = store.select(s => s.console, () => setOutput(store.getState().console));

  return () => {
    if (unmountCurrent) unmountCurrent();
    uCon();
  };
}

// ==================== TAB 1: 备份与灾备 ====================
function mountBackups(container, run, store, setOutput) {
  let homes = [];
  let selectedHome = "windows";
  let cloudBackups = null;
  let loadingCloudBackups = false;
  const homeTabs = h("div", { class: "home-tabs" });
  const cloudBackupsCard = h("div", { class: "card" });

  const uploadBtn = h("button", { class: "btn btn-danger", type: "button" }, "生成并上传到服务器（明文）");
  uploadBtn.addEventListener("click", async () => {
    const cfg = asObject(store.getState().status?.config);
    if (!cfg.full_backup_allow_plaintext_upload) {
      showToast("请先到「系统设置 → 备份策略」开启「允许明文上传」", "warning");
      return;
    }
    const home = selected();
    if (!window.confirm(`${home.label || home.id} 的完整对话备份包含真实对话内容，且当前未加密。确认以明文上传到服务器？`)) return;
    uploadBtn.disabled = true;
    try {
      if (home.kind === "wsl") {
        await run("wsl-full-backup", {
          payload: { distro: home.distro, include_config: true, include_memories: true, upload: true, allow_plaintext_upload: true },
          okMsg: `${home.label || "WSL"} 完整备份已生成并上传`,
        });
      } else {
        await run("full-backup-now", {
          payload: { upload: true, allow_plaintext_upload: true },
          okMsg: "Windows 完整备份已生成并上传",
        });
      }
      await refreshCloudBackups();
    } catch(e) {} finally {
      uploadBtn.disabled = false;
    }
  });

  const fullCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "Codex Home 完整备份"),
    h("p", { class: "card-desc" }, "选择一个 Codex 运行环境（Windows 或 WSL）对其所有会话进行完整打包归档。"),
    homeTabs,
    h(
      "div",
      { class: "card-actions" },
      actionButton("刷新运行环境", "btn-ghost", () => loadHomes(true)),
      actionButton("检查同步状态", "btn-ghost", checkSelected),
      actionButton("生成本地备份包", "btn-primary", backupSelected),
      actionButton("恢复最近一次备份", "btn-danger", restoreSelected),
      uploadBtn,
      actionButton("列出云端备份", "btn-ghost", refreshCloudBackups),
      actionButton("标记对话为 Dirty", "btn-ghost", () => run("notify-change", { okMsg: "已补发 dirty 标记" }))
    )
  );

  const disasterCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "灾难级预飞保护 (Disaster Backup)"),
    h("p", { class: "card-desc" }, "立即把本机 Codex 核心数据库和配置全部打包到 ~/.codex-sync/disaster-backups，不经过任何脱敏，且只保留在本地。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("创建灾难备份", "btn-primary", () =>
        run("preflight-backup", { payload: { force: true, reason: "web_manual" }, okMsg: "灾难备份已创建" })
      )
    )
  );

  container.replaceChildren(
    h("div", { class: "section" }, fullCard),
    h("div", { class: "section" }, cloudBackupsCard),
    h("div", { class: "section" }, disasterCard)
  );

  const syncUpload = () => {
    const allow = Boolean(asObject(store.getState().status?.config).full_backup_allow_plaintext_upload);
    uploadBtn.disabled = !allow;
    uploadBtn.title = allow ? "以明文上传当前选中环境的完整对话包" : "需先在设置开启「允许明文上传」";
  };

  function selected() {
    return homes.find(home => home.id === selectedHome) || { id: "windows", kind: "windows", label: "Windows" };
  }

  function renderHomes() {
    if (!homes.length) {
      homeTabs.replaceChildren(loadingSpinner("正在拉取 Codex 运行环境（包含 WSL 检查）…"));
      return;
    }
    homeTabs.replaceChildren(
      ...homes.map(home =>
        h("button", {
          class: `home-tab ${home.id === selectedHome ? "active" : ""}`,
          type: "button",
          disabled: !home.ok || !home.has_codex,
          onClick: () => {
            selectedHome = home.id;
            renderHomes();
            syncUpload();
          }
        },
        h("span", { class: "home-tab-title" }, home.label || home.id),
        h("span", { class: "home-tab-sub" }, home.ok ? (home.has_codex ? (home.current_provider ? `当前渠道 ${home.current_provider}` : "可备份") : "未检测到 ~/.codex 目录") : home.error || "不可访问")
        )
      )
    );
  }

  async function loadHomes(notify = false) {
    try {
      const result = await runAction("list-codex-homes");
      homes = Array.isArray(result.homes) ? result.homes : [];
      if (!homes.some(h => h.id === selectedHome && h.ok && h.has_codex)) {
        selectedHome = (homes.find(h => h.ok && h.has_codex) || homes[0] || { id: "windows" }).id;
      }
      renderHomes();
      syncUpload();
      if (notify) showToast("环境列表已刷新", "success");
    } catch(e) {
      homes = [{ id: "windows", kind: "windows", label: "Windows", ok: true, has_codex: true }];
      renderHomes();
    }
  }

  async function checkSelected() {
    const home = selected();
    if (home.kind === "wsl") return run("wsl-status", { refresh: false });
    return run("full-backup-status", { refresh: false });
  }

  async function backupSelected() {
    const home = selected();
    if (home.kind === "wsl") {
      return run("wsl-full-backup", {
        payload: { distro: home.distro, include_config: true, include_memories: true },
        okMsg: `${home.label || "WSL"} 本地完整备份包已生成`
      });
    }
    return run("full-backup-now", { payload: { upload: false }, okMsg: "Windows 本地完整备份包已生成" });
  }

  async function restoreSelected() {
    const home = selected();
    if (home.kind !== "wsl") {
      showToast("Windows 完整恢复请使用 CLI restore-full-backup 并确认包 ID，以防误操作覆盖。", "warning");
      return;
    }
    if (!window.confirm(`将把最近一份 WSL 完整备份还原到 ${home.label}。默认不覆盖 config.toml 配置文件。继续吗？`)) return;
    return run("wsl-restore-latest", {
      payload: { distro: home.distro, restore_config: false },
      okMsg: "WSL 备份已还原"
    });
  }

  async function refreshCloudBackups() {
    loadingCloudBackups = true;
    renderCloudBackups();
    try {
      cloudBackups = await run("list-full-backups", { refresh: false, okMsg: "云端备份列表已更新" });
    } finally {
      loadingCloudBackups = false;
      renderCloudBackups();
    }
  }

  function renderCloudBackups() {
    const backups = Array.isArray(cloudBackups?.full_backups) ? cloudBackups.full_backups : [];
    cloudBackupsCard.replaceChildren(
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "云端完整备份"),
        h("span", { class: `badge ${cloudBackups?.error ? "danger" : backups.length ? "ok" : ""}` }, loadingCloudBackups ? "加载中" : cloudBackups?.error ? "失败" : `${backups.length} 个`)
      ),
      cloudBackups?.error
        ? h("div", { class: "status-line error" }, cloudBackups.error)
        : backups.length
          ? h(
              "div",
              { class: "table-wrap" },
              h(
                "table",
                { class: "table" },
                h("thead", {}, h("tr", {},
                  h("th", {}, "环境"),
                  h("th", {}, "本机时间"),
                  h("th", {}, "大小"),
                  h("th", {}, "状态"),
                  h("th", {}, "ID")
                )),
                h("tbody", {}, ...backups.slice(0, 12).map(cloudBackupRow))
              )
            )
          : h("div", { class: "empty compact" }, loadingCloudBackups ? "正在读取云端备份…" : "还没有加载云端备份，点击“列出云端备份”。")
    );
  }

  syncUpload();
  loadHomes();
  renderCloudBackups();
  const uConfig = store.select(s => s.status?.config, syncUpload);

  return () => {
    uConfig();
  };
}

function cloudBackupRow(item) {
  const backup = asObject(item);
  const state = backup.sync_state || (backup.parent_backup_id ? "incremental" : "root");
  const time = backup.received_at || backup.created_at || "";
  return h(
    "tr",
    {},
    h(
      "td",
      { class: "cell-ellipsis", title: backup.device_id || "" },
      h("div", {}, fullBackupEnvLabel(backup)),
      h("div", { class: "muted", style: { fontSize: "12px" } }, backup.branch_id || backup.device_id || "-")
    ),
    h("td", { title: time ? `UTC: ${time}` : "" }, formatDateTime(time)),
    h("td", {}, formatBytes(backup.size_bytes)),
    h(
      "td",
      {},
      h("span", { class: `badge ${backup.encrypted ? "ok" : "warn"}` }, backup.encrypted ? "已加密" : "明文"),
      h("span", { class: "badge", style: { marginLeft: "6px" } }, state)
    ),
    h("td", { class: "mono", title: backup.id || "" }, shortId(backup.id))
  );
}

function fullBackupEnvLabel(backup) {
  const device = String(backup.device_id || "");
  if (device.startsWith("wsl:")) return `WSL · ${device.slice(4) || "unknown"}`;
  return device ? `Windows · ${device}` : "Windows";
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ==================== TAB 2: 对话与渠道 ====================
function mountBrowse(container, run, store, setOutput) {
  let homes = [];
  let sourceHome = "all";
  let channelsData = null;
  let conversationsData = null;
  let detail = null;
  let busy = false;
  const selected = new Map();
  let search = "";
  let fcwd = "";
  let fprovider = "";
  let targetProvider = "";

  const homeSel = h("select", { class: "input", style: { flex: "0 1 180px" } });
  const searchInput = h("input", { class: "input", type: "search", style: { flex: "1 1 200px" }, placeholder: "搜索标题、预览、对话历史…" });
  const cwdSel = h("select", { class: "input", style: { flex: "1 1 150px" } });
  const provSel = h("select", { class: "input", style: { flex: "1 1 150px" } });
  const targetProviderSel = h("select", { class: "input", style: { flex: "0 1 180px" } });

  const listWrap = h("div", { class: "card", style: { maxHeight: "60vh", overflow: "auto" } });
  const detailWrap = h("div", { class: "card", style: { maxHeight: "60vh", overflow: "auto" } });
  const batchWrap = h("div", { class: "card-actions" });

  const channelsCollapse = h(
    "details",
    { class: "collapse" },
    h("summary", {}, "本地渠道整合与分布"),
    h("div", { class: "collapse-body" })
  );

  searchInput.addEventListener("change", () => { search = searchInput.value.trim(); refreshConversations(); });
  cwdSel.addEventListener("change", () => { fcwd = cwdSel.value; refreshConversations(); });
  provSel.addEventListener("change", () => { fprovider = provSel.value; refreshConversations(); });
  targetProviderSel.addEventListener("change", () => { targetProvider = targetProviderSel.value; });

  const browserCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "对话浏览"),
    h("p", { class: "card-desc" }, "检索和批量管理本机所有 Codex 对话历史，点标题看正文。"),
    h(
      "div",
      { class: "card-actions" },
      homeSel,
      searchInput,
      cwdSel,
      provSel,
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: refreshConversations }, "刷新对话"),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: refreshEnvironment }, "刷新环境/渠道")
    )
  );

  container.replaceChildren(
    h("div", { class: "section" }, channelsCollapse),
    h("div", { class: "section" }, browserCard),
    h("div", { class: "section" }, batchWrap),
    h("div", { class: "grid grid-2" }, listWrap, detailWrap)
  );

  function fillSelect(sel, items, cur, allLabel, labeler) {
    sel.replaceChildren(
      h("option", { value: "" }, allLabel),
      ...(items || []).map(x => h("option", { value: x }, labeler ? labeler(x) : x))
    );
    sel.value = cur || "";
  }

  function homeLabel(homeId) {
    if (homeId === "all") return "全部本机环境";
    const home = homes.find(item => item.id === homeId);
    if (home) return home.label || home.id;
    if (String(homeId || "").startsWith("wsl:")) return `WSL ${String(homeId).split(":", 2)[1]}`;
    return "Windows";
  }

  function renderHomeOptions(loading = false) {
    const usable = homes.filter(home => home && home.ok && home.has_codex);
    if (sourceHome !== "all" && !usable.some(home => home.id === sourceHome)) {
      sourceHome = "all";
    }
    homeSel.replaceChildren(
      h("option", { value: "all" }, loading ? "正在检测本机环境…" : "全部本机环境"),
      ...(homes || []).map(home =>
        h(
          "option",
          { value: home.id, disabled: !home.ok || !home.has_codex },
          `${home.label || home.id}${home.ok && home.has_codex ? "" : "（不可用）"}`
        )
      )
    );
    homeSel.value = sourceHome;
  }

  async function loadHomes(force = false) {
    if (!force && Array.isArray(browseCache.homes)) {
      homes = browseCache.homes;
      renderHomeOptions(false);
      return;
    }
    if (force) browseCache.channelsByHome.clear();
    renderHomeOptions(true);
    try {
      const result = await runAction("list-codex-homes");
      homes = Array.isArray(result.homes) ? result.homes : [];
      browseCache.homes = homes;
    } catch {
      homes = [{ id: "windows", kind: "windows", label: "Windows", ok: true, has_codex: true }];
      browseCache.homes = homes;
    }
    renderHomeOptions(false);
  }

  function convKey(c) {
    return `${c.home_id || "windows"}::${c.id}`;
  }

  function selectedRows() {
    return [...selected.values()];
  }

  function isWindowsRow(row) {
    return (row.home_id || "windows") === "windows";
  }

  function disabledButton(label, cls, title) {
    return h("button", { class: `btn ${cls}`, type: "button", disabled: true, title }, label);
  }

  async function refreshAll(force = false) {
    await Promise.all([refreshChannels(force), refreshConversations()]);
  }

  async function refreshEnvironment() {
    await loadHomes(true);
    await refreshChannels(true);
    await refreshConversations();
  }

  homeSel.addEventListener("change", async () => {
    sourceHome = homeSel.value || "all";
    selected.clear();
    detail = null;
    fcwd = "";
    fprovider = "";
    renderDetail();
    await refreshAll(false);
  });

  // --- 渠道分布渲染 ---
  async function refreshChannels(force = false) {
    const cacheKey = sourceHome || "windows";
    if (!force && browseCache.channelsByHome.has(cacheKey)) {
      channelsData = browseCache.channelsByHome.get(cacheKey);
      renderChannels();
      fillTargetProviderOptions();
      return;
    }
    try {
      channelsData = await runAction("list-channels", { source_home: sourceHome });
      browseCache.channelsByHome.set(cacheKey, channelsData);
    } catch(e) {
      channelsData = { success: false, error: String(e.message || e) };
    }
    renderChannels();
    fillTargetProviderOptions();
  }

  async function submitChannelOp(action, opts, label) {
    if (busy) return;
    if (sourceHome !== "windows") {
      showToast("渠道并入/还原只支持 Windows 单环境，请先选择 Windows。", "warning");
      return;
    }
    let closeCodex = false;
    if (channelsData && channelsData.codex_running) {
      if (!window.confirm(CONFIRM_MSG)) return;
      closeCodex = true;
    }
    busy = true;
    renderChannels();
    try {
      let res = await runAction(action, { ...opts, source_home: sourceHome, close_codex: closeCodex });
      if (res && res.needs_close && !closeCodex) {
        if (window.confirm(CONFIRM_MSG)) {
          res = await runAction(action, { ...opts, source_home: sourceHome, close_codex: true });
        } else {
          setOutput(res);
          return;
        }
      }
      setOutput(res);
      const ok = res && res.success;
      showToast(ok ? `${label}完成 · 刷新 Codex 即可生效` : (res && res.error) || `${label}失败`, ok ? "success" : "error");
    } catch(e) {
      setOutput({ error: String(e.message || e) });
      showToast(String(e.message || e), "error");
    } finally {
      busy = false;
      await refreshChannels(true);
      await refreshConversations();
      refreshStatus(store).catch(() => {});
    }
  }

  function renderChannels() {
    const body = channelsCollapse.querySelector(".collapse-body");
    if (!channelsData) {
      body.replaceChildren(h("p", { class: "muted" }, "正在加载渠道分布数据…"));
      return;
    }
    if (!channelsData.success) {
      body.replaceChildren(
        h("p", { class: "muted" }, "读取渠道失败：" + (channelsData.error || "")),
        actionButton("刷新重试", "btn-ghost btn-sm", refreshChannels)
      );
      return;
    }

    if (channelsData.home_groups) {
      const groups = channelsData.home_groups || [];
      body.replaceChildren(
        h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "10px" } },
          h("span", {}, "查看范围：", h("strong", {}, "全部本机环境")),
          h("span", { class: "badge warn" }, "只读")
        ),
        h("p", { class: "card-desc" }, "跨环境查看按 Windows/WSL 分组展示；并入或还原请先切换到 Windows 单环境。"),
        ...groups.map(group => renderChannelGroup(group))
      );
      return;
    }

    const writable = channelsData.write_supported !== false && sourceHome === "windows";
    const cur = channelsData.current_provider || "(未知)";
    const running = Boolean(channelsData.codex_running);
    const merged = channelsData.merged || {};

    const rows = (channelsData.channels || []).map(c => {
      const isCur = c.provider === cur;
      const into = (merged.by_target && merged.by_target[c.provider]) || 0;
      return h(
        "div",
        { style: ROW_STYLE },
        h("div", {},
          h("strong", {}, c.provider),
          isCur ? h("span", { class: "badge ok", style: { marginLeft: "8px" } }, "当前") : null,
          into ? h("span", { class: "badge", style: { marginLeft: "8px" } }, `含并入 ${into}`) : null
        ),
        h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
          h("span", { class: "muted" }, `${c.threads} 个对话`),
          isCur || !writable ? h("span", { class: "muted" }, "—") : actionButton("并入当前", "btn-primary btn-sm", () => submitChannelOp("merge-channels", { sources: [c.provider] }, `并入 ${c.provider}`))
        )
      );
    });

    const origins = {};
    const ob = merged.origin_breakdown || {};
    for (const t in ob) for (const o in ob[t]) origins[o] = (origins[o] || 0) + ob[t][o];
    const originKeys = Object.keys(origins).sort();

    const runBadge = writable
      ? h("span", { class: `badge ${running ? "warn" : "ok"}` }, running ? "Codex 运行中" : "Codex 未运行")
      : h("span", { class: "badge warn" }, "只读");

    body.replaceChildren(
      h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "10px" } },
        h("span", {}, `${homeLabel(sourceHome)} 默认渠道：`, h("strong", {}, cur)),
        runBadge
      ),
      writable
        ? h("p", { class: "card-desc" }, "「并入当前」会修改被合并渠道的 provider，将它们显示在你的当前对话列表里。这只是软链接，不会破坏会话内容，随时能一键还原。")
        : h("p", { class: "card-desc" }, "WSL 渠道当前以查看为主；需要写入并入/还原时请切到 Windows。"),
      rows.length ? h("div", { style: { margin: "10px 0" } }, ...rows) : h("div", { class: "empty" }, "暂无渠道数据"),
      writable && merged.total
        ? h("div", { style: { borderTop: "1px dashed var(--border)", paddingTop: "10px", marginTop: "10px" } },
            h("strong", {}, "已并入列表（可按源还原）："),
            ...originKeys.map(o => h("div", { style: ROW_STYLE },
              h("div", {}, "源渠道 ", h("strong", {}, o), ` · ${origins[o]} 个对话`),
              actionButton(`还原 ${o}`, "btn-ghost btn-sm", () => submitChannelOp("restore-channels", { sources: [o] }, `还原 ${o}`))
            )),
            h("div", { class: "card-actions", style: { marginTop: "10px" } }, actionButton("全部还原", "btn-ghost btn-sm", () => submitChannelOp("restore-channels", { all: true }, "全部还原")))
          )
        : null,
      writable ? h("div", { class: "card-actions", style: { marginTop: "10px" } },
        actionButton("全部并入当前", "btn-primary btn-sm", () => submitChannelOp("merge-channels", { all: true }, "全部并入"))
      ) : null
    );
  }

  function renderChannelGroup(group) {
    if (!group || !group.success) {
      return h("div", { class: "empty", style: { margin: "10px 0" } }, `${group?.home_label || "本机环境"} 读取失败：${group?.error || ""}`);
    }
    const cur = group.current_provider || "(未知)";
    const rows = (group.channels || []).map(c =>
      h("div", { style: ROW_STYLE },
        h("div", {},
          h("strong", {}, c.provider),
          c.provider === cur ? h("span", { class: "badge ok", style: { marginLeft: "8px" } }, "当前") : null
        ),
        h("span", { class: "muted" }, `${c.threads} 个对话`)
      )
    );
    return h(
      "div",
      { style: { borderTop: "1px dashed var(--border)", paddingTop: "10px", marginTop: "10px" } },
      h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "6px" } },
        h("strong", {}, group.home_label || group.home_id || "本机环境"),
        h("span", { class: "badge warn" }, "只读")
      ),
      h("div", { class: "muted", style: { fontSize: "12px" } }, "默认渠道：", cur),
      rows.length ? h("div", {}, ...rows) : h("div", { class: "empty" }, "暂无渠道数据")
    );
  }

  // --- 对话历史浏览 ---
  async function refreshConversations() {
    busy = true;
    renderBatch();
    try {
      conversationsData = await runAction("list-conversations", { source_home: sourceHome, search, cwd: fcwd, provider: fprovider });
    } catch (e) {
      conversationsData = { success: false, error: String(e.message || e) };
    }
    busy = false;
    if (conversationsData && conversationsData.success) {
      fillSelect(cwdSel, conversationsData.cwds, fcwd, "全部项目", shortCwd);
      fillSelect(provSel, conversationsData.providers, fprovider, "全部渠道");
    }
    fillTargetProviderOptions();
    renderList();
    renderBatch();
  }

  function fillTargetProviderOptions() {
    const current = channelsData?.current_provider || "";
    const fromChannels = Array.isArray(channelsData?.channels) ? channelsData.channels.map(c => c.provider) : [];
    const fromConversations = Array.isArray(conversationsData?.providers) ? conversationsData.providers : [];
    const providers = [...new Set([current, ...fromChannels, ...fromConversations].filter(p => p && p !== "(unknown)"))].sort();
    if (!providers.length) {
      targetProvider = "";
      targetProviderSel.replaceChildren(h("option", { value: "" }, "无可用目标渠道"));
      targetProviderSel.disabled = true;
      return;
    }
    targetProviderSel.disabled = false;
    if (!targetProvider || !providers.includes(targetProvider)) targetProvider = current || providers[0];
    targetProviderSel.replaceChildren(
      ...providers.map(provider =>
        h("option", { value: provider }, provider === current ? `${provider}（当前）` : provider)
      )
    );
    targetProviderSel.value = targetProvider;
  }

  function renderList() {
    if (!conversationsData) {
      listWrap.replaceChildren(loadingSpinner("正在检索对话历史…"));
      return;
    }
    if (!conversationsData.success) {
      listWrap.replaceChildren(h("div", { class: "empty" }, "加载失败：" + (conversationsData.error || "")));
      return;
    }
    const convos = conversationsData.conversations || [];
    const errors = conversationsData.errors || [];
    if (!convos.length) {
      listWrap.replaceChildren(h("div", { class: "empty" }, "没有发现匹配的对话"));
      return;
    }
    listWrap.replaceChildren(
      h("div", {},
        errors.length ? h("div", { class: "muted", style: { fontSize: "12px", marginBottom: "8px" } }, `部分环境读取失败：${errors.map(e => e.label || e.home_id).join("、")}`) : null,
        ...convos.map(convRow)
      )
    );
  }

  function convRow(c) {
    const key = convKey(c);
    const cb = h("input", { type: "checkbox", checked: selected.has(key) || null });
    cb.addEventListener("change", () => {
      if (cb.checked) selected.set(key, c);
      else selected.delete(key);
      renderBatch();
    });
    const title = h(
      "div",
      { style: { fontWeight: "600", cursor: "pointer" }, onClick: () => openDetail(c) },
      c.title || "(无标题)"
    );
    const meta = h(
      "div",
      { class: "muted", style: { fontSize: "12px" } },
      sourceHome === "all" ? h("span", { class: "badge", style: { marginRight: "6px" } }, c.home_label || homeLabel(c.home_id)) : null,
      fmtTime(c.updated_at) + " · " + shortCwd(c.cwd) + " · ",
      h("span", { class: "badge" }, c.model_provider || "-"),
      c.merged ? h("span", { class: "badge ok", style: { marginLeft: "6px" } }, "已并入") : null
    );
    const preview = c.preview ? h("div", { class: "muted", style: { fontSize: "12px", opacity: "0.75" } }, String(c.preview).slice(0, 90)) : null;
    return h(
      "div",
      { style: ROW_STYLE },
      h("div", { style: { paddingTop: "2px" } }, cb),
      h("div", { style: { flex: "1", minWidth: "0" } }, title, meta, preview)
    );
  }

  async function openDetail(c) {
    const homeId = c.home_id || "windows";
    detailWrap.replaceChildren(loadingSpinner("正在拉取对话上下文正文…"));
    try {
      const result = await runAction("read-conversation", { thread_id: c.id, source_home: homeId, include_tools: false });
      detail = { ...result, home_id: homeId, home_label: c.home_label || homeLabel(homeId) };
    } catch (e) {
      detail = { success: false, error: String(e.message || e) };
    }
    renderDetail();
  }

  function renderDetail() {
    if (!detail) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "点左侧对话标题查看具体正文"));
      return;
    }
    if (!detail.success) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "正文加载失败：" + (detail.error || "")));
      return;
    }
    const t = detail.thread || {};
    const msgs = (detail.messages || []).map(m =>
      h(
        "div",
        { style: msgStyle(m.role) },
        h("div", { class: "muted", style: { fontSize: "12px", marginBottom: "2px" } }, ROLE_LABEL[m.role] || m.role),
        h("div", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, m.text || "")
      )
    );
    detailWrap.replaceChildren(
      h("div", { class: "card-title" }, h("span", {}, t.title || "(无标题)")),
      h("p", { class: "card-desc" }, `${detail.home_label || t.home_label || homeLabel(t.home_id)} · ${t.model_provider || "-"} · ${shortCwd(t.cwd)} · ${detail.message_count} 条消息`),
      h("div", {}, ...msgs),
      detail.truncated ? h("div", { class: "muted" }, "（正文较长，已截断）") : null
    );
  }

  function renderBatch() {
    const rows = selectedRows();
    const canWrite = sourceHome === "windows" && rows.every(isWindowsRow);
    const writeTip = "并入/还原只支持 Windows 单环境，请先在本机环境里选择 Windows。";
    batchWrap.replaceChildren(
      h("span", { class: "muted" }, busy ? "加载中…" : `已选 ${selected.size} 项`),
      actionButton("全选本页", "btn-ghost btn-sm", async () => {
        (conversationsData?.conversations || []).forEach(c => selected.set(convKey(c), c));
        renderList();
        renderBatch();
      }),
      actionButton("清空选择", "btn-ghost btn-sm", async () => {
        selected.clear();
        renderList();
        renderBatch();
      }),
      actionButton("导出 Markdown", "btn-ghost btn-sm", doExport),
      h("span", { class: "muted", style: { marginLeft: "8px" } }, "目标渠道"),
      targetProviderSel,
      canWrite ? actionButton("改到目标渠道", "btn-primary btn-sm", () => writeOp("merge-threads", "已改到目标渠道", targetProvider || targetProviderSel.value)) : disabledButton("改到目标渠道", "btn-primary btn-sm", writeTip),
      canWrite ? actionButton("还原原始渠道", "btn-ghost btn-sm", () => writeOp("restore-threads", "已还原")) : disabledButton("还原原始渠道", "btn-ghost btn-sm", writeTip)
    );
  }

  async function doExport() {
    if (!selected.size) { showToast("请先勾选对话", "warning"); return; }
    const paths = [];
    for (const row of selectedRows()) {
      try {
        const r = await runAction("export-conversation", { thread_id: row.id, source_home: row.home_id || "windows" });
        if (r && r.path) paths.push(r.path);
      } catch(e) {}
    }
    setOutput({ exported_count: paths.length, paths });
    showToast(`已成功导出 ${paths.length} 个对话到 ~/.codex-sync/exports`, paths.length ? "success" : "error");
  }

  async function writeOp(action, label, target = "") {
    if (!selected.size) { showToast("请先勾选对话", "warning"); return; }
    const rows = selectedRows();
    if (sourceHome !== "windows" || rows.some(row => !isWindowsRow(row))) {
      showToast("并入/还原只支持 Windows 单环境，请先选择 Windows。", "warning");
      return;
    }
    if (action === "merge-threads" && !target) {
      showToast("请先选择目标渠道", "warning");
      return;
    }
    const ids = rows.map(row => row.id);
    try {
      const payload = { source_home: sourceHome, thread_ids: ids };
      if (action === "merge-threads") payload.target = target;
      let res = await runAction(action, payload);
      if (res && res.needs_close) {
        if (!window.confirm(CONFIRM_MSG)) {
          setOutput(res);
          return;
        }
        res = await runAction(action, { ...payload, close_codex: true });
      }
      setOutput(res);
      const ok = res && res.success;
      const n = res && (res.moved ?? res.restored);
      showToast(ok ? `${label} ${n} 个对话 · 刷新 Codex 可见` : (res && res.error) || `${label}失败`, ok ? "success" : "error");
      if (ok) selected.clear();
    } catch (e) {
      showToast(String(e.message || e), "error");
    } finally {
      await refreshChannels(true);
      await refreshConversations();
      refreshStatus(store).catch(() => {});
    }
  }

  renderHomeOptions(true);
  listWrap.replaceChildren(loadingSpinner("正在检测本机环境…"));
  detailWrap.replaceChildren(h("div", { class: "empty" }, "点左侧对话标题查看具体正文"));
  loadHomes(false).finally(() => refreshAll(false));

  return () => {};
}

// ==================== TAB 3: 备份导入 ====================
function mountImport(container, run, store, setOutput) {
  let backups = null;
  let homes = [];
  let convos = null;
  let detail = null;
  let curBackupId = "";
  let targetHome = "windows";
  let target = "";
  let busy = false;
  const selected = new Set();

  const backupSel = h("select", { class: "input" });
  const homeSel = h("select", { class: "input" });
  const targetSel = h("select", { class: "input" });
  const listWrap = h("div", { class: "card", style: { maxHeight: "56vh", overflow: "auto" } });
  const detailWrap = h("div", { class: "card", style: { maxHeight: "56vh", overflow: "auto" } });
  const batchWrap = h("div", { class: "card-actions" });

  backupSel.addEventListener("change", () => {
    curBackupId = backupSel.value;
    selected.clear();
    loadConvos();
  });
  targetSel.addEventListener("change", () => {
    target = targetSel.value;
  });
  homeSel.addEventListener("change", async () => {
    targetHome = homeSel.value || "windows";
    target = "";
    if (curBackupId) await loadConvos();
    else if (convos && convos.success) await fillTarget(convos);
    renderBatch();
  });

  const toolbar = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "备份导入向导"),
    h("p", { class: "card-desc" }, "第一步：在下拉框中选择要加载的备份包。第二步：勾选需要恢复导入的对话。第三步：指定目标环境和渠道，执行导入。"),
    h("div", { class: "card-actions" }, h("span", { class: "muted" }, "选择备份源包"), backupSel, actionButton("刷新备份包列表", "btn-ghost btn-sm", loadBackups))
  );

  container.replaceChildren(
    h("div", { class: "section" }, toolbar),
    h("div", { class: "section" }, batchWrap),
    h("div", { class: "grid grid-2" }, listWrap, detailWrap)
  );

  async function loadBackups() {
    busy = true;
    renderBatch();
    try {
      backups = await runAction("list-importable-backups");
    } catch(e) {
      backups = { success: false, error: String(e.message || e) };
    }
    busy = false;
    const items = (backups && backups.success && backups.backups) || [];
    const ids = new Set(items.map(item => item.backup_id));
    backupSel.replaceChildren(
      h("option", { value: "" }, items.length ? `-- 选择备份包（共 ${items.length} 个）--` : "-- 无可用备份包 --"),
      ...items.map(b =>
        h("option", { value: b.backup_id }, `${sourceLabel(b.source)} · ${b.device_id || "?"} · ${formatDateTime(b.created_at)}${b.downloaded ? " ✓本地就绪" : ""}`)
      )
    );
    if (curBackupId && ids.has(curBackupId)) {
      backupSel.value = curBackupId;
    } else {
      curBackupId = "";
      backupSel.value = "";
      selected.clear();
      convos = null;
      detail = null;
      renderList();
      renderDetail();
    }
    if (!items.length) {
      listWrap.replaceChildren(
        h("div", { class: "empty" }, backups && backups.remote_error ? "服务器连接失败，且本地无缓存备份：" + backups.remote_error : "暂无可导入的备份文件")
      );
    }
    renderBatch();
  }

  async function loadHomes() {
    try {
      const result = await runAction("list-codex-homes");
      homes = Array.isArray(result.homes) ? result.homes : [];
    } catch(e) {
      homes = [{ id: "windows", label: "Windows", kind: "windows", ok: true, has_codex: true }];
    }
    const usable = homes.filter(home => home.ok && home.has_codex);
    if (!usable.some(home => home.id === targetHome)) {
      targetHome = (usable[0] || homes[0] || { id: "windows" }).id;
    }
    homeSel.replaceChildren(
      ...homes.map(home =>
        h("option", { value: home.id, disabled: !home.ok || !home.has_codex }, `${home.label || home.id}${home.has_codex ? "" : "（无 .codex 目录）"}`)
      )
    );
    homeSel.value = targetHome;
  }

  async function loadConvos() {
    if (!curBackupId) {
      convos = null;
      renderList();
      renderBatch();
      return;
    }
    listWrap.replaceChildren(loadingSpinner("正在解析备份包（云端备份将自动拉取缓存）…"));
    try {
      convos = await runAction("list-backup-conversations", { backup_id: curBackupId, target_home: targetHome });
    } catch(e) {
      convos = { success: false, error: String(e.message || e) };
    }
    if (convos && convos.success) await fillTarget(convos);
    renderList();
    renderBatch();
  }

  async function fillTarget(data) {
    let local = null;
    try {
      local = await runAction("list-target-channels", { target_home: targetHome });
    } catch(e) {
      local = { success: false, error: String(e.message || e) };
    }

    const current = local?.current_provider || data.current_provider || "";
    const localProviders = uniqueProviders([
      current,
      ...((local && local.success && local.channels) || []).map(c => c.provider),
    ]);
    const backupProviders = uniqueProviders((data.conversations || []).map(c => c.model_provider));
    if (!target) target = current || localProviders[0] || backupProviders[0] || "";

    const groups = [];
    if (localProviders.length) {
      groups.push(
        h("optgroup", { label: targetHome === "windows" ? "Windows 渠道" : "目标 WSL 渠道" },
          ...localProviders.map(p => h("option", { value: p }, providerLabel(p, current, "目标")))
        )
      );
    }
    if (backupProviders.length) {
      groups.push(
        h("optgroup", { label: "备份包自带渠道" },
          ...backupProviders.map(p => h("option", { value: p }, providerLabel(p, current, "备份")))
        )
      );
    }

    targetSel.replaceChildren(...(groups.length ? groups : [h("option", { value: "" }, "未检测到可选渠道")]));
    targetSel.value = target || current || "";
    if (targetSel.value !== (target || current || "")) {
      targetSel.value = current || localProviders[0] || backupProviders[0] || "";
    }
    target = targetSel.value;
  }

  function uniqueProviders(items) {
    return [...new Set((items || []).filter(p => p && p !== "(unknown)"))].sort();
  }

  function providerLabel(provider, current, source) {
    const tags = [source];
    if (provider === current) tags.push("当前");
    return `${provider}（${tags.join("，")}）`;
  }

  function renderList() {
    if (!convos) {
      listWrap.replaceChildren(h("div", { class: "empty" }, "请先选择一个备份包以加载列表"));
      return;
    }
    if (!convos.success) {
      listWrap.replaceChildren(h("div", { class: "empty" }, "加载失败：" + (convos.error || "")));
      return;
    }
    const list = convos.conversations || [];
    if (!list.length) {
      listWrap.replaceChildren(h("div", { class: "empty" }, "包内暂无有效对话数据"));
      return;
    }
    listWrap.replaceChildren(h("div", {}, ...list.map(convRow)));
  }

  function convRow(c) {
    const cb = h("input", { type: "checkbox", checked: selected.has(c.id) || null, disabled: !c.has_body || null });
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(c.id);
      else selected.delete(c.id);
      renderBatch();
    });
    const title = h("div", { style: { fontWeight: "600", cursor: "pointer" }, onClick: () => openDetail(c.id) }, c.title || "(无标题)");
    const tags = h(
      "div",
      { class: "muted", style: { fontSize: "12px" } },
      h("span", { class: "badge" }, c.model_provider || "-"),
      c.present
        ? h("span", { class: "badge", style: { marginLeft: "6px" } }, c.newer ? "本地已存在更新版本" : "本地已有")
        : h("span", { class: "badge ok", style: { marginLeft: "6px" } }, "全新导入"),
      c.has_body ? null : h("span", { class: "badge warn", style: { marginLeft: "6px" } }, "空内容")
    );
    return h(
      "div",
      { style: ROW_STYLE },
      h("div", { style: { paddingTop: "2px" } }, cb),
      h("div", { style: { flex: "1", minWidth: "0" } }, title, tags)
    );
  }

  async function openDetail(id) {
    detailWrap.replaceChildren(loadingSpinner("正在加载预览文本…"));
    try {
      detail = await runAction("read-backup-conversation", { backup_id: curBackupId, thread_id: id });
    } catch (e) {
      detail = { success: false, error: String(e.message || e) };
    }
    renderDetail();
  }

  function renderDetail() {
    if (!detail) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "点左侧对话标题以预览对话正文"));
      return;
    }
    if (!detail.success) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "正文加载失败：" + (detail.error || "")));
      return;
    }
    const msgs = (detail.messages || []).map(m =>
      h(
        "div",
        { style: msgStyle(m.role) },
        h("div", { class: "muted", style: { fontSize: "12px" } }, ROLE_LABEL[m.role] || m.role),
        h("div", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, m.text || "")
      )
    );
    detailWrap.replaceChildren(
      h("div", { class: "card-title" }, h("span", {}, (detail.thread || {}).title || "(无标题)")),
      h("div", {}, ...msgs),
      detail.truncated ? h("div", { class: "muted" }, "（已截断）") : null
    );
  }

  function renderBatch() {
    batchWrap.replaceChildren(
      h("span", { class: "muted" }, busy ? "加载中…" : `已选择 ${selected.size} 项`),
      h("span", { class: "muted", style: { marginLeft: "8px" } }, "目标位置"),
      homeSel,
      h("span", { class: "muted", style: { marginLeft: "8px" } }, "接入渠道"),
      targetSel,
      actionButton("导入到选定环境", "btn-primary btn-sm", doImport)
    );
  }

  async function doImport() {
    if (!curBackupId) { showToast("请先选择备份包", "warning"); return; }
    if (!selected.size) { showToast("请先勾选需要导入的对话", "warning"); return; }
    const ids = [...selected];
    const t = target || targetSel.value || "";
    try {
      let res = await runAction("import-conversations", { backup_id: curBackupId, thread_ids: ids, target: t, target_home: targetHome });
      if (res && res.needs_close) {
        if (!window.confirm(targetHome.startsWith("wsl:") ? WSL_CONFIRM_MSG : CONFIRM_MSG)) {
          setOutput(res);
          return;
        }
        res = await runAction("import-conversations", { backup_id: curBackupId, thread_ids: ids, target: t, target_home: targetHome, close_codex: true });
      }
      setOutput(res);
      const ok = res && res.success;
      const homeLabel = homes.find(h => h.id === targetHome)?.label || targetHome;
      showToast(ok ? `已成功导入 ${res.imported} 个会话到 ${homeLabel} · ${res.target}` : (res && res.error) || "导入失败", ok ? "success" : "error");
      if (ok) {
        selected.clear();
        await loadConvos();
      }
    } catch (e) {
      showToast(String(e.message || e), "error");
    } finally {
      refreshStatus(store).catch(() => {});
    }
  }

  loadHomes().then(() => {
    renderBatch();
    loadBackups();
  });
  renderDetail();

  return () => {};
}

function sourceLabel(source) {
  if (source === "wsl") return "WSL";
  if (source === "local") return "本地缓存";
  return "云服务器";
}
