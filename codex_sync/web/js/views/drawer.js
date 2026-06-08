// 远端快照抽屉：详情 / 接续 / 还原预览 / 还原（二次确认）。常驻挂在 #drawer-root，由 store.drawer 驱动。
// 安全约束：还原必须先 preview-restore 成功 → 勾选 ack → restore-snapshot 同时传 snapshot_id 与 confirm_snapshot_id（且相等）。
import { h, asObject, shortId, formatDiff } from "../dom.js";
import { runAction } from "../api.js";
import { refreshStatus } from "../poller.js";
import { showToast } from "../toast.js";
import { formatDateTime } from "../ui.js";

function patch(store, p) {
  store.setState({ drawer: { ...store.getState().drawer, ...p } });
}
function currentSnapshot(d) {
  return d.detail?.snapshot || d.summary || {};
}
function currentId(d) {
  return currentSnapshot(d).id || d.summary?.id || "";
}

// 从快照列表打开抽屉。intent: detail | resume | restore。
export function openSnapshot(store, summary, intent = "detail") {
  store.setState({
    drawer: {
      open: true,
      summary: summary || null,
      detail: null,
      preview: null,
      resume: null,
      status: summary?.id ? "正在加载快照详情…" : "未选择快照",
      statusType: "info",
      busy: false,
    },
  });
  const id = summary?.id;
  if (!id) return;
  loadDetail(store, id).then(() => {
    if (intent === "resume") genResume(store, id);
    else if (intent === "restore") previewRestore(store, id);
  });
}

async function loadDetail(store, id) {
  patch(store, { busy: true, status: "正在拉取快照详情…", statusType: "info" });
  try {
    const detail = await runAction("snapshot-detail", { snapshot_id: id });
    patch(store, {
      detail: detail.success ? detail : null,
      busy: false,
      status: detail.success ? "快照详情已加载。" : `详情加载失败：${detail.error || "未知错误"}`,
      statusType: detail.success ? "info" : "error",
    });
  } catch (e) {
    patch(store, { busy: false, status: `详情加载失败：${e.message || e}`, statusType: "error" });
  }
}

async function genResume(store, id) {
  if (!id) return;
  patch(store, { busy: true, status: "正在生成远端接续 Prompt…", statusType: "info" });
  try {
    const r = await runAction("remote-resume", { snapshot_id: id });
    patch(store, {
      resume: r,
      busy: false,
      status: r.success ? "接续 Prompt 已生成。" : `接续生成失败：${r.error || "未知错误"}`,
      statusType: r.success ? "info" : "error",
    });
    showToast(r.success ? "接续 Prompt 已生成" : "接续生成失败", r.success ? "success" : "error");
  } catch (e) {
    patch(store, { busy: false, status: `接续生成失败：${e.message || e}`, statusType: "error" });
  }
}

async function previewRestore(store, id) {
  if (!id) return;
  patch(store, { busy: true, preview: null, status: "正在生成还原差异预览…", statusType: "info" });
  try {
    const preview = await runAction("preview-restore", { snapshot_id: id });
    const changed = Array.isArray(preview.files) ? preview.files.filter((f) => f.changed).length : 0;
    patch(store, {
      preview,
      busy: false,
      status: preview.success
        ? `还原预览已生成，${changed} 个文件将变更。`
        : `还原预览失败：${preview.error || "未知错误"}`,
      statusType: preview.success ? "info" : "error",
    });
  } catch (e) {
    patch(store, { busy: false, status: `还原预览失败：${e.message || e}`, statusType: "error" });
  }
}

async function confirmRestore(store, id, ackChecked) {
  const d = store.getState().drawer;
  if (!id || d.preview?.success !== true || !ackChecked) {
    showToast("请先生成差异预览并勾选确认项", "warning");
    return;
  }
  patch(store, { busy: true, status: "正在执行还原，操作前会先创建灾难备份…", statusType: "info" });
  try {
    // 必须同时传 snapshot_id 与 confirm_snapshot_id（后端要求两者相等才执行）。
    const r = await runAction("restore-snapshot", { snapshot_id: id, confirm_snapshot_id: id });
    patch(store, {
      busy: false,
      status: r.success ? "配置还原完成，灾难备份已生成。" : `还原失败：${r.error || "未知错误"}`,
      statusType: r.success ? "info" : "error",
    });
    showToast(r.success ? "配置还原成功" : "配置还原失败", r.success ? "success" : "error");
    if (r.success) await refreshStatus(store);
  } catch (e) {
    patch(store, { busy: false, status: `还原失败：${e.message || e}`, statusType: "error" });
    showToast(String(e.message || e), "error");
  }
}

export function mount(root, store) {
  const backdrop = h("div", { class: "drawer-backdrop" });
  backdrop.addEventListener("click", () => patch(store, { open: false }));

  const title = h("h3", {}, "快照详情");
  const subtitle = h("p", {}, "选择一个云端快照查看状态、生成接续或预览还原。");
  const closeBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "关闭" }, "✕");
  closeBtn.addEventListener("click", () => patch(store, { open: false }));

  const statusLine = h("div", { class: "status-line" }, "等待加载");
  const body = h("div", { class: "drawer-body" });

  const ack = h("input", { type: "checkbox" });
  ack.addEventListener("change", updateFooter);
  const resumeBtn = h("button", { class: "btn btn-ghost", type: "button" }, "生成接续 Prompt");
  const previewBtn = h("button", { class: "btn btn-ghost", type: "button" }, "预览还原差异");
  const confirmBtn = h("button", { class: "btn btn-danger", type: "button" }, "确认还原");
  resumeBtn.addEventListener("click", () => genResume(store, currentId(store.getState().drawer)));
  previewBtn.addEventListener("click", () => previewRestore(store, currentId(store.getState().drawer)));
  confirmBtn.addEventListener("click", () => confirmRestore(store, currentId(store.getState().drawer), ack.checked));

  const footer = h(
    "div",
    { class: "drawer-foot" },
    h("label", { class: "ack" }, ack, h("span", {}, "我已检查差异，允许覆盖当前 Codex 配置")),
    h("div", { class: "card-actions" }, resumeBtn, previewBtn, confirmBtn)
  );

  const panel = h(
    "aside",
    { class: "drawer-panel", role: "dialog", "aria-modal": "true" },
    h(
      "div",
      { class: "drawer-head" },
      h("div", {}, h("div", { class: "drawer-eyebrow" }, "Remote Snapshot"), title, subtitle),
      closeBtn
    ),
    body,
    footer
  );
  root.replaceChildren(backdrop, panel);

  function updateFooter() {
    const d = store.getState().drawer;
    const id = currentId(d);
    const previewReady = d.preview?.success === true;
    resumeBtn.disabled = !id || d.busy;
    previewBtn.disabled = !id || d.busy;
    ack.disabled = !previewReady || d.busy;
    if (!previewReady) ack.checked = false;
    confirmBtn.disabled = !previewReady || !ack.checked || d.busy;
  }

  function renderBody() {
    const d = store.getState().drawer;
    const snap = currentSnapshot(d);
    const id = currentId(d);
    const createdAt = snap.created_at || d.summary?.created_at || "";
    title.textContent = id ? `快照 ${shortId(id)}` : "快照详情";
    subtitle.textContent = `${snap.device_id || d.summary?.device_id || "-"} · ${formatDateTime(createdAt)}`;
    statusLine.textContent = d.status || "等待加载";
    statusLine.className = `status-line ${d.statusType === "error" ? "error" : ""}`;
    body.replaceChildren(
      statusLine,
      summarySection(snap),
      configSection(snap),
      eventsSection(snap),
      previewSection(d),
      resumeSection(d)
    );
  }

  function renderOpen() {
    const open = store.getState().drawer.open;
    root.classList.toggle("open", open);
    root.setAttribute("aria-hidden", open ? "false" : "true");
  }

  const render = () => {
    renderBody();
    updateFooter();
    renderOpen();
  };
  render();
  const unsub = store.select((s) => s.drawer, render);
  return () => unsub();
}

function section(name, children) {
  return h("div", { class: "drawer-section" }, h("h4", {}, name), ...(Array.isArray(children) ? children : [children]));
}

function summarySection(snap) {
  const git = asObject(snap.git);
  const codex = asObject(snap.codex);
  const configFiles = asObject(codex.config_files);
  const events = Array.isArray(snap.recent_events) ? snap.recent_events : [];
  const items = [
    ["Snapshot ID", snap.id || "-", true],
    ["Device", snap.device_id || "-", false],
    ["Created", formatDateTime(snap.created_at), true],
    ["CWD", snap.cwd || "-", false],
    ["Git", git.is_repo ? `${git.branch || "detached"} · ${git.dirty ? "dirty" : "clean"}` : "非 Git 仓库", false],
    ["Commit", git.commit ? shortId(git.commit) : "-", true],
    ["配置文件", `${Object.keys(configFiles).length} 个`, false],
    ["最近事件", `${events.length} 条`, false],
  ];
  return h(
    "div",
    { class: "summary-grid" },
    ...items.map(([k, v, mono]) =>
      h("div", { class: "summary-item" }, h("div", { class: "k" }, k), h("div", { class: `v ${mono ? "mono" : ""}` }, v))
    )
  );
}

function configSection(snap) {
  const codex = asObject(snap.codex);
  const files = Object.entries(asObject(codex.config_files));
  const inner = files.length
    ? files.map(([name, metaV]) => {
        const meta = asObject(metaV);
        const item = h(
          "div",
          { class: "list-item" },
          h(
            "div",
            { class: "list-item-head" },
            h("span", { class: "list-item-name" }, name),
            h("span", { class: "badge" }, `${meta.bytes ?? "?"} bytes`)
          ),
          h("div", { class: "list-item-meta" }, meta.sha256 ? `sha256=${String(meta.sha256).slice(0, 16)}…` : "sha256=-")
        );
        if (meta.redacted_preview) item.append(h("pre", { class: "diff" }, meta.redacted_preview));
        return item;
      })
    : [h("div", { class: "empty" }, "无配置文件元数据（默认只保留脱敏预览与校验信息）。")];
  return section("01 / 配置文件", inner);
}

function eventsSection(snap) {
  const events = Array.isArray(snap.recent_events) ? snap.recent_events.slice(-10).reverse() : [];
  const inner = events.length
    ? events.map((evV) => {
        const ev = asObject(evV);
        return h(
          "div",
          { class: "list-item" },
          h(
            "div",
            { class: "list-item-head" },
            h("span", { class: "list-item-name" }, ev.event || ev.type || "-"),
            h("span", { class: "list-item-meta", title: ev.created_at ? `UTC: ${ev.created_at}` : "" }, formatDateTime(ev.created_at))
          ),
          h("div", { class: "list-item-meta" }, ev.cwd ? `cwd=${ev.cwd}` : "cwd=-")
        );
      })
    : [h("div", { class: "empty" }, "无最近事件记录。")];
  return section("02 / 最近事件", inner);
}

function previewSection(d) {
  const preview = d.preview;
  let inner;
  if (!preview) {
    inner = [h("div", { class: "empty" }, "点击「预览还原差异」后，这里展示将被覆盖的本地配置。")];
  } else if (!preview.success) {
    inner = [h("div", { class: "empty" }, `还原预览失败：${preview.error || "未知错误"}`)];
  } else {
    const files = Array.isArray(preview.files) ? preview.files : [];
    const skipped = Array.isArray(preview.skipped) ? preview.skipped : [];
    inner = [];
    if (skipped.length) inner.push(h("div", { class: "empty" }, `已跳过：${skipped.join(", ")}`));
    if (!files.length) inner.push(h("div", { class: "empty" }, "快照中没有可还原的允许文件。"));
    for (const fileV of files) {
      const file = asObject(fileV);
      const changed = Boolean(file.changed);
      const item = h(
        "div",
        { class: "list-item" },
        h(
          "div",
          { class: "list-item-head" },
          h("span", { class: "list-item-name" }, file.name || "-"),
          h("span", { class: `badge ${changed ? "changed" : "clean"}` }, changed ? "将变更" : "无变化")
        ),
        h(
          "div",
          { class: "list-item-meta" },
          `${file.local_exists ? "本地存在" : "本地不存在"} · 远端 ${file.remote_bytes ?? "?"} bytes`
        )
      );
      const diffText = file.diff || (changed ? "差异为空，可能仅换行/编码变化。" : "无差异。");
      item.append(h("pre", { class: "diff", html: formatDiff(diffText) }));
      inner.push(item);
    }
  }
  return section("03 / 还原差异预览", inner);
}

function resumeSection(d) {
  const r = d.resume;
  let inner;
  if (!r) {
    inner = [h("div", { class: "empty" }, "尚未生成远端接续 Prompt。")];
  } else if (!r.success) {
    inner = [h("div", { class: "empty" }, `接续生成失败：${r.error || "未知错误"}`)];
  } else {
    inner = [
      h(
        "div",
        { class: "list-item" },
        h("div", { class: "list-item-meta" }, "接续 Prompt 已写入："),
        h("div", { class: "list-item-name" }, r.path || "-")
      ),
    ];
  }
  return section("04 / 接续结果", inner);
}
