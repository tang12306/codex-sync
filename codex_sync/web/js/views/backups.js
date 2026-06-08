// 数据备份与恢复控制中心：完整备份与灾备、云端备份与快照、本地备份导入、服务器留存策略。
import { h, asObject, shortId } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { refreshStatus, fetchSnapshots } from "../poller.js";
import { consoleCard, makeRun, actionButton, formatDateTime } from "../ui.js";
import { openSnapshot } from "./drawer.js";

const ROLE_LABEL = { user: "用户", assistant: "助手", developer: "系统注入", reasoning: "思考", tool_call: "工具调用", tool_output: "工具输出" };
const ROW_STYLE = { display: "flex", gap: "10px", padding: "10px 0", borderBottom: "1px solid var(--border)" };

const CONFIRM_MSG =
  "检测到 Codex 正在运行。\n\n这步会修改 Codex 的对话数据库，需要先关闭它的后台进程（codex.exe）——" +
  "它会在你下次使用 Codex 时自动重启，不影响编辑器和本页面。\n\n确认关闭 Codex 并继续吗？";

const WSL_CONFIRM_MSG =
  "检测到目标 WSL 里 Codex 正在运行。\n\n导入会写入该发行版的 ~/.codex 状态库，需要先关闭它的 Codex 后台进程。\n\n确认关闭并导入吗？";

const backupCache = {
  homes: null,
  targetChannelsByHome: new Map(),
};

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function fact(label, value) {
  return h("div", { class: "result-fact" }, h("span", {}, label), h("b", {}, value == null || value === "" ? "-" : String(value)));
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
  const { card: outCard, setOutput } = consoleCard("备份管理输出详情");

  let currentTab = "full"; // full | cloud | import | retention
  const tabContainer = h("div", { class: "sub-tabs" });
  const viewContainer = h("div", { class: "tab-view-root" });

  const tabs = [
    { id: "full", label: "完整备份与灾备" },
    { id: "cloud", label: "云端备份与快照" },
    { id: "import", label: "本地备份导入" },
    { id: "retention", label: "服务器留存策略" }
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

    if (tabId === "full") {
      unmountCurrent = mountFullBackups(viewContainer, run, store, setOutput);
    } else if (tabId === "cloud") {
      unmountCurrent = mountCloudSnapshots(viewContainer, run, store, setOutput);
    } else if (tabId === "import") {
      unmountCurrent = mountImportArchive(viewContainer, run, store, setOutput);
    } else if (tabId === "retention") {
      unmountCurrent = mountRetentionPolicy(viewContainer, run, store, setOutput);
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

// ==================== TAB 1: 完整备份与灾备 ====================
function mountFullBackups(container, run, store, setOutput) {
  let homes = [];
  let selectedHome = "windows";
  const homeTabs = h("div", { class: "home-tabs" });

  const uploadBtn = h("button", { class: "btn btn-danger", type: "button" }, "生成并上传到服务器（明文）");
  uploadBtn.addEventListener("click", async () => {
    const cfg = asObject(store.getState().status?.config);
    if (!cfg.full_backup_allow_plaintext_upload) {
      showToast("请先到「系统设置 → 备份参数策略」开启「允许云端明文上传」", "warning");
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
    const elements = homes.map(home =>
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
    );
    homeTabs.replaceChildren(...elements.filter(Boolean));
  }

  async function loadHomes(notify = false) {
    try {
      const result = await runAction("list-codex-homes");
      homes = Array.isArray(result.homes) ? result.homes : [];
      backupCache.homes = homes;
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

  syncUpload();
  if (backupCache.homes) {
    homes = backupCache.homes;
    if (!homes.some(h => h.id === selectedHome && h.ok && h.has_codex)) {
      selectedHome = (homes.find(h => h.ok && h.has_codex) || homes[0] || { id: "windows" }).id;
    }
    renderHomes();
  } else {
    loadHomes();
  }
  const uConfig = store.select(s => s.status?.config, syncUpload);

  return () => {
    uConfig();
  };
}

// ==================== TAB 2: 云端备份与快照 ====================
function mountCloudSnapshots(container, run, store, setOutput) {
  let cloudBackups = null;
  let loadingCloudBackups = false;
  let showAllCloudBackups = false;

  const cloudBackupsCard = h("div", { class: "card" });
  const tableBody = h("tbody", {});

  const snapCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" },
      h("span", {}, "云端轻量快照历史"),
      actionButton("刷新快照列表", "btn-ghost btn-sm", () => fetchSnapshots(store, { force: true }))
    ),
    h("p", { class: "card-desc" }, "快照仅记录最近的工作 Cwd、Git 补丁大小与活跃时间，主要用于多设备接力工作现场的紧急诊断与恢复。"),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, "发送设备"),
            h("th", {}, "项目路径"),
            h("th", {}, "上传时间（本机）"),
            h("th", {}, "快照 ID"),
            h("th", {}, "诊断操作")
          )
        ),
        tableBody
      )
    )
  );

  container.replaceChildren(
    h("div", { class: "section" }, cloudBackupsCard),
    h("div", { class: "section" }, snapCard)
  );

  async function refreshCloudBackups({ notify = false } = {}) {
    loadingCloudBackups = true;
    renderCloudBackups();
    try {
      cloudBackups = await runAction("list-full-backups");
      if (notify) {
        setOutput(cloudBackups);
        showToast(cloudBackups?.error ? "云端备份列表读取失败" : "云端备份列表已更新", cloudBackups?.error ? "error" : "success");
      }
    } catch (e) {
      cloudBackups = { success: false, error: String(e.message || e) };
      if (notify) {
        setOutput(cloudBackups);
        showToast(String(e.message || e), "error");
      }
    } finally {
      loadingCloudBackups = false;
      renderCloudBackups();
    }
  }

  function renderCloudBackups() {
    const backups = Array.isArray(cloudBackups?.full_backups) ? cloudBackups.full_backups : [];
    const visibleBackups = showAllCloudBackups ? backups : backups.slice(0, 12);
    const children = [
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "云端完整备份"),
        h("span", { class: `badge ${cloudBackups?.error ? "danger" : backups.length ? "ok" : ""}` },
          loadingCloudBackups ? "加载中" : cloudBackups?.error ? "失败" : `${backups.length} 个`
        ),
        actionButton("刷新云端备份", "btn-ghost btn-sm", () => refreshCloudBackups({ notify: true }))
      ),
      cloudBackups?.error
        ? h("div", { class: "status-line error" }, cloudBackups.error)
        : backups.length
          ? h(
              "div",
              {},
              h(
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
                  h("tbody", {}, ...visibleBackups.map(cloudBackupRow))
                )
              ),
              backups.length > 12
                ? h(
                    "div",
                    { class: "card-actions", style: { marginTop: "10px" } },
                    h("span", { class: "muted" }, showAllCloudBackups ? `已显示全部 ${backups.length} 个备份` : `已显示最新 12 个，共 ${backups.length} 个`),
                    actionButton(showAllCloudBackups ? "收起列表" : "显示全部", "btn-ghost btn-sm", () => {
                      showAllCloudBackups = !showAllCloudBackups;
                      renderCloudBackups();
                    })
                  )
                : null
            )
          : h("div", { class: "empty compact" }, loadingCloudBackups ? "正在读取云端备份…" : "点击右上方“刷新云端备份”按钮加载云端大包列表。")
    ];
    cloudBackupsCard.replaceChildren(...children.filter(Boolean));
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

  const renderTable = () => {
    const { items, loading, error } = store.getState().snapshots;
    if (loading && !items.length) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, loadingSpinner("正在拉取远程快照…"))));
      return;
    }
    if (error) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, `拉取失败：${error}`)));
      return;
    }
    if (!items.length) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, "暂无快照历史")));
      return;
    }
    const rows = items.map(snap => {
      const snapObj = asObject(snap);
      const mini = (label, intent) => {
        const b = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, label);
        b.addEventListener("click", () => openSnapshot(store, snapObj, intent));
        return b;
      };
      const cellActions = [mini("详情", "detail"), mini("接续", "resume"), mini("还原", "restore")];
      return h("tr", {},
        h("td", {}, snapObj.device_id || "-"),
        h("td", { class: "mono cell-ellipsis", title: snapObj.cwd || "" }, snapObj.cwd || "-"),
        h("td", { title: snapObj.created_at ? `UTC: ${snapObj.created_at}` : "" }, formatDateTime(snapObj.created_at)),
        h("td", { class: "mono", title: snapObj.id || "" }, snapObj.id ? shortId(snapObj.id) : "-"),
        h("td", {}, h("div", { class: "row-actions" }, ...cellActions.filter(Boolean)))
      );
    });
    tableBody.replaceChildren(...rows.filter(Boolean));
  };

  fetchSnapshots(store, { force: false }).catch(() => {});
  refreshCloudBackups();
  renderCloudBackups();

  const uSnapshots = store.select(s => s.snapshots, renderTable);

  return () => {
    uSnapshots();
  };
}

// ==================== TAB 3: 本地备份导入 ====================
function mountImportArchive(container, run, store, setOutput) {
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
    h(
      "div",
      { class: "card-actions" },
      h("span", { class: "muted" }, "选择备份源包"),
      backupSel,
      actionButton("刷新备份包列表", "btn-ghost btn-sm", loadBackups),
      actionButton("刷新目标环境", "btn-ghost btn-sm", refreshTargetHomes)
    )
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
    const opts = [
      h("option", { value: "" }, items.length ? `-- 选择备份包（共 ${items.length} 个）--` : "-- 无可用备份包 --"),
      ...items.map(b =>
        h("option", { value: b.backup_id }, `${sourceLabel(b.source)} · ${b.device_id || "?"} · ${formatDateTime(b.created_at)}${b.downloaded ? " ✓本地就绪" : ""}`)
      )
    ];
    backupSel.replaceChildren(...opts.filter(Boolean));
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

  async function loadHomes(force = false) {
    if (!force && Array.isArray(backupCache.homes)) {
      homes = backupCache.homes;
      renderTargetHomes();
      return;
    }
    if (force) backupCache.targetChannelsByHome.clear();
    try {
      const result = await runAction("list-codex-homes");
      homes = Array.isArray(result.homes) ? result.homes : [];
    } catch(e) {
      homes = [{ id: "windows", label: "Windows", kind: "windows", ok: true, has_codex: true }];
    }
    backupCache.homes = homes;
    renderTargetHomes();
  }

  function renderTargetHomes() {
    const usable = homes.filter(home => home.ok && home.has_codex);
    if (!usable.some(home => home.id === targetHome)) {
      targetHome = (usable[0] || homes[0] || { id: "windows" }).id;
    }
    const opts = homes.map(home =>
      h("option", { value: home.id, disabled: !home.ok || !home.has_codex }, `${home.label || home.id}${home.has_codex ? "" : "（无 .codex 目录）"}`)
    );
    homeSel.replaceChildren(...opts.filter(Boolean));
    homeSel.value = targetHome;
  }

  async function refreshTargetHomes() {
    await loadHomes(true);
    target = "";
    if (curBackupId) await loadConvos();
    else renderBatch();
    showToast("目标环境已刷新", "success");
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
      local = await loadTargetChannels();
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

    const targetOpts = groups.length ? groups : [h("option", { value: "" }, "未检测到可选渠道")];
    targetSel.replaceChildren(...targetOpts.filter(Boolean));
    targetSel.value = target || current || "";
    if (targetSel.value !== (target || current || "")) {
      targetSel.value = current || localProviders[0] || backupProviders[0] || "";
    }
    target = targetSel.value;
  }

  function uniqueProviders(items) {
    return [...new Set((items || []).filter(p => p && p !== "(unknown)"))].sort();
  }

  async function loadTargetChannels() {
    const key = targetHome || "windows";
    if (backupCache.targetChannelsByHome.has(key)) {
      return backupCache.targetChannelsByHome.get(key);
    }
    const result = await runAction("list-target-channels", { target_home: targetHome });
    backupCache.targetChannelsByHome.set(key, result);
    return result;
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
    const elements = list.map(convRow);
    listWrap.replaceChildren(h("div", {}, ...elements.filter(Boolean)));
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
    const rowChildren = [
      h("div", { style: { paddingTop: "2px" } }, cb),
      h("div", { style: { flex: "1", minWidth: "0" } }, title, tags)
    ];
    return h("div", { style: ROW_STYLE }, ...rowChildren.filter(Boolean));
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
      detailWrap.replaceChildren(h("div", { class: "empty" }, "点击左侧对话标题以预览对话正文"));
      return;
    }
    if (!detail.success) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "正文加载失败：" + (detail.error || "")));
      return;
    }
    const msgs = (detail.messages || []).map(m => {
      const msgChildren = [
        h("div", { class: "muted", style: { fontSize: "12px" } }, ROLE_LABEL[m.role] || m.role),
        h("div", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, m.text || "")
      ];
      return h("div", { style: msgStyle(m.role) }, ...msgChildren.filter(Boolean));
    });
    const nodes = [
      h("div", { class: "card-title" }, h("span", {}, (detail.thread || {}).title || "(无标题)")),
      h("div", {}, ...msgs),
      detail.truncated ? h("div", { class: "muted" }, "（已截断）") : null
    ];
    detailWrap.replaceChildren(...nodes.filter(Boolean));
  }

  function renderBatch() {
    const batchNodes = [
      h("span", { class: "muted" }, busy ? "加载中…" : `已选择 ${selected.size} 项`),
      h("span", { class: "muted", style: { marginLeft: "8px" } }, "目标位置"),
      homeSel,
      h("span", { class: "muted", style: { marginLeft: "8px" } }, "接入渠道"),
      targetSel,
      actionButton("导入到选定环境", "btn-primary btn-sm", doImport)
    ];
    batchWrap.replaceChildren(...batchNodes.filter(Boolean));
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

// ==================== TAB 4: 服务器留存策略 ====================
function mountRetentionPolicy(container, run, store, setOutput) {
  let retentionData = null;
  let retentionLoading = false;
  const retentionBody = h("div", { class: "result-stack" });

  async function loadServerRetention() {
    retentionLoading = true;
    renderRetention();
    try {
      retentionData = await runAction("server-retention-status");
      showToast(retentionData.success === false ? "服务器留存策略读取失败" : "服务器留存策略已更新", retentionData.success === false ? "error" : "success");
    } catch (e) {
      retentionData = { success: false, error: String(e.message || e) };
      showToast(String(e.message || e), "error");
    } finally {
      retentionLoading = false;
      renderRetention();
    }
  }

  async function pruneServerRetention() {
    if (!window.confirm("将删除服务器端超出留存策略的完整备份、项目备份和轻量快照。每个设备/项目会保留最新入口。确认执行？")) return;
    retentionLoading = true;
    renderRetention();
    try {
      retentionData = await runAction("server-retention-prune", { dry_run: false });
      const n = Number(retentionData.deleted_count || 0);
      showToast(`服务器清理完成：删除 ${n} 项`, retentionData.success === false ? "error" : "success");
    } catch (e) {
      retentionData = { success: false, error: String(e.message || e) };
      showToast(String(e.message || e), "error");
    } finally {
      retentionLoading = false;
      renderRetention();
    }
  }

  const retentionCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "服务器留存与自动清理"),
    h("p", { class: "card-desc" }, "服务器在接收到新备份后会自动按策略进行清理；这里可以随时查看保留预览，也可以手动强制立即执行清理。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("查看清理预览", "btn-ghost", loadServerRetention),
      actionButton("立即执行清理", "btn-danger", pruneServerRetention)
    ),
    retentionBody
  );

  container.replaceChildren(
    h("div", { class: "section" }, retentionCard)
  );

  function renderRetention() {
    if (retentionLoading) {
      retentionBody.replaceChildren(loadingSpinner("正在读取服务器留存状态…"));
      return;
    }
    if (!retentionData) {
      retentionBody.replaceChildren(h("div", { class: "empty compact" }, "尚未读取服务器留存状态，请点击上方“查看清理预览”。"));
      return;
    }
    if (retentionData.success === false || retentionData.error) {
      retentionBody.replaceChildren(h("div", { class: "status-line error" }, retentionData.error || "读取失败"));
      return;
    }
    const policy = asObject(retentionData.policy);
    const full = asObject(policy.full_backups);
    const project = asObject(policy.project_backups);
    const snapshots = asObject(policy.snapshots);
    const usage = asObject(retentionData.usage);
    const planned = Number(retentionData.planned_count || 0);
    const actionCount = retentionData.dry_run ? planned : Number(retentionData.deleted_count ?? planned);
    const actionBytes = retentionData.dry_run ? retentionData.planned_bytes : (retentionData.deleted_bytes ?? retentionData.planned_bytes);
    const nodes = [
      h(
        "div",
        { class: "result-facts" },
        fact("完整备份策略", `每设备/分支 ${full.keep_per_device_branch ?? "-"} 份 · ${full.max_age_days ?? "-"} 天`),
        fact("项目备份策略", `每项目/设备 ${project.keep_per_repo_device ?? "-"} 份 · ${project.max_age_days ?? "-"} 天`),
        fact("轻量快照策略", `每设备 ${snapshots.keep_per_device ?? "-"} 条 · ${snapshots.max_age_days ?? "-"} 天`),
        fact("服务器容量上限", formatBytes(policy.server_total_max_bytes)),
        fact("当前备份占用", formatBytes(usage.backup_bytes)),
        fact("当前记录", `完整 ${usage.full_backup_count || 0} · 项目 ${usage.project_backup_count || 0} · 快照 ${usage.snapshot_count || 0}`),
        fact(retentionData.dry_run ? "预览删除" : "本次删除", `${actionCount} 项 · ${formatBytes(actionBytes)}`),
        fact("已保护入口", `${usage.protected_count || 0} 项`)
      ),
      Array.isArray(retentionData.warnings) && retentionData.warnings.length
        ? h("div", { class: "status-line warn" }, retentionData.warnings.join("；"))
        : null
    ];
    retentionBody.replaceChildren(...nodes.filter(Boolean));
  }

  loadServerRetention();

  return () => {};
}

function sourceLabel(source) {
  if (source === "wsl") return "WSL";
  if (source === "local") return "本地缓存";
  return "云服务器";
}
