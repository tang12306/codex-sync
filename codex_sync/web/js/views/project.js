// 项目备份：指定项目快照上传到自建同步服务器。
import { h, asObject, shortId } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { consoleCard, makeRun, actionButton } from "../ui.js";

export function mount(root, store) {
  const run = makeRun(store);
  const { card: outCard, setOutput } = consoleCard("项目备份详情");
  let projectStatus = asObject(store.getState().status?.git);
  let selectedPath = projectStatus.root || projectStatus.selected_path || store.getState().status?.cwd || "";
  let serverBackups = null;
  let loadingStatus = false;
  let loadingBackups = false;

  const projectPathInput = h("input", {
    class: "input project-path-input",
    type: "text",
    placeholder: "留空则使用当前目录",
    value: selectedPath,
  });
  projectPathInput.addEventListener("input", () => {
    selectedPath = projectPathInput.value.trim();
  });

  const projectStatusCard = h("div", { class: "card" });
  const serverBackupsCard = h("div", { class: "card" });

  const currentProjectPath = () => projectPathInput.value.trim();

  const refreshProjectStatus = async (notify = true) => {
    loadingStatus = true;
    renderProjectStatus();
    try {
      projectStatus = notify
        ? await run("git-state", { payload: { project_path: currentProjectPath() }, refresh: false, okMsg: "项目状态已更新" })
        : await runAction("git-state", { project_path: currentProjectPath() });
      if (projectStatus.root && !currentProjectPath()) {
        projectPathInput.value = projectStatus.root;
        selectedPath = projectStatus.root;
      }
    } finally {
      loadingStatus = false;
      renderProjectStatus();
    }
  };

  const refreshServerBackups = async () => {
    loadingBackups = true;
    renderServerBackups();
    try {
      serverBackups = await run("list-project-backups", { refresh: false, okMsg: "服务器备份列表已更新" });
    } finally {
      loadingBackups = false;
      renderServerBackups();
    }
  };

  const uploadProject = async () => {
    const result = await run("project-backup", { payload: { project_path: currentProjectPath() }, okMsg: "项目备份已上传到服务器", refresh: false });
    if (result?.success) await refreshServerBackups();
  };

  const chooseProjectDir = async () => {
    try {
      const result = await runAction("choose-project-dir", { project_path: currentProjectPath() });
      setOutput(result);
      if (result?.success && result.path) {
        projectPathInput.value = result.path;
        selectedPath = result.path;
        showToast("已选择项目文件夹", "success");
        await refreshProjectStatus();
      }
    } catch (e) {
      setOutput({ error: String(e.message || e) });
      showToast(String(e.message || e), "error");
    }
  };

  const useCurrentDir = async () => {
    const cwd = store.getState().status?.cwd || "";
    projectPathInput.value = cwd;
    selectedPath = cwd;
    await refreshProjectStatus();
  };

  const primary = h(
    "div",
    { class: "task-panel project-task-panel" },
    h("div", { class: "task-panel-main" },
      h("span", { class: "task-kicker" }, "项目保护"),
      h("h2", {}, "把指定项目备份到同步服务器"),
      h(
        "p",
        {},
        "选择一个项目目录。Git 项目上传补丁快照；非 Git 目录按安全排除规则打包文件。"
      )
    ),
    h(
      "div",
      { class: "task-panel-actions" },
      projectPathInput,
      actionButton("选择文件夹", "btn-ghost", chooseProjectDir),
      actionButton("使用当前目录", "btn-ghost", useCurrentDir),
      actionButton("上传到服务器", "btn-primary", uploadProject),
      actionButton("刷新服务器备份", "btn-ghost", refreshServerBackups),
      actionButton("检测项目状态", "btn-ghost", refreshProjectStatus)
    )
  );

  const details = h(
    "div",
    { class: "grid grid-2" },
    h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, "备份内容"),
      h("div", { class: "concept-hidden-list" },
        item("Git 项目", "保存 git diff、状态信息，以及允许范围内的小型未跟踪文件。"),
        item("非 Git 项目", "直接打包当前目录中的普通项目文件，使用同一套安全排除规则。"),
        item("本地副本", "上传前会在 ~/.codex-sync/project-backups 保留一份 zip。")
      )
    ),
    h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, "安全边界"),
      h("div", { class: "concept-hidden-list" },
        item("默认跳过", ".env、auth.json、deploy.json、私钥、证书、node_modules、dist、build、缓存。"),
        item("大小限制", "单个项目文件超过“设置 -> 项目备份”的上限会记录为跳过。"),
        item("服务器", "项目 zip 会上传到已配置的自建同步服务器。")
      )
    )
  );

  root.replaceChildren(
    h("div", { class: "section" }, primary),
    h("div", { class: "section grid grid-2" }, projectStatusCard, serverBackupsCard),
    h("div", { class: "section" }, details),
    h("div", { class: "section" }, outCard)
  );

  function renderProjectStatus() {
    const state = asObject(projectStatus);
    const isRepo = Boolean(state.is_repo);
    const exists = state.exists !== false;
    const valid = exists && state.is_dir !== false;
    const nodes = [
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "项目状态"),
        h("span", { class: `badge ${!valid ? "danger" : isRepo ? (state.dirty ? "warn" : "ok") : ""}` }, loadingStatus ? "检查中" : !valid ? "路径不可用" : isRepo ? (state.dirty ? "有改动" : "干净") : "非 Git")
      ),
      h(
        "div",
        { class: "result-facts" },
        fact("模式", valid ? (isRepo ? "Git 补丁快照" : "文件快照") : "-"),
        fact("分支", state.branch || "-"),
        fact("提交", state.commit ? shortId(state.commit) : "-"),
        fact("未跟踪", Array.isArray(state.untracked) ? `${state.untracked.length} 个` : "-")
      ),
      h("p", { class: "card-desc" }, state.root || currentProjectPath() || "当前目录不是 Git 仓库；上传时会按安全排除规则打包普通项目文件。"),
      !valid ? h("div", { class: "status-line error" }, "请选择一个存在的项目文件夹。") : null,
      Array.isArray(state.untracked) && state.untracked.length
        ? h(
            "details",
            { class: "collapse" },
            h("summary", {}, "未跟踪文件"),
            h("div", { class: "collapse-body" }, h("div", { class: "project-file-list" }, ...state.untracked.slice(0, 20).map((name) => h("span", {}, name))))
          )
        : null
    ];
    projectStatusCard.replaceChildren(...nodes.filter(Boolean));
  }

  function renderServerBackups() {
    const backups = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    serverBackupsCard.replaceChildren(
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "服务器项目备份"),
        h("span", { class: `badge ${serverBackups?.error ? "danger" : backups.length ? "ok" : ""}` }, loadingBackups ? "加载中" : serverBackups?.error ? "失败" : `${backups.length} 个`)
      ),
      serverBackups?.error
        ? h("div", { class: "status-line error" }, serverBackups.error)
        : backups.length
          ? h(
              "div",
              { class: "table-wrap" },
              h(
                "table",
                { class: "table" },
                h("thead", {}, h("tr", {}, h("th", {}, "项目"), h("th", {}, "时间"), h("th", {}, "大小"), h("th", {}, "ID"))),
                h("tbody", {}, ...backups.slice(0, 8).map(backupRow))
              )
            )
          : h("div", { class: "empty compact" }, loadingBackups ? "正在读取服务器备份…" : "还没有加载服务器备份，点击“刷新服务器备份”。")
    );
  }

  renderProjectStatus();
  renderServerBackups();
  refreshProjectStatus(false);
  setOutput(store.getState().console);
  const unsub = store.select((s) => s.console, () => setOutput(store.getState().console));
  const unsubStatus = store.select((s) => s.status?.cwd, () => {
    if (!projectPathInput.value.trim()) {
      const status = store.getState().status || {};
      const rootPath = status.git?.root || status.cwd || "";
      projectPathInput.value = rootPath;
      selectedPath = rootPath;
    }
  });
  return () => {
    unsub();
    unsubStatus();
  };
}

function item(title, desc) {
  return h("div", { class: "concept-hidden-item" }, h("b", {}, title), h("span", {}, desc));
}

function fact(label, value) {
  return h("div", { class: "result-fact" }, h("span", {}, label), h("b", {}, value || "-"));
}

function backupRow(item) {
  const backup = asObject(item);
  return h(
    "tr",
    {},
    h("td", { class: "cell-ellipsis", title: backup.repo_root || backup.repo_name || "" }, backup.repo_name || "-"),
    h("td", {}, backup.received_at || backup.created_at || "-"),
    h("td", {}, formatBytes(backup.size_bytes)),
    h("td", { class: "mono", title: backup.id || "" }, shortId(backup.id))
  );
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
