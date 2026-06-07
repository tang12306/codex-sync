// 浮动通知。用 textContent 写入消息，天然防 XSS。

const TYPE_CLASS = {
  success: "toast-success",
  error: "toast-error",
  warning: "toast-warning",
  info: "toast-info",
};

export function showToast(message, type = "info") {
  const container = document.getElementById("toast-root");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast ${TYPE_CLASS[type] || TYPE_CLASS.info}`;
  toast.textContent = String(message);
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => {
    toast.classList.add("toast-leaving");
    setTimeout(() => toast.remove(), 220);
  }, 2600);
}
