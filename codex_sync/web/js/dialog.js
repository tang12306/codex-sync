// 应用内对话框，替代 window.confirm：支持多行说明、要点列表和危险操作样式。
// 键盘可用：焦点限制在对话框内、Esc 取消、关闭后焦点回到原来的按钮。
import { h } from "./dom.js";
import { CODEX_RUNNING_CONFIRM, WSL_RUNNING_CONFIRM } from "./format.js";

let openCount = 0;
let idSeq = 0;

export function isDialogOpen() {
  return openCount > 0;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// actions: [{label, value, class, autofocus}]，点击后 Promise 以该 value 结束；
// 点遮罩、右上角关闭或按 Esc 以 dismissValue 结束。
export function showDialog({ title, body, actions = [], wide = false, danger = false, dismissValue = null } = {}) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const titleId = `dialog-title-${(idSeq += 1)}`;
    let done = false;

    const close = (value) => {
      if (done) return;
      done = true;
      openCount -= 1;
      backdrop.remove();
      if (previous instanceof HTMLElement && document.contains(previous)) previous.focus();
      resolve(value);
    };

    const buttons = actions.map((action) =>
      h("button", { class: `btn ${action.class || "btn-ghost"}`, type: "button", onClick: () => close(action.value) }, action.label)
    );
    const closeBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "关闭", onClick: () => close(dismissValue) }, "✕");
    const dialog = h(
      "div",
      { class: `dialog${wide ? " wide" : ""}`, role: danger ? "alertdialog" : "dialog", "aria-modal": "true", "aria-labelledby": titleId },
      h("div", { class: `dialog-head${danger ? " danger" : ""}` }, h("h2", { id: titleId }, title || "请确认"), closeBtn),
      h("div", { class: "dialog-body" }, body),
      buttons.length ? h("div", { class: "dialog-foot" }, ...buttons) : null
    );
    const backdrop = h("div", { class: "dialog-backdrop" }, dialog);

    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) close(dismissValue);
    });
    backdrop.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close(dismissValue);
        return;
      }
      if (event.key !== "Tab") return;
      const items = [...dialog.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    document.body.append(backdrop);
    openCount += 1;
    const preferred = buttons[actions.findIndex((action) => action.autofocus)];
    (preferred || buttons[buttons.length - 1] || closeBtn).focus();
  });
}

// 确认框，返回 Promise<boolean>。危险操作默认聚焦「取消」，防止回车误触。
export function confirmDialog({ title = "请确认", message = "", details = [], confirmLabel = "确认", cancelLabel = "取消", danger = false } = {}) {
  const list = (details || []).filter(Boolean);
  const body = h(
    "div",
    {},
    message ? h("p", { class: "dialog-message" }, message) : null,
    list.length ? h("ul", { class: "dialog-details" }, ...list.map((item) => h("li", {}, item))) : null
  );
  return showDialog({
    title,
    body,
    danger,
    dismissValue: false,
    actions: [
      { label: cancelLabel, value: false, class: "btn-ghost", autofocus: danger },
      { label: confirmLabel, value: true, class: danger ? "btn-danger" : "btn-primary", autofocus: !danger },
    ],
  }).then(Boolean);
}

// 后端返回 needs_close / codex_running 时使用的统一确认。
export function confirmCloseCodex({ wsl = false } = {}) {
  return confirmDialog({
    title: wsl ? "需要关闭 WSL 里的 Codex" : "需要关闭 Codex",
    message: wsl ? WSL_RUNNING_CONFIRM : CODEX_RUNNING_CONFIRM,
    confirmLabel: "关闭 Codex 并继续",
  });
}
