// 对话与渠道：对话浏览（搜索、筛选、正文查看、导出、批量改渠道）与本地渠道整合。
import { h, asObject, fill } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { refreshStatus } from "../poller.js";
import { actionButton, createSubTabs } from "../ui.js";
import { confirmDialog, confirmCloseCodex } from "../dialog.js";
import { renderMessage } from "../markdown.js";
import { copyText, failureReason, formatEpoch, isFailed, loadingSpinner, shortCwd, statusLine } from "../format.js";

const PAGE_SIZE = 100;

// 两个页签共用：切换页签或页面时不必重新扫描环境。
const browseCache = {
  homes: null,
  workspaceByHome: new Map(),
};

function homeLabelFor(homes, homeId) {
  if (homeId === "all") return "全部本机环境";
  const home = homes.find((item) => item.id === homeId);
  if (home) return home.label || home.id;
  if (String(homeId || "").startsWith("wsl:")) return `WSL ${String(homeId).split(":", 2)[1]}`;
  return "Windows";
}

function homeOptions(homes, { allLabel }) {
  return [
    h("option", { value: "all" }, allLabel),
    ...homes.map((home) =>
      h("option", { value: home.id, disabled: !home.ok || !home.has_codex }, `${home.label || home.id}${home.ok && home.has_codex ? "" : "（不可用）"}`)
    ),
  ];
}

async function fetchWorkspace(sourceHome, { force = false, probe = false } = {}) {
  if (force) browseCache.workspaceByHome.clear();
  if (!force && browseCache.workspaceByHome.has(sourceHome)) return browseCache.workspaceByHome.get(sourceHome);
  let workspace;
  try {
    workspace = await runAction("conversation-workspace", { source_home: sourceHome, force, probe_homes: probe });
    browseCache.workspaceByHome.set(sourceHome, workspace);
  } catch (e) {
    workspace = { success: false, error: String(e.message || e) };
  }
  if (Array.isArray(workspace.homes)) browseCache.homes = workspace.homes;
  return workspace;
}

async function openExportsDir() {
  try {
    const result = await runAction("open-exports-dir");
    if (isFailed(result)) showToast(`打开导出文件夹失败：${failureReason(result)}`, "error");
  } catch (e) {
    showToast(`打开导出文件夹失败：${e.message || e}`, "error");
  }
}

export function mount(root, store, { sub } = {}) {
  const viewContainer = h("div");
  let unmountCurrent = null;

  const tabs = createSubTabs(
    [
      { key: "browse", label: "对话浏览" },
      { key: "channels", label: "渠道整合" },
    ],
    { initial: sub, onChange: show, label: "对话页面分区" }
  );

  function show(key) {
    if (unmountCurrent) {
      try {
        unmountCurrent();
      } catch {
        /* ignore */
      }
      unmountCurrent = null;
    }
    viewContainer.replaceChildren();
    unmountCurrent = key === "channels" ? mountChannels(viewContainer, store) : mountBrowse(viewContainer, store);
  }

  fill(root, tabs.el, viewContainer);
  show(tabs.active);

  return () => {
    if (unmountCurrent) unmountCurrent();
  };
}

// ==================== 对话浏览 ====================
function mountBrowse(container, store) {
  let alive = true;
  let homes = Array.isArray(browseCache.homes) ? browseCache.homes : [];
  let sourceHome = "all";
  let baseData = null; // 工作区返回的最近对话（客户端筛选的数据源）
  let listData = null; // 当前列表实际显示的数据
  let channelsData = null;
  let loadingList = true;
  let querySeq = 0;
  const selected = new Map();
  let search = "";
  let fcwd = "";
  let fprovider = "";
  let includeArchived = false;
  let targetProvider = "";
  let shown = PAGE_SIZE;
  let searchTimer = null;

  let detailRow = null;
  let detail = null;
  let detailLoading = false;
  let detailSeq = 0;
  let showTools = false;

  const homeSel = h("select", { class: "input", "aria-label": "对话所在环境" });
  const searchInput = h("input", { class: "input", type: "search", placeholder: "搜索标题、首条消息、项目路径或渠道", "aria-label": "搜索对话" });
  const cwdSel = h("select", { class: "input", "aria-label": "按项目筛选" }, h("option", { value: "" }, "全部项目"));
  const provSel = h("select", { class: "input", "aria-label": "按渠道筛选" }, h("option", { value: "" }, "全部渠道"));
  const archivedBox = h("input", { type: "checkbox" });
  const targetProviderSel = h("select", { class: "input", "aria-label": "目标渠道" });
  const noticeWrap = h("div");
  const batchWrap = h("div", { class: "batch-bar" });
  const listHead = h("div", { class: "conv-pane-head" });
  const listBody = h("div", { class: "conv-pane-body" });
  const detailPane = h("div", { class: "conv-pane" });

  fill(container,
    h(
      "div",
      { class: "card section" },
      h(
        "div",
        { class: "filter-bar" },
        homeSel,
        searchInput,
        cwdSel,
        provSel,
        h("label", { class: "checkbox-control", title: "已归档的对话默认不显示" }, archivedBox, h("span", {}, "包含已归档")),
        actionButton("刷新", "btn-ghost btn-sm", () => loadWorkspace({ force: true }), { title: "重新读取对话列表" }),
        actionButton("重新检测环境", "btn-ghost btn-sm", () => loadWorkspace({ force: true, probe: true }), { title: "重新扫描 Windows 和 WSL 里的 Codex" })
      ),
      noticeWrap
    ),
    h("div", { class: "section" }, batchWrap),
    h("div", { class: "conv-layout" }, h("div", { class: "conv-pane" }, listHead, listBody), detailPane)
  );

  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      search = searchInput.value.trim();
      applyFilters();
    }, 250);
  });
  cwdSel.addEventListener("change", () => {
    fcwd = cwdSel.value;
    applyFilters();
  });
  provSel.addEventListener("change", () => {
    fprovider = provSel.value;
    applyFilters();
  });
  archivedBox.addEventListener("change", () => {
    includeArchived = archivedBox.checked;
    applyFilters();
  });
  targetProviderSel.addEventListener("change", () => {
    targetProvider = targetProviderSel.value;
  });
  homeSel.addEventListener("change", () => {
    sourceHome = homeSel.value || "all";
    selected.clear();
    fcwd = "";
    fprovider = "";
    detailRow = null;
    detail = null;
    renderDetail();
    loadWorkspace();
  });

  const homeLabel = (homeId) => homeLabelFor(homes, homeId);
  const convKey = (c) => `${c.home_id || "windows"}::${c.id}`;
  const selectedRows = () => [...selected.values()];
  const isRowInSourceHome = (row) => sourceHome !== "all" && (row.home_id || "windows") === sourceHome;
  const filtersActive = () => Boolean(search || fcwd || fprovider);
  const needsServerQuery = () => includeArchived || Boolean(baseData?.truncated && filtersActive());

  function renderHomeOptions(loading = false) {
    const usable = homes.filter((home) => home && home.ok && home.has_codex);
    if (sourceHome !== "all" && !usable.some((home) => home.id === sourceHome)) sourceHome = "all";
    fill(homeSel, ...homeOptions(homes, { allLabel: loading ? "正在检测本机环境…" : "全部本机环境" }));
    homeSel.value = sourceHome;
  }

  function fillSelect(sel, items, current, allLabel, labeler) {
    fill(sel,
      h("option", { value: "" }, allLabel),
      ...(items || []).map((item) => h("option", { value: item, title: item }, labeler ? labeler(item) : item))
    );
    sel.value = current || "";
  }

  function syncFilterOptions() {
    const data = baseData && baseData.success ? baseData : null;
    if (!data) return;
    fillSelect(cwdSel, data.cwds, fcwd, "全部项目", shortCwd);
    fillSelect(provSel, data.providers, fprovider, "全部渠道");
  }

  function fillTargetProviderOptions() {
    const current = channelsData?.current_provider || "";
    const fromChannels = Array.isArray(channelsData?.channels) ? channelsData.channels.map((c) => c.provider) : [];
    const fromConversations = Array.isArray(baseData?.providers) ? baseData.providers : [];
    const providers = [...new Set([current, ...fromChannels, ...fromConversations].filter((p) => p && p !== "(unknown)"))].sort();
    if (!providers.length) {
      targetProvider = "";
      fill(targetProviderSel, h("option", { value: "" }, "无可用渠道"));
      targetProviderSel.disabled = true;
      return;
    }
    targetProviderSel.disabled = false;
    if (!targetProvider || !providers.includes(targetProvider)) targetProvider = current || providers[0];
    fill(targetProviderSel, ...providers.map((p) => h("option", { value: p }, p === current ? `${p}（当前）` : p)));
    targetProviderSel.value = targetProvider;
  }

  async function loadWorkspace({ force = false, probe = false } = {}) {
    loadingList = true;
    if (!browseCache.workspaceByHome.has(sourceHome) || force) {
      renderHomeOptions(!homes.length);
      renderList();
    }
    const workspace = await fetchWorkspace(sourceHome, { force, probe });
    if (!alive) return;
    homes = Array.isArray(browseCache.homes) ? browseCache.homes : homes;
    if (!homes.length) homes = [{ id: "windows", kind: "windows", label: "Windows", ok: true, has_codex: true }];
    baseData =
      workspace?.conversations && typeof workspace.conversations === "object"
        ? workspace.conversations
        : { success: false, error: workspace?.error || "读取对话列表失败" };
    channelsData = workspace?.channels && typeof workspace.channels === "object" ? workspace.channels : null;
    renderHomeOptions(false);
    syncFilterOptions();
    fillTargetProviderOptions();
    await applyFilters();
  }

  function clientFilter(data) {
    if (!data || !data.success) return data;
    const needle = search.toLowerCase();
    const filtered = (data.conversations || []).filter((c) => {
      if (fcwd && c.cwd !== fcwd) return false;
      if (fprovider && c.model_provider !== fprovider) return false;
      if (!needle) return true;
      return [c.title, c.preview, c.first_user_message, c.cwd, c.model_provider, c.home_label]
        .map((v) => String(v || "").toLowerCase())
        .join("\n")
        .includes(needle);
    });
    return { ...data, conversations: filtered, count: filtered.length, serverQuery: false };
  }

  async function applyFilters() {
    shown = PAGE_SIZE;
    if (!baseData || !baseData.success) {
      loadingList = false;
      listData = baseData;
      renderAll();
      return;
    }
    const seq = (querySeq += 1);
    if (!needsServerQuery()) {
      loadingList = false;
      listData = clientFilter(baseData);
      renderAll();
      return;
    }
    // 归档对话、或最近列表被截断时的搜索/筛选：交给后端在全部对话里查
    loadingList = true;
    renderList();
    let result;
    try {
      result = await runAction("list-conversations", {
        source_home: sourceHome,
        search,
        cwd: fcwd,
        provider: fprovider,
        include_archived: includeArchived,
      });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive || seq !== querySeq) return;
    loadingList = false;
    listData = result && result.success ? { ...result, serverQuery: true } : result;
    renderAll();
  }

  function renderAll() {
    renderNotice();
    renderList();
    renderBatch();
  }

  function renderNotice() {
    const nodes = [];
    const errors = Array.isArray(listData?.errors) ? listData.errors : [];
    if (errors.length) {
      nodes.push(h("div", { class: "status-line warn" }, `部分环境读取失败：${errors.map((e) => `${e.label || e.home_id}（${e.error || "未知错误"}）`).join("、")}`));
    }
    if (listData?.success && listData.truncated) {
      const count = (listData.conversations || []).length;
      nodes.push(
        h(
          "div",
          { class: "status-line info" },
          listData.serverQuery
            ? `匹配的对话较多，只显示最近 ${count} 个。可以输入更具体的关键词缩小范围。`
            : `为了加载速度，列表只包含最近 ${baseData?.count ?? count} 个对话。输入搜索词或选择筛选条件时，会在全部对话中查找。`
        )
      );
    }
    fill(noticeWrap, ...nodes);
    if (nodes.length) noticeWrap.style.marginTop = "12px";
    else noticeWrap.style.marginTop = "";
  }

  function clearFilters() {
    search = "";
    fcwd = "";
    fprovider = "";
    includeArchived = false;
    searchInput.value = "";
    cwdSel.value = "";
    provSel.value = "";
    archivedBox.checked = false;
    applyFilters();
  }

  function renderList() {
    if (!listData || (loadingList && !listData?.success)) {
      fill(listHead, h("span", {}, "对话列表"));
      fill(listBody, loadingSpinner(homes.length ? "正在读取对话…" : "正在检测本机环境…"));
      return;
    }
    if (!listData.success) {
      fill(listHead, h("span", {}, "对话列表"));
      fill(listBody,
        h("div", { class: "status-line error" }, `读取对话失败：${listData.error || "未知错误"}`),
        h("div", { class: "list-more" }, actionButton("重试", "btn-ghost btn-sm", () => loadWorkspace({ force: true })))
      );
      return;
    }
    const convos = listData.conversations || [];
    fill(listHead,
      h("span", {}, loadingList ? "正在查找…" : `${convos.length} 个对话`),
      loadingList ? h("span", { class: "spinner", "aria-hidden": "true" }) : null
    );
    if (!convos.length) {
      const hasFilter = filtersActive() || includeArchived;
      fill(listBody,
        h("div", { class: "empty" }, hasFilter ? "没有匹配的对话。" : "这个环境里还没有 Codex 对话。"),
        hasFilter ? h("div", { class: "list-more" }, h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: clearFilters }, "清空筛选条件")) : null
      );
      return;
    }
    const rows = convos.slice(0, shown).map(convRow);
    const rest = convos.length - shown;
    const more =
      rest > 0
        ? h(
            "div",
            { class: "list-more" },
            h(
              "button",
              {
                class: "btn btn-ghost btn-sm",
                type: "button",
                onClick: () => {
                  shown += PAGE_SIZE;
                  renderList();
                },
              },
              `显示更多（还有 ${rest} 个）`
            )
          )
        : null;
    fill(listBody, ...rows, more);
  }

  function convRow(c) {
    const key = convKey(c);
    const titleText = c.title || "(无标题)";
    const cb = h("input", { type: "checkbox", "aria-label": `选择：${titleText}` });
    cb.checked = selected.has(key);
    cb.addEventListener("change", () => {
      if (cb.checked) selected.set(key, c);
      else selected.delete(key);
      renderBatch();
    });
    const meta = h(
      "div",
      { class: "conv-meta" },
      sourceHome === "all" ? h("span", { class: "badge" }, c.home_label || homeLabel(c.home_id)) : null,
      h("span", {}, formatEpoch(c.updated_at)),
      h("span", { title: c.cwd || "" }, `· ${shortCwd(c.cwd)}`),
      h("span", { class: "badge" }, c.model_provider || "-"),
      c.merged ? h("span", { class: "badge ok", title: c.original_provider ? `原渠道：${c.original_provider}` : "" }, "已并入") : null,
      Number(c.archived) ? h("span", { class: "badge warn" }, "已归档") : null
    );
    const previewText = c.preview || c.first_user_message || "";
    const row = h(
      "div",
      { class: `conv-row${detailRow && convKey(detailRow) === key ? " active" : ""}`, dataset: { key } },
      cb,
      h(
        "div",
        { class: "conv-main" },
        h("button", { class: "conv-title", type: "button", title: titleText, onClick: () => openDetail(c) }, titleText),
        meta,
        previewText ? h("div", { class: "conv-preview" }, String(previewText).slice(0, 160)) : null
      )
    );
    row.addEventListener("click", (event) => {
      if (event.target === cb || event.target.closest("button")) return;
      openDetail(c);
    });
    return row;
  }

  function markActive() {
    const key = detailRow ? convKey(detailRow) : "";
    for (const row of listBody.querySelectorAll(".conv-row")) row.classList.toggle("active", row.dataset.key === key);
  }

  function renderBatch() {
    const rows = selectedRows();
    const n = rows.length;
    const convos = listData?.success ? listData.conversations || [] : [];
    const canWrite = n > 0 && sourceHome !== "all" && rows.every(isRowInSourceHome);
    const writeTip = sourceHome === "all" ? "改渠道需要先在上方选择单个 Windows 或 WSL 环境" : n ? "" : "请先勾选对话";
    fill(batchWrap,
      h("span", { class: "batch-count" }, `已选 ${n} 个`),
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm",
          type: "button",
          disabled: !convos.length,
          onClick: () => {
            convos.forEach((c) => selected.set(convKey(c), c));
            renderList();
            renderBatch();
          },
        },
        convos.length ? `全选（${convos.length}）` : "全选"
      ),
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm",
          type: "button",
          disabled: !n,
          onClick: () => {
            selected.clear();
            renderList();
            renderBatch();
          },
        },
        "清空选择"
      ),
      actionButton("导出所选", "btn-ghost btn-sm", () => exportRows(selectedRows()), { disabled: !n, title: n ? "导出为 Markdown 文件" : "请先勾选对话" }),
      h("span", { class: "divider", "aria-hidden": "true" }),
      h("span", { class: "batch-label" }, "改到渠道"),
      targetProviderSel,
      actionButton("改到此渠道", "btn-primary btn-sm", () => writeOp("merge-threads", "已改到目标渠道", targetProvider || targetProviderSel.value), {
        disabled: !canWrite,
        title: writeTip || "把所选对话移到目标渠道，随时可以还原",
      }),
      actionButton("还原原始渠道", "btn-ghost btn-sm", () => writeOp("restore-threads", "已还原"), { disabled: !canWrite, title: writeTip || "" }),
      sourceHome === "all" ? h("span", { class: "batch-label small" }, "改渠道需先选择单个环境") : null
    );
  }

  async function exportRows(rows) {
    if (!rows.length) {
      showToast("请先勾选要导出的对话", "warning");
      return;
    }
    let okCount = 0;
    const failures = [];
    for (const row of rows) {
      const name = row.title || row.id;
      try {
        const result = await runAction("export-conversation", { thread_id: row.id, source_home: row.home_id || "windows" });
        if (result && result.path && !isFailed(result)) okCount += 1;
        else failures.push(`${name}：${failureReason(result)}`);
      } catch (e) {
        failures.push(`${name}：${e.message || e}`);
      }
    }
    const action = { label: "打开导出文件夹", onClick: openExportsDir };
    if (!failures.length) showToast(`已导出 ${okCount} 个对话（Markdown）`, "success", { action });
    else if (okCount) showToast(`已导出 ${okCount} 个，${failures.length} 个失败。\n${failures[0]}`, "warning", { action });
    else showToast(`导出失败：${failures[0]}`, "error");
  }

  async function writeOp(action, label, target = "") {
    const rows = selectedRows();
    if (!rows.length) {
      showToast("请先勾选对话", "warning");
      return;
    }
    if (sourceHome === "all" || rows.some((row) => !isRowInSourceHome(row))) {
      showToast("请先选择单个 Windows 或 WSL 环境", "warning");
      return;
    }
    if (action === "merge-threads" && !target) {
      showToast("请先选择目标渠道", "warning");
      return;
    }
    const payload = { source_home: sourceHome, thread_ids: rows.map((row) => row.id) };
    if (action === "merge-threads") payload.target = target;
    let res;
    try {
      res = await runAction(action, payload);
      if (res && res.needs_close) {
        if (!(await confirmCloseCodex({ wsl: sourceHome.startsWith("wsl:") }))) return;
        res = await runAction(action, { ...payload, close_codex: true });
      }
    } catch (e) {
      showToast(`${label}失败：${e.message || e}`, "error");
      return;
    }
    const ok = Boolean(res && res.success);
    const n = res ? res.moved ?? res.restored ?? rows.length : 0;
    showToast(ok ? `${label} ${n} 个对话，重新打开 Codex 后可见` : `${label}失败：${failureReason(res)}`, ok ? "success" : "error");
    if (ok) selected.clear();
    await loadWorkspace({ force: true });
    refreshStatus(store);
  }

  // ---- 正文 ----
  async function openDetail(c) {
    detailRow = c;
    markActive();
    await loadDetail();
  }

  async function loadDetail() {
    if (!detailRow) return;
    const c = detailRow;
    const seq = (detailSeq += 1);
    detailLoading = true;
    renderDetail();
    const homeId = c.home_id || "windows";
    let result;
    try {
      result = await runAction("read-conversation", { thread_id: c.id, source_home: homeId, include_tools: showTools });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive || seq !== detailSeq) return;
    detail = { ...result, home_id: homeId, home_label: c.home_label || homeLabel(homeId) };
    detailLoading = false;
    renderDetail();
  }

  function renderDetail() {
    if (!detailRow) {
      fill(detailPane, h("div", { class: "conv-pane-body" }, h("div", { class: "empty" }, "在左侧选择一个对话查看正文")));
      return;
    }
    if (detailLoading) {
      fill(detailPane, h("div", { class: "conv-pane-body" }, loadingSpinner("正在读取对话正文…")));
      return;
    }
    if (!detail || !detail.success) {
      fill(detailPane,
        h(
          "div",
          { class: "conv-pane-body" },
          h("div", { class: "status-line error" }, `读取正文失败：${detail?.error || "未知错误"}`),
          h("div", { class: "list-more" }, actionButton("重试", "btn-ghost btn-sm", loadDetail))
        )
      );
      return;
    }
    const t = asObject(detail.thread);
    const threadId = t.id || detailRow.id;
    const toolsBox = h("input", { type: "checkbox" });
    toolsBox.checked = showTools;
    toolsBox.addEventListener("change", () => {
      showTools = toolsBox.checked;
      loadDetail();
    });
    const metaParts = [
      detail.home_label || t.home_label || homeLabel(t.home_id),
      t.model_provider || detailRow.model_provider || "-",
      `${detail.message_count ?? (detail.messages || []).length} 条消息`,
      formatEpoch(t.updated_at || detailRow.updated_at),
    ].filter(Boolean);
    const head = h(
      "div",
      { class: "detail-head" },
      h("div", { class: "detail-title" }, t.title || detailRow.title || "(无标题)"),
      h("div", { class: "detail-meta" }, metaParts.join(" · ")),
      t.cwd || detailRow.cwd ? h("div", { class: "detail-meta mono", title: t.cwd || detailRow.cwd }, t.cwd || detailRow.cwd) : null,
      h(
        "div",
        { class: "detail-actions" },
        h(
          "button",
          {
            class: "btn btn-ghost btn-sm",
            type: "button",
            title: "复制后在终端运行即可在 Codex CLI 中继续这个对话",
            onClick: async () => {
              const command = `codex resume ${threadId}`;
              const ok = await copyText(command);
              showToast(ok ? `已复制：${command}` : "复制失败", ok ? "success" : "error");
            },
          },
          "复制恢复命令"
        ),
        actionButton("导出 Markdown", "btn-ghost btn-sm", () => exportRows([detailRow])),
        h("label", { class: "checkbox-control" }, toolsBox, h("span", {}, "显示工具调用"))
      )
    );
    const messages = (detail.messages || []).map(renderMessage);
    const body = h(
      "div",
      { class: "conv-pane-body" },
      h(
        "div",
        { class: "msg-list" },
        ...(messages.length ? messages : [h("div", { class: "empty compact" }, "这个对话没有可显示的消息")]),
        detail.truncated ? statusLine("正文较长，只显示了前面一部分。", "info") : null
      )
    );
    fill(detailPane, head, body);
  }

  renderHomeOptions(true);
  renderAll();
  renderDetail();
  loadWorkspace();

  return () => {
    alive = false;
    clearTimeout(searchTimer);
  };
}

// ==================== 渠道整合 ====================
function mountChannels(container, store) {
  let alive = true;
  let homes = Array.isArray(browseCache.homes) ? browseCache.homes : [];
  let sourceHome = "windows";
  let channelsData = null;
  let busy = false;

  const homeSel = h("select", { class: "input", "aria-label": "渠道所在环境" });
  const body = h("div", { class: "card-body" });

  fill(container,
    h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, "渠道整合"),
      h(
        "p",
        { class: "card-desc" },
        "Codex 按「渠道」（模型服务商）分开显示对话。切换过服务商后，旧渠道的对话在当前列表里看不到；把它们并入当前渠道即可重新看到。并入只修改对话的渠道标记，不改动内容，可以随时还原。"
      ),
      h(
        "div",
        { class: "card-actions" },
        homeSel,
        actionButton("刷新", "btn-ghost btn-sm", () => load({ force: true })),
        actionButton("重新检测环境", "btn-ghost btn-sm", () => load({ force: true, probe: true }))
      ),
      body
    )
  );

  const homeLabel = (homeId) => homeLabelFor(homes, homeId);

  function renderHomeOptions(loading = false) {
    fill(homeSel, ...homeOptions(homes, { allLabel: loading ? "正在检测本机环境…" : "全部本机环境（只读）" }));
    homeSel.value = sourceHome;
  }

  homeSel.addEventListener("change", () => {
    sourceHome = homeSel.value || "all";
    load();
  });

  async function load({ force = false, probe = false } = {}) {
    if (force || !browseCache.workspaceByHome.has(sourceHome)) {
      channelsData = null;
      renderHomeOptions(!homes.length);
      render();
    }
    const workspace = await fetchWorkspace(sourceHome, { force, probe });
    if (!alive) return;
    homes = Array.isArray(browseCache.homes) ? browseCache.homes : homes;
    channelsData =
      workspace?.channels && typeof workspace.channels === "object"
        ? workspace.channels
        : { success: false, error: workspace?.error || "读取渠道失败" };
    renderHomeOptions(false);
    render();
  }

  async function submit(action, opts, label, confirmOpts) {
    if (busy) return;
    if (sourceHome === "all" || channelsData?.write_supported === false) {
      showToast("并入或还原需要先选择单个 Windows 或 WSL 环境", "warning");
      return;
    }
    if (confirmOpts && !(await confirmDialog(confirmOpts))) return;
    const wsl = sourceHome.startsWith("wsl:");
    let closeCodex = false;
    if (channelsData?.codex_running) {
      if (!(await confirmCloseCodex({ wsl }))) return;
      closeCodex = true;
    }
    busy = true;
    render();
    let res;
    try {
      res = await runAction(action, { ...opts, source_home: sourceHome, close_codex: closeCodex });
      if (res && res.needs_close && !closeCodex) {
        if (!(await confirmCloseCodex({ wsl }))) return;
        res = await runAction(action, { ...opts, source_home: sourceHome, close_codex: true });
      }
      const ok = Boolean(res && res.success);
      showToast(ok ? `${label}完成，重新打开 Codex 后生效` : `${label}失败：${failureReason(res)}`, ok ? "success" : "error");
    } catch (e) {
      showToast(`${label}失败：${e.message || e}`, "error");
    } finally {
      busy = false;
      render();
    }
    await load({ force: true });
    refreshStatus(store);
  }

  function render() {
    if (!channelsData) {
      fill(body, loadingSpinner("正在读取渠道分布…"));
      return;
    }
    if (!channelsData.success) {
      fill(body,
        h("div", { class: "status-line error" }, `读取渠道失败：${channelsData.error || "未知错误"}`),
        actionButton("重试", "btn-ghost btn-sm", () => load({ force: true }))
      );
      return;
    }

    if (channelsData.home_groups) {
      const groups = channelsData.home_groups || [];
      fill(body,
        h("div", { class: "status-line info" }, "这里按环境分组只读展示。要并入或还原，请先在上方选择单个 Windows 或 WSL 环境。"),
        ...groups.map(renderGroup)
      );
      return;
    }

    const writable = channelsData.write_supported !== false && sourceHome !== "all";
    const cur = channelsData.current_provider || "(未知)";
    const running = Boolean(channelsData.codex_running);
    const runningKnown = channelsData.codex_running_known !== false;
    const merged = asObject(channelsData.merged);
    const byTarget = asObject(merged.by_target);

    const rows = (channelsData.channels || []).map((c) => {
      const isCur = c.provider === cur;
      const into = byTarget[c.provider] || 0;
      return h(
        "div",
        { class: "channel-row" },
        h(
          "div",
          { class: "channel-name" },
          h("span", {}, c.provider),
          isCur ? h("span", { class: "badge ok" }, "当前渠道") : null,
          into ? h("span", { class: "badge" }, `含并入 ${into}`) : null
        ),
        h(
          "div",
          { class: "channel-side" },
          h("span", { class: "muted" }, `${c.threads} 个对话`),
          isCur || !writable
            ? null
            : actionButton("并入当前渠道", "btn-primary btn-sm", () => submit("merge-channels", { sources: [c.provider] }, `并入 ${c.provider}`), { disabled: busy })
        )
      );
    });

    const origins = {};
    const breakdown = asObject(merged.origin_breakdown);
    for (const target of Object.keys(breakdown)) {
      for (const origin of Object.keys(asObject(breakdown[target]))) origins[origin] = (origins[origin] || 0) + breakdown[target][origin];
    }
    const originKeys = Object.keys(origins).sort();
    const otherCount = (channelsData.channels || []).filter((c) => c.provider !== cur).length;

    fill(body,
      ...[
        h(
          "div",
          { class: "channel-head" },
          h("span", {}, `${homeLabel(sourceHome)} 当前渠道：`, h("strong", {}, cur)),
          writable
            ? h("span", { class: `badge ${!runningKnown || running ? "warn" : "ok"}` }, runningKnown ? (running ? "Codex 运行中" : "Codex 未运行") : "操作时检查 Codex 状态")
            : h("span", { class: "badge warn" }, "只读")
        ),
        rows.length ? h("div", { class: "channel-list" }, ...rows) : h("div", { class: "empty" }, "暂无渠道数据"),
        writable && otherCount
          ? h(
              "div",
              { class: "card-actions" },
              actionButton(
                "全部并入当前渠道",
                "btn-primary btn-sm",
                () =>
                  submit("merge-channels", { all: true }, "全部并入", {
                    title: "全部并入当前渠道",
                    message: `将把其它 ${otherCount} 个渠道的对话全部并入「${cur}」。之后可以在下方按来源渠道还原。`,
                    confirmLabel: "全部并入",
                  }),
                { disabled: busy }
              )
            )
          : null,
        writable && merged.total
          ? h(
              "div",
              { class: "channel-group" },
              h("div", { class: "channel-head" }, h("strong", {}, "已并入的对话（可按来源还原）"), h("span", { class: "muted small" }, `共 ${merged.total} 个`)),
              ...originKeys.map((origin) =>
                h(
                  "div",
                  { class: "channel-row" },
                  h("div", { class: "channel-name" }, h("span", {}, origin), h("span", { class: "muted small" }, `${origins[origin]} 个对话`)),
                  actionButton(`还原到 ${origin}`, "btn-ghost btn-sm", () => submit("restore-channels", { sources: [origin] }, `还原 ${origin}`), { disabled: busy })
                )
              ),
              h(
                "div",
                { class: "card-actions" },
                actionButton(
                  "全部还原",
                  "btn-ghost btn-sm",
                  () =>
                    submit("restore-channels", { all: true }, "全部还原", {
                      title: "全部还原",
                      message: `将把 ${merged.total} 个已并入的对话全部还原到它们原来的渠道。`,
                      confirmLabel: "全部还原",
                    }),
                  { disabled: busy }
                )
              )
            )
          : null,
      ].filter(Boolean)
    );
  }

  function renderGroup(group) {
    if (!group || !group.success) {
      return h("div", { class: "channel-group" }, h("div", { class: "status-line error" }, `${group?.home_label || "本机环境"} 读取失败：${group?.error || "未知错误"}`));
    }
    const cur = group.current_provider || "(未知)";
    const rows = (group.channels || []).map((c) =>
      h(
        "div",
        { class: "channel-row" },
        h("div", { class: "channel-name" }, h("span", {}, c.provider), c.provider === cur ? h("span", { class: "badge ok" }, "当前渠道") : null),
        h("span", { class: "muted" }, `${c.threads} 个对话`)
      )
    );
    return h(
      "div",
      { class: "channel-group" },
      h("div", { class: "channel-head" }, h("strong", {}, group.home_label || group.home_id || "本机环境"), h("span", { class: "muted small" }, `当前渠道：${cur}`)),
      rows.length ? h("div", {}, ...rows) : h("div", { class: "empty compact" }, "暂无渠道数据")
    );
  }

  renderHomeOptions(!homes.length);
  render();
  load();

  return () => {
    alive = false;
  };
}
