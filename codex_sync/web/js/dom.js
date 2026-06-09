// DOM 与纯函数工具，所有视图共用。
// h() 用 createElement + textContent/append 构建节点，天然防 XSS；
// 只有显式传 html 属性、或调用方手写 innerHTML 时，才需要自行 escapeHtml。

export const $ = (id) => document.getElementById(id);

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// h("div", {class, dataset, style, text, html, onClick, ...attrs}, ...children)
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class" || key === "className") {
      node.className = value;
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else if (key === "text") {
      node.textContent = value;
    } else if (key === "html") {
      node.innerHTML = value; // 调用方负责转义
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      node.setAttribute(key, value);
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// 用一组节点替换 root 的全部内容。
export function mount(root, ...nodes) {
  if (!root) return;
  root.replaceChildren(...nodes.flat(Infinity).filter((n) => n != null && n !== false));
}

export function clear(el) {
  if (el) el.replaceChildren();
}

export function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function shortId(value) {
  const text = value ? String(value) : "";
  if (!text) return "-";
  return text.length > 16 ? `${text.slice(0, 12)}…` : text;
}
