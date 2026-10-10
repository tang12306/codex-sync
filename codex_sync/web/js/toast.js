// 浮动通知。用 textContent 写入消息，天然防 XSS。
// 错误停留更久且可手动关闭；鼠标悬停时暂停计时；可附带一个操作按钮。

const TYPE_CLASS = {
  success: "toast-success",
  error: "toast-error",
  warning: "toast-warning",
  info: "toast-info",
};

const DEFAULT_DURATION = { success: 3500, info: 3500, warning: 6000, error: 9000 };
const MAX_TOASTS = 5;

// showToast(message, type, { action: { label, onClick }, duration }) → 返回关闭函数
export function showToast(message, type = "info", options = {}) {
  const container = document.getElementById("toast-root");
  if (!container) return () => {};
  const { action, duration } = options || {};

  const toast = document.createElement("div");
  toast.className = `toast ${TYPE_CLASS[type] || TYPE_CLASS.info}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");

  const msg = document.createElement("span");
  msg.className = "toast-msg";
  msg.textContent = String(message);
  toast.append(msg);

  let timer = null;
  let remaining = duration ?? DEFAULT_DURATION[type] ?? 3500;
  let startedAt = 0;
  let closed = false;

  const dismiss = () => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    toast.classList.add("toast-leaving");
    setTimeout(() => toast.remove(), 220);
  };

  if (action && action.label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", () => {
      dismiss();
      if (typeof action.onClick === "function") action.onClick();
    });
    toast.append(btn);
  }

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "toast-close";
  closeBtn.setAttribute("aria-label", "关闭通知");
  closeBtn.textContent = "✕";
  closeBtn.addEventListener("click", dismiss);
  toast.append(closeBtn);

  const start = () => {
    if (closed) return;
    startedAt = Date.now();
    timer = setTimeout(dismiss, Math.max(remaining, 1200));
  };
  const pause = () => {
    clearTimeout(timer);
    remaining -= Date.now() - startedAt;
  };
  toast.addEventListener("mouseenter", pause);
  toast.addEventListener("mouseleave", start);

  container.appendChild(toast);
  while (container.children.length > MAX_TOASTS) container.firstElementChild.remove();
  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  start();
  return dismiss;
}
