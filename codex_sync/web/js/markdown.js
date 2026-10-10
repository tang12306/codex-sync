// 轻量 Markdown：只覆盖对话里常见的语法（代码块、标题、列表、引用、行内代码、粗体、链接）。
// 全部用 DOM 节点构建，不拼 innerHTML；链接只显示目标、不跳转，避免在桌面窗口里离开控制台。
import { h } from "./dom.js";
import { ROLE_LABEL, copyText } from "./format.js";
import { showToast } from "./toast.js";

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})\s*([\w.+#-]*).*$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE_RE = /^\s{0,3}>/;
const UL_RE = /^\s*[-*+]\s+(.*)$/;
const OL_RE = /^\s*(\d{1,9})[.)]\s+(.*)$/;
const INLINE_RE = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\*\*(?=\S)([\s\S]+?)\*\*|\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

// 一条对话消息：角色标题 + 复制按钮 + 正文（工具调用/输出按原样等宽显示）。
export function renderMessage(message) {
  const role = message?.role || "unknown";
  const text = message?.text || "";
  const isTool = role === "tool_call" || role === "tool_output";
  const copyBtn = h(
    "button",
    {
      class: "btn btn-ghost btn-sm msg-copy",
      type: "button",
      onClick: async () => {
        const ok = await copyText(text);
        showToast(ok ? "消息已复制" : "复制失败", ok ? "success" : "error");
      },
    },
    "复制"
  );
  return h(
    "div",
    { class: `msg role-${role}` },
    h("div", { class: "msg-head" }, h("span", {}, ROLE_LABEL[role] || role), copyBtn),
    isTool ? h("pre", { class: "msg-tool" }, text) : renderMarkdown(text)
  );
}

export function renderMarkdown(text) {
  const root = h("div", { class: "md" });
  appendBlocks(root, String(text ?? "").replace(/\r\n?/g, "\n").split("\n"));
  return root;
}

function appendBlocks(root, lines) {
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) root.append(paragraph(para));
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(FENCE_RE);
    if (fence) {
      flush();
      const marker = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // 跳过结束标记（未闭合时等于读到末尾）
      root.append(codeBlock(body.join("\n"), fence[2]));
      continue;
    }

    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }

    const heading = line.match(HEADING_RE);
    if (heading) {
      flush();
      const level = heading[1].length;
      root.append(h("div", { class: `md-h md-h${level}`, role: "heading", "aria-level": String(Math.min(level + 2, 6)) }, ...inline(heading[2])));
      i += 1;
      continue;
    }

    if (HR_RE.test(line)) {
      flush();
      root.append(h("hr"));
      i += 1;
      continue;
    }

    if (QUOTE_RE.test(line)) {
      flush();
      const quoted = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i += 1;
      }
      const block = h("blockquote");
      appendBlocks(block, quoted);
      root.append(block);
      continue;
    }

    const ul = line.match(UL_RE);
    const ol = ul ? null : line.match(OL_RE);
    if (ul || ol) {
      flush();
      const ordered = Boolean(ol);
      const list = h(ordered ? "ol" : "ul");
      if (ordered && Number(ol[1]) !== 1) list.setAttribute("start", String(Number(ol[1])));
      while (i < lines.length) {
        const item = ordered ? lines[i].match(OL_RE) : lines[i].match(UL_RE);
        if (item) {
          list.append(h("li", {}, ...inline(ordered ? item[2] : item[1])));
          i += 1;
          continue;
        }
        // 缩进的续行并入上一项（嵌套列表也按续行显示，保证内容不丢）
        if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && list.lastChild) {
          list.lastChild.append(h("br"), ...inline(lines[i].trim()));
          i += 1;
          continue;
        }
        break;
      }
      root.append(list);
      continue;
    }

    para.push(line);
    i += 1;
  }
  flush();
}

function paragraph(lines) {
  const p = h("p");
  lines.forEach((line, index) => {
    if (index) p.append(h("br"));
    p.append(...inline(line));
  });
  return p;
}

function inline(text) {
  const out = [];
  let last = 0;
  for (const match of text.matchAll(INLINE_RE)) {
    if (match.index > last) out.push(text.slice(last, match.index));
    if (match[1]) {
      out.push(h("code", { class: "md-code" }, match[2]));
    } else if (match[3]) {
      out.push(h("strong", {}, ...inline(match[3])));
    } else {
      out.push(h("span", { class: "md-link", title: match[5] }, match[4]));
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.map((part) => (typeof part === "string" ? document.createTextNode(part) : part));
}

function codeBlock(code, lang) {
  const copyBtn = h(
    "button",
    {
      class: "btn btn-ghost btn-sm",
      type: "button",
      onClick: async () => {
        const ok = await copyText(code);
        showToast(ok ? "代码已复制" : "复制失败，请手动选择复制", ok ? "success" : "error");
      },
    },
    "复制"
  );
  return h(
    "div",
    { class: "md-pre-wrap" },
    h("div", { class: "md-pre-head" }, h("span", {}, lang || "代码"), copyBtn),
    h("pre", { class: "md-pre" }, h("code", {}, code))
  );
}
