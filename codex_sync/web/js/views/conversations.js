// 对话与渠道管理页：对话浏览与搜索、本地多渠道整合与还原。
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

const browseCache = {
  homes: null,
  channelsByHome: new Map(),
  conversationsByHome: new Map(),
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

  let currentTab = "browse"; // browse | channels
  const tabContainer = h("div", { class: "sub-tabs" });
  const viewContainer = h("div", { class: "tab-view-root" });

  const tabs = [
    { id: "browse", label: "对话浏览" },
    { id: "channels", label: "渠道整合" }
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

    if (tabId === "browse") {
      unmountCurrent = mountBrowse(viewContainer, run, store, setOutput);
    } else if (tabId === "channels") {
      unmountCurrent = mountChannels(viewContainer, run, store, setOutput);
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

// ==================== TAB 1: 对话浏览 ====================
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
  let searchTimer = null;

  const homeSel = h("select", { class: "input", style: { flex: "0 1 180px" } });
  const searchInput = h("input", { class: "input", type: "search", style: { flex: "1 1 200px" }, placeholder: "搜索标题、预览、对话历史…" });
  const cwdSel = h("select", { class: "input", style: { flex: "1 1 150px" } });
  const provSel = h("select", { class: "input", style: { flex: "1 1 150px" } });
  const targetProviderSel = h("select", { class: "input", style: { flex: "0 1 180px" } });

  const listWrap = h("div", { class: "card", style: { maxHeight: "60vh", overflow: "auto" } });
  const detailWrap = h("div", { class: "card", style: { maxHeight: "60vh", overflow: "auto" } });
  const batchWrap = h("div", { class: "card-actions" });

  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      search = searchInput.value.trim();
      refreshConversations();
    }, 250);
  });
  cwdSel.addEventListener("change", () => { fcwd = cwdSel.value; refreshConversations(); });
  provSel.addEventListener("change", () => { fprovider = provSel.value; refreshConversations(); });
  targetProviderSel.addEventListener("change", () => { targetProvider = targetProviderSel.value; });

  const browserCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "对话浏览"),
    h("p", { class: "card-desc" }, "检索和批量管理本机所有 Codex 对话历史，点击标题查看正文详情。"),
    h(
      "div",
      { class: "card-actions" },
      homeSel,
      searchInput,
      cwdSel,
      provSel,
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: () => refreshConversations(true) }, "刷新对话"),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: refreshEnvironment }, "刷新环境")
    )
  );

  container.replaceChildren(
    h("div", { class: "section" }, browserCard),
    h("div", { class: "section" }, batchWrap),
    h("div", { class: "grid grid-2" }, listWrap, detailWrap)
  );

  function fillSelect(sel, items, cur, allLabel, labeler) {
    const opts = [
      h("option", { value: "" }, allLabel),
      ...(items || []).map(x => h("option", { value: x }, labeler ? labeler(x) : x))
    ];
    sel.replaceChildren(...opts.filter(Boolean));
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
    const opts = [
      h("option", { value: "all" }, loading ? "正在检测本机环境…" : "全部本机环境"),
      ...(homes || []).map(home =>
        h(
          "option",
          { value: home.id, disabled: !home.ok || !home.has_codex },
          `${home.label || home.id}${home.ok && home.has_codex ? "" : "（不可用）"}`
        )
      )
    ];
    homeSel.replaceChildren(...opts.filter(Boolean));
    homeSel.value = sourceHome;
  }

  async function loadHomes(force = false) {
    if (!force && Array.isArray(browseCache.homes)) {
      homes = browseCache.homes;
      renderHomeOptions(false);
      return;
    }
    if (force) {
      browseCache.channelsByHome.clear();
      browseCache.conversationsByHome.clear();
    }
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

  function isRowInSourceHome(row) {
    return sourceHome !== "all" && (row.home_id || "windows") === sourceHome;
  }

  function disabledButton(label, cls, title) {
    return h("button", { class: `btn ${cls}`, type: "button", disabled: true, title }, label);
  }

  async function refreshAll(force = false) {
    await Promise.all([loadChannelsData(force), refreshConversations(force)]);
  }

  async function refreshEnvironment() {
    await loadHomes(true);
    await refreshAll(true);
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

  async function loadChannelsData(force = false) {
    const cacheKey = sourceHome || "windows";
    if (!force && browseCache.channelsByHome.has(cacheKey)) {
      channelsData = browseCache.channelsByHome.get(cacheKey);
      fillTargetProviderOptions();
      return;
    }
    try {
      channelsData = await runAction("list-channels", { source_home: sourceHome });
      browseCache.channelsByHome.set(cacheKey, channelsData);
    } catch(e) {
      channelsData = { success: false, error: String(e.message || e) };
    }
    fillTargetProviderOptions();
  }

  async function refreshConversations(force = false) {
    const cacheKey = sourceHome || "windows";
    if (!force && browseCache.conversationsByHome.has(cacheKey)) {
      conversationsData = filterConversations(browseCache.conversationsByHome.get(cacheKey));
      syncConversationFilters();
      fillTargetProviderOptions();
      renderList();
      renderBatch();
      return;
    }

    busy = true;
    renderBatch();
    try {
      const raw = await runAction("list-conversations", { source_home: sourceHome, search: "", cwd: "", provider: "" });
      browseCache.conversationsByHome.set(cacheKey, raw);
      conversationsData = filterConversations(raw);
    } catch (e) {
      conversationsData = { success: false, error: String(e.message || e) };
    }
    busy = false;
    syncConversationFilters();
    fillTargetProviderOptions();
    renderList();
    renderBatch();
  }

  function filterConversations(data) {
    if (!data || !data.success) return data;
    const needle = search.trim().toLowerCase();
    const filtered = (data.conversations || []).filter(c => {
      if (fcwd && c.cwd !== fcwd) return false;
      if (fprovider && c.model_provider !== fprovider) return false;
      if (!needle) return true;
      const haystack = [c.title, c.preview, c.first_user_message, c.cwd, c.model_provider, c.home_label]
        .map(v => String(v || "").toLowerCase())
        .join("\n");
      return haystack.includes(needle);
    });
    return { ...data, conversations: filtered, count: filtered.length };
  }

  function syncConversationFilters() {
    if (!conversationsData || !conversationsData.success) return;
    fillSelect(cwdSel, conversationsData.cwds, fcwd, "全部项目", shortCwd);
    fillSelect(provSel, conversationsData.providers, fprovider, "全部渠道");
  }

  function fillTargetProviderOptions() {
    const current = channelsData?.current_provider || "";
    const fromChannels = Array.isArray(channelsData?.channels) ? channelsData.channels.map(c => c.provider) : [];
    const fromConversations = Array.isArray(conversationsData?.providers) ? conversationsData.providers : [];
    const providers = [...new Set([current, ...fromChannels, ...fromConversations].filter(p => p && p !== "(unknown)"))].sort();
    if (!providers.length) {
      targetProvider = "";
      const opts = [h("option", { value: "" }, "无可用目标渠道")];
      targetProviderSel.replaceChildren(...opts.filter(Boolean));
      targetProviderSel.disabled = true;
      return;
    }
    targetProviderSel.disabled = false;
    if (!targetProvider || !providers.includes(targetProvider)) targetProvider = current || providers[0];
    const opts = providers.map(provider =>
      h("option", { value: provider }, provider === current ? `${provider}（当前）` : provider)
    );
    targetProviderSel.replaceChildren(...opts.filter(Boolean));
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
    const children = [
      errors.length ? h("div", { class: "muted", style: { fontSize: "12px", marginBottom: "8px" } }, `部分环境读取失败：${errors.map(e => e.label || e.home_id).join("、")}`) : null,
      ...convos.map(convRow)
    ];
    listWrap.replaceChildren(...children.filter(Boolean));
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
    const children = [
      h("div", { style: { paddingTop: "2px" } }, cb),
      h("div", { style: { flex: "1", minWidth: "0" } }, title, meta, preview)
    ];
    return h("div", { style: ROW_STYLE }, ...children.filter(Boolean));
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
      detailWrap.replaceChildren(h("div", { class: "empty" }, "点击左侧对话标题查看具体正文"));
      return;
    }
    if (!detail.success) {
      detailWrap.replaceChildren(h("div", { class: "empty" }, "正文加载失败：" + (detail.error || "")));
      return;
    }
    const t = detail.thread || {};
    const msgs = (detail.messages || []).map(m => {
      const children = [
        h("div", { class: "muted", style: { fontSize: "12px", marginBottom: "2px" } }, ROLE_LABEL[m.role] || m.role),
        h("div", { style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }, m.text || "")
      ];
      return h("div", { style: msgStyle(m.role) }, ...children.filter(Boolean));
    });
    const nodes = [
      h("div", { class: "card-title" }, h("span", {}, t.title || "(无标题)")),
      h("p", { class: "card-desc" }, `${detail.home_label || t.home_label || homeLabel(t.home_id)} · ${t.model_provider || "-"} · ${shortCwd(t.cwd)} · ${detail.message_count} 条消息`),
      h("div", {}, ...msgs),
      detail.truncated ? h("div", { class: "muted" }, "（正文较长，已截断）") : null
    ];
    detailWrap.replaceChildren(...nodes.filter(Boolean));
  }

  function renderBatch() {
    const rows = selectedRows();
    const canWrite = sourceHome !== "all" && rows.every(isRowInSourceHome);
    const writeTip = "并入/还原需要先选择单个 Windows 或 WSL 环境。";
    const batchNodes = [
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
    ];
    batchWrap.replaceChildren(...batchNodes.filter(Boolean));
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
    if (sourceHome === "all" || rows.some(row => !isRowInSourceHome(row))) {
      showToast("请先选择单个 Windows 或 WSL 环境。", "warning");
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
      await refreshAll(true);
      refreshStatus(store).catch(() => {});
    }
  }

  renderHomeOptions(true);
  listWrap.replaceChildren(loadingSpinner("正在检测本机环境…"));
  detailWrap.replaceChildren(h("div", { class: "empty" }, "点击左侧对话标题查看具体正文"));
  loadHomes(false).finally(() => refreshAll(false));

  return () => {
    clearTimeout(searchTimer);
  };
}

// ==================== TAB 2: 渠道整合 ====================
function mountChannels(container, run, store, setOutput) {
  let homes = [];
  let sourceHome = "windows"; // 默认只选择 windows，因为渠道写入功能仅支持它
  let channelsData = null;
  let busy = false;

  const homeSel = h("select", { class: "input", style: { flex: "0 1 200px" } });
  const channelsCardBody = h("div", { class: "card-body" });

  const channelsCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "本地渠道整合与分布"),
    h("p", { class: "card-desc" }, "查看与归并在各个环境与服务提供商下分布的对话数据，支持一键软链接渠道或无损还原。"),
    h(
      "div",
      { class: "card-actions", style: { marginBottom: "15px" } },
      homeSel,
      actionButton("刷新渠道", "btn-ghost btn-sm", () => refreshChannels(true)),
      actionButton("刷新运行环境", "btn-ghost btn-sm", () => loadHomes(true))
    ),
    channelsCardBody
  );

  container.replaceChildren(
    h("div", { class: "section" }, channelsCard)
  );

  function homeLabel(homeId) {
    if (homeId === "all") return "全部本机环境";
    const home = homes.find(item => item.id === homeId);
    if (home) return home.label || home.id;
    if (String(homeId || "").startsWith("wsl:")) return `WSL ${String(homeId).split(":", 2)[1]}`;
    return "Windows";
  }

  function renderHomeOptions(loading = false) {
    homeSel.replaceChildren(
      h("option", { value: "all" }, loading ? "正在检测本机环境…" : "全部本机环境 (只读)"),
      ...homes.map(home =>
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
    if (force) {
      browseCache.channelsByHome.clear();
      browseCache.conversationsByHome.clear();
    }
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

  homeSel.addEventListener("change", async () => {
    sourceHome = homeSel.value || "all";
    await refreshChannels(false);
  });

  async function refreshChannels(force = false) {
    const cacheKey = sourceHome || "windows";
    if (!force && browseCache.channelsByHome.has(cacheKey)) {
      channelsData = browseCache.channelsByHome.get(cacheKey);
      renderChannels();
      return;
    }
    channelsCardBody.replaceChildren(loadingSpinner("正在加载渠道分布数据…"));
    try {
      channelsData = await runAction("list-channels", { source_home: sourceHome });
      browseCache.channelsByHome.set(cacheKey, channelsData);
    } catch(e) {
      channelsData = { success: false, error: String(e.message || e) };
    }
    renderChannels();
  }

  async function submitChannelOp(action, opts, label) {
    if (busy) return;
    if (sourceHome === "all" || (channelsData && channelsData.write_supported === false)) {
      showToast("渠道并入/还原需要先选择单个 Windows 或 WSL 环境。", "warning");
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
      refreshStatus(store).catch(() => {});
    }
  }

  function renderChannels() {
    if (!channelsData) {
      channelsCardBody.replaceChildren(h("p", { class: "muted" }, "正在加载渠道分布数据…"));
      return;
    }
    if (!channelsData.success) {
      channelsCardBody.replaceChildren(
        h("p", { class: "muted" }, "读取渠道失败：" + (channelsData.error || "")),
        actionButton("刷新重试", "btn-ghost btn-sm", () => refreshChannels(true))
      );
      return;
    }

    if (channelsData.home_groups) {
      const groups = channelsData.home_groups || [];
      const nodes = [
        h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "10px" } },
          h("span", {}, "查看范围：", h("strong", {}, "全部本机环境")),
          h("span", { class: "badge warn" }, "只读")
        ),
        h("p", { class: "card-desc" }, "跨环境查看按 Windows/WSL 分组展示；并入或还原请先切换到单个环境。"),
        ...groups.map(group => renderChannelGroup(group))
      ];
      channelsCardBody.replaceChildren(...nodes.filter(Boolean));
      return;
    }

    const writable = channelsData.write_supported !== false && sourceHome !== "all";
    const cur = channelsData.current_provider || "(未知)";
    const running = Boolean(channelsData.codex_running);
    const merged = channelsData.merged || {};

    const rows = (channelsData.channels || []).map(c => {
      const isCur = c.provider === cur;
      const into = (merged.by_target && merged.by_target[c.provider]) || 0;
      const rowChildren = [
        h("div", {},
          h("strong", {}, c.provider),
          isCur ? h("span", { class: "badge ok", style: { marginLeft: "8px" } }, "当前") : null,
          into ? h("span", { class: "badge", style: { marginLeft: "8px" } }, `含并入 ${into}`) : null
        ),
        h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
          h("span", { class: "muted" }, `${c.threads} 个对话`),
          isCur || !writable ? h("span", { class: "muted" }, "—") : actionButton("并入当前", "btn-primary btn-sm", () => submitChannelOp("merge-channels", { sources: [c.provider] }, `并入 ${c.provider}`))
        )
      ];
      return h("div", { style: ROW_STYLE }, ...rowChildren.filter(Boolean));
    });

    const origins = {};
    const ob = merged.origin_breakdown || {};
    for (const t in ob) for (const o in ob[t]) origins[o] = (origins[o] || 0) + ob[t][o];
    const originKeys = Object.keys(origins).sort();

    const runBadge = writable
      ? h("span", { class: `badge ${running ? "warn" : "ok"}` }, running ? "Codex 运行中" : "Codex 未运行")
      : h("span", { class: "badge warn" }, "只读");

    const nodes = [
      h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "10px" } },
        h("span", {}, `${homeLabel(sourceHome)} 默认渠道：`, h("strong", {}, cur)),
        runBadge
      ),
      writable
        ? h("p", { class: "card-desc" }, "「并入当前」会修改被合并渠道的 provider，将它们显示在你的当前对话列表里。这只是软链接，不会破坏会话内容，随时能一键还原。")
        : h("p", { class: "card-desc" }, "全部环境视图为只读；并入或还原请切换到单个 Windows 或 WSL 环境。"),
      rows.length ? h("div", { style: { margin: "10px 0" } }, ...rows) : h("div", { class: "empty" }, "暂无渠道数据"),
      writable && merged.total
        ? h("div", { style: { borderTop: "1px dashed var(--border)", paddingTop: "10px", marginTop: "10px" } },
            h("strong", {}, "已并入列表（可按源还原）："),
            ...originKeys.map(o => {
              const oChildren = [
                h("div", {}, "源渠道 ", h("strong", {}, o), ` · ${origins[o]} 个对话`),
                actionButton(`还原 ${o}`, "btn-ghost btn-sm", () => submitChannelOp("restore-channels", { sources: [o] }, `还原 ${o}`))
              ];
              return h("div", { style: ROW_STYLE }, ...oChildren.filter(Boolean));
            }),
            h("div", { class: "card-actions", style: { marginTop: "10px" } }, actionButton("全部还原", "btn-ghost btn-sm", () => submitChannelOp("restore-channels", { all: true }, "全部还原")))
          )
        : null,
      writable ? h("div", { class: "card-actions", style: { marginTop: "15px" } },
        actionButton("全部并入当前", "btn-primary btn-sm", () => submitChannelOp("merge-channels", { all: true }, "全部并入"))
      ) : null
    ];
    channelsCardBody.replaceChildren(...nodes.filter(Boolean));
  }

  function renderChannelGroup(group) {
    if (!group || !group.success) {
      return h("div", { class: "empty", style: { margin: "10px 0" } }, `${group?.home_label || "本机环境"} 读取失败：${group?.error || ""}`);
    }
    const cur = group.current_provider || "(未知)";
    const rows = (group.channels || []).map(c => {
      const rowChildren = [
        h("div", {},
          h("strong", {}, c.provider),
          c.provider === cur ? h("span", { class: "badge ok", style: { marginLeft: "8px" } }, "当前") : null
        ),
        h("span", { class: "muted" }, `${c.threads} 个对话`)
      ];
      return h("div", { style: ROW_STYLE }, ...rowChildren.filter(Boolean));
    });
    const groupChildren = [
      h("div", { style: { display: "flex", justifyContent: "space-between", marginBottom: "6px" } },
        h("strong", {}, group.home_label || group.home_id || "本机环境"),
        h("span", { class: "badge warn" }, "只读")
      ),
      h("div", { class: "muted", style: { fontSize: "12px" } }, "默认渠道：", cur),
      rows.length ? h("div", {}, ...rows) : h("div", { class: "empty" }, "暂无渠道数据")
    ];
    return h(
      "div",
      { style: { borderTop: "1px dashed var(--border)", paddingTop: "10px", marginTop: "10px" } },
      ...groupChildren.filter(Boolean)
    );
  }

  loadHomes(false).finally(() => refreshChannels(false));

  return () => {};
}
