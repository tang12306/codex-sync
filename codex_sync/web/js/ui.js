// 共享 UI：统一动作执行器、按钮包装、二级页签。供各视图复用。
import { h } from "./dom.js";
import { runAction } from "./api.js";
import { refreshStatus } from "./poller.js";
import { showToast } from "./toast.js";
import { navigate, setSubRoute } from "./router.js";
import { formatDateTime, failureReason, isFailed } from "./format.js";

export { formatDateTime };

// 统一动作执行器：runAction → toast → 可选 refreshStatus。
// okMsg: 字符串，或按结果生成文案的函数；toast: 传给 showToast 的额外选项（如成功后的操作按钮）。
export function makeRun(store) {
  return async function run(name, opts = {}) {
    const { payload = {}, okMsg, errMsg, refresh = true, label, toast } = opts;
    try {
      const result = await runAction(name, payload);
      const failed = isFailed(result);
      const success = typeof okMsg === "function" ? okMsg(result) : okMsg;
      showToast(
        failed ? `${errMsg || `${label || name}失败`}：${failureReason(result)}` : success || `${label || name}已完成`,
        failed ? "error" : "success",
        failed ? {} : toast
      );
      if (refresh) await refreshStatus(store);
      return result;
    } catch (e) {
      showToast(`${errMsg || `${label || name}失败`}：${e.message || e}`, "error");
      throw e;
    }
  };
}

// 包一层按钮：点击期间禁用并显示转圈，吞掉异常（已由 run 提示）。
export function actionButton(label, cls, handler, attrs = {}) {
  const b = h("button", { class: `btn ${cls}`, type: "button", ...attrs }, label);
  b.addEventListener("click", async () => {
    b.disabled = true;
    b.classList.add("is-busy");
    b.setAttribute("aria-busy", "true");
    try {
      await handler();
    } catch {
      /* 已提示 */
    } finally {
      b.disabled = false;
      b.classList.remove("is-busy");
      b.removeAttribute("aria-busy");
    }
  });
  return b;
}

// 二级页签：tablist 语义 + 方向键切换；切换时写入地址栏（#/route/sub），刷新后停留在同一页签。
// tabs: [{key, label}]；第一个页签是默认页，不写入地址栏。
export function createSubTabs(tabs, { initial, onChange, label = "页面分区" } = {}) {
  const keys = tabs.map((tab) => tab.key);
  let active = keys.includes(initial) ? initial : keys[0];

  const buttons = tabs.map((tab) =>
    h("button", { class: "sub-tab-btn", type: "button", role: "tab", dataset: { key: tab.key }, onClick: () => select(tab.key, true) }, tab.label)
  );
  const el = h("div", { class: "sub-tabs", role: "tablist", "aria-label": label }, ...buttons);

  el.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1 };
    let index = keys.indexOf(active);
    if (event.key in moves) index = (index + moves[event.key] + keys.length) % keys.length;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = keys.length - 1;
    else return;
    event.preventDefault();
    select(keys[index], true);
    buttons[index].focus();
  });

  function paint() {
    for (const button of buttons) {
      const on = button.dataset.key === active;
      button.classList.toggle("active", on);
      button.setAttribute("aria-selected", on ? "true" : "false");
      button.tabIndex = on ? 0 : -1;
    }
  }

  function select(key, fromUser) {
    if (!keys.includes(key)) return;
    if (fromUser && key === active) return;
    active = key;
    paint();
    setSubRoute(key === keys[0] ? "" : key);
    if (onChange) onChange(key);
  }

  paint();
  return {
    el,
    get active() {
      return active;
    },
    select: (key) => select(key, false),
  };
}

// 开关（复选框外观为拨动开关）。
export function switchControl({ checked = false, disabled = false, label = "", onChange } = {}) {
  const input = h("input", { type: "checkbox", role: "switch", "aria-label": label || null });
  input.checked = Boolean(checked);
  input.disabled = Boolean(disabled);
  if (onChange) input.addEventListener("change", () => onChange(input.checked, input));
  const el = h("label", { class: "switch" }, input, h("span", { class: "slider", "aria-hidden": "true" }));
  return { el, input };
}

// 带说明文字的开关行。
export function switchRow(title, desc, control) {
  return h("div", { class: "switch-row" }, h("div", { class: "switch-text" }, h("b", {}, title), desc ? h("span", {}, desc) : null), control);
}

// 还没填服务器地址时的统一提示，带跳转到设置页的链接。
export function serverMissingNotice(text = "还没有配置同步服务器。") {
  return h(
    "div",
    { class: "status-line info" },
    `${text} `,
    h("button", { class: "link-btn", type: "button", onClick: () => navigate("settings", "server") }, "去配置服务器")
  );
}
