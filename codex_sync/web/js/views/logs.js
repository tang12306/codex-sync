// 全局日志面板：常驻挂在 #logs-root，由 store.logs.panelOpen 控制滑出。
// 数据来自 status.logs（后端每次 status 带最近若干条），本模块只做过滤/搜索/渲染。
import { h, asObject } from "../dom.js";

const FILTERS = [
  ["all", "全部"],
  ["info", "Info"],
  ["ok", "成功"],
  ["daemon", "Daemon"],
  ["error", "错误"],
];

export function mount(root, store) {
  const patchLogs = (patch) => store.setState({ logs: { ...store.getState().logs, ...patch } });

  const body = h("div", { class: "logs-body" });

  const search = h("input", { class: "input", type: "text", placeholder: "过滤关键字…" });
  search.addEventListener("input", () => patchLogs({ search: search.value }));

  const autoScroll = h("input", { type: "checkbox" });
  autoScroll.checked = store.getState().logs.autoScroll;
  autoScroll.addEventListener("change", () => patchLogs({ autoScroll: autoScroll.checked }));

  const chips = FILTERS.map(([key, label]) => {
    const chip = h("button", { class: "chip", dataset: { filter: key } }, label);
    chip.addEventListener("click", () => patchLogs({ filter: key }));
    return chip;
  });

  const closeBtn = h("button", { class: "icon-btn", title: "关闭", "aria-label": "关闭日志面板" }, "✕");
  closeBtn.addEventListener("click", () => patchLogs({ panelOpen: false }));

  root.replaceChildren(
    h("div", { class: "logs-head" }, h("h3", {}, "事件日志"), closeBtn),
    h(
      "div",
      { class: "logs-toolbar" },
      h("div", { class: "logs-filters" }, ...chips),
      h(
        "div",
        { class: "logs-controls" },
        search,
        h("label", { class: "checkbox-control" }, autoScroll, h("span", {}, "自动滚动"))
      )
    ),
    body
  );

  const renderList = () => {
    const { status, logs } = store.getState();
    const all = Array.isArray(status?.logs) ? status.logs : [];
    const rows = applyFilter(all, logs.filter, logs.search);
    body.replaceChildren(
      ...(rows.length ? rows.map(logRow) : [h("div", { class: "empty" }, "暂无匹配日志")])
    );
    for (const chip of chips) chip.classList.toggle("active", chip.dataset.filter === logs.filter);
    if (logs.autoScroll) body.scrollTop = 0; // 最新在顶部
  };

  const renderOpen = () => {
    const open = store.getState().logs.panelOpen;
    root.classList.toggle("open", open);
    root.setAttribute("aria-hidden", open ? "false" : "true");
  };

  renderList();
  renderOpen();
  const u1 = store.select((s) => s.status?.logs, renderList);
  const u2 = store.select(
    (s) => s.logs,
    () => {
      renderList();
      renderOpen();
    }
  );
  return () => {
    u1();
    u2();
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
      return (
        String(o.message || "").toLowerCase().includes(q) ||
        String(o.level || "").toLowerCase().includes(q)
      );
    });
  }
  return out.slice(-200).reverse();
}

function logRow(item) {
  const o = asObject(item);
  const row = h(
    "div",
    { class: "log-row", dataset: { level: o.level || "info" } },
    h(
      "div",
      { class: "log-meta" },
      h("span", {}, o.time || ""),
      h("span", { class: "log-level" }, o.level || "")
    ),
    h("div", { class: "log-msg" }, o.message || "")
  );
  if (o.data != null && o.data !== "") {
    row.append(
      h("div", { class: "log-data" }, typeof o.data === "string" ? o.data : JSON.stringify(o.data, null, 2))
    );
  }
  return row;
}
