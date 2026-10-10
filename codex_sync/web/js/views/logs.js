// 全局日志面板：常驻挂在 #logs-root，由 store.logs.panelOpen 控制滑出。
// 数据来自 status.logs（后端每次 status 带最近若干条），本模块只做过滤/搜索/渲染。
import { h, asObject, fill } from "../dom.js";
import { formatDateTime } from "../format.js";

// 与后端 runtime.log() 使用的级别对应
const FILTERS = [
  ["all", "全部"],
  ["ok", "成功"],
  ["warn", "警告"],
  ["error", "错误"],
  ["run", "操作"],
  ["daemon", "后台"],
  ["http", "HTTP"],
];

const LEVEL_LABEL = { ok: "成功", warn: "警告", error: "错误", run: "操作", daemon: "后台", http: "HTTP", warmup: "预热", info: "信息" };

export function mount(root, store) {
  const patchLogs = (patch) => store.setState({ logs: { ...store.getState().logs, ...patch } });

  const body = h("div", { class: "logs-body", role: "log" });

  const search = h("input", { class: "input", type: "search", placeholder: "搜索日志内容…", "aria-label": "搜索日志" });
  search.value = store.getState().logs.search || "";
  search.addEventListener("input", () => patchLogs({ search: search.value }));

  const autoScroll = h("input", { type: "checkbox" });
  autoScroll.checked = store.getState().logs.autoScroll;
  autoScroll.addEventListener("change", () => patchLogs({ autoScroll: autoScroll.checked }));

  const chips = FILTERS.map(([key, label]) =>
    h("button", { class: "chip", type: "button", dataset: { filter: key }, onClick: () => patchLogs({ filter: key }) }, label)
  );

  const closeBtn = h("button", { class: "icon-btn", type: "button", title: "关闭", "aria-label": "关闭日志面板", onClick: () => patchLogs({ panelOpen: false }) }, "✕");

  fill(root,
    h("div", { class: "logs-head" }, h("h3", {}, "运行日志"), closeBtn),
    h(
      "div",
      { class: "logs-toolbar" },
      h("div", { class: "logs-filters", role: "group", "aria-label": "按级别筛选" }, ...chips),
      h("div", { class: "logs-controls" }, search, h("label", { class: "checkbox-control" }, autoScroll, h("span", {}, "新日志置顶")))
    ),
    body
  );

  const renderList = () => {
    const { status, logs } = store.getState();
    const all = Array.isArray(status?.logs) ? status.logs : [];
    const rows = applyFilter(all, logs.filter, logs.search);
    fill(body, ...(rows.length ? rows.map(logRow) : [h("div", { class: "empty" }, all.length ? "没有匹配的日志" : "暂无日志")]));
    for (const chip of chips) {
      const on = chip.dataset.filter === logs.filter;
      chip.classList.toggle("active", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (logs.autoScroll) body.scrollTop = 0; // 最新在顶部
  };

  const renderOpen = () => {
    const open = store.getState().logs.panelOpen;
    root.classList.toggle("open", open);
    root.setAttribute("aria-hidden", open ? "false" : "true");
    if (open) closeBtn.focus({ preventScroll: true });
  };

  renderList();
  renderOpen();
  const u1 = store.select((s) => s.status?.logs, renderList);
  const u2 = store.select((s) => s.logs, renderList);
  const u3 = store.select((s) => s.logs.panelOpen, renderOpen);
  return () => {
    u1();
    u2();
    u3();
  };
}

function applyFilter(logs, filter, search) {
  let out = logs;
  if (filter && filter !== "all") {
    out = out.filter((i) => (asObject(i).level || "info") === filter);
  }
  const q = (search || "").toLowerCase().trim();
  if (q) {
    out = out.filter((i) => {
      const o = asObject(i);
      const data = o.data == null ? "" : typeof o.data === "string" ? o.data : JSON.stringify(o.data);
      return `${o.message || ""}\n${o.level || ""}\n${data}`.toLowerCase().includes(q);
    });
  }
  return out.slice(-200).reverse();
}

function logRow(item) {
  const o = asObject(item);
  const level = o.level || "info";
  const row = h(
    "div",
    { class: "log-row", dataset: { level } },
    h("div", { class: "log-meta" }, h("span", {}, o.time ? formatDateTime(o.time) : ""), h("span", { class: "log-level" }, LEVEL_LABEL[level] || level)),
    h("div", { class: "log-msg" }, o.message || "")
  );
  if (o.data != null && o.data !== "") {
    row.append(h("div", { class: "log-data" }, typeof o.data === "string" ? o.data : JSON.stringify(o.data, null, 2)));
  }
  return row;
}
