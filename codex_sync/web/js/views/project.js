// 项目备份：把指定项目的完整快照上传到同步服务器，设置自动备份，并按版本恢复到任意目录。
import { h, asObject, shortId, fill } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { navigate } from "../router.js";
import { makeRun, actionButton, serverMissingNotice, switchControl, switchRow } from "../ui.js";
import { confirmDialog, showDialog } from "../dialog.js";
import { facts, failureReason, formatBytes, formatCountdown, formatDateTime, isFailed, loadingSpinner, statusLine } from "../format.js";

const PROJECT_PATH_KEY = "codexSync.project.selectedPath";
const PAGE_SIZE = 200;
const UNTRACKED_PREVIEW = 20;

const TRIGGER_LABELS = {
  realtime: "实时备份",
  "codex-stop": "Codex 关闭时",
  "git-post-commit": "Git 提交后",
  manual: "手动",
  manual_full: "手动",
};
const PLAN_STATUS = {
  create: ["新建", "ok"],
  overwrite: ["覆盖", "warn"],
  same_size: ["大小相同", ""],
};

function loadSavedProjectPath() {
  try {
    return window.localStorage.getItem(PROJECT_PATH_KEY) || "";
  } catch {
    return "";
  }
}

function saveProjectPath(path) {
  try {
    if (path) window.localStorage.setItem(PROJECT_PATH_KEY, path);
  } catch {
    /* localStorage 不可用时忽略 */
  }
}

export function mount(root, store) {
  const run = makeRun(store);
  let alive = true;
  const initialStatus = store.getState().status || {};
  let projectState = asObject(initialStatus.git);
  let autoState = null;
  let serverBackups = null;
  let hasMore = false;
  let loadingState = false;
  let loadingBackups = false;
  let stateSeq = 0;
  const expandedKeys = new Set();

  const startPath = loadSavedProjectPath() || projectState.root || projectState.selected_path || initialStatus.cwd || "";
  const pathInput = h("input", { class: "input", type: "text", placeholder: "项目文件夹路径，留空则使用当前目录", "aria-label": "项目文件夹", spellcheck: "false" });
  pathInput.value = startPath;
  const restoreInput = h("input", { class: "input", type: "text", placeholder: "恢复到哪个文件夹（可以是新文件夹）", spellcheck: "false" });
  restoreInput.value = startPath;
  const searchInput = h("input", { class: "input", type: "search", placeholder: "搜索项目名、设备或原始路径", "aria-label": "搜索项目" });

  const stateCard = h("div", { class: "card" });
  const autoCard = h("div", { class: "card" });
  const libraryCard = h("div", { class: "card section" });

  const projectPath = () => pathInput.value.trim();
  const restoreTarget = () => restoreInput.value.trim() || projectPath();
  const serverReady = () => Boolean(store.getState().status?.config?.server_url);

  let lastPath = projectPath();
  const onPathCommitted = () => {
    const path = projectPath();
    if (path === lastPath) return;
    lastPath = path;
    saveProjectPath(path);
    refreshProjectState();
  };
  pathInput.addEventListener("change", onPathCommitted);
  pathInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") onPathCommitted();
  });
  searchInput.addEventListener("input", () => renderLibrary());

  function setProjectPath(path) {
    pathInput.value = path;
    lastPath = path;
    saveProjectPath(path);
  }

  fill(root,
    h(
      "div",
      { class: "task-panel section" },
      h("h2", {}, "要备份的项目"),
      h(
        "p",
        {},
        "选一个项目文件夹，把它的完整快照上传到同步服务器。会自动排除 .git、node_modules、构建产物、缓存和密钥等文件；内容没有变化时不会重复保存。"
      ),
      h(
        "div",
        { class: "path-bar" },
        pathInput,
        actionButton("选择文件夹", "btn-ghost", chooseProjectDir),
        actionButton("使用当前目录", "btn-ghost", useCurrentDir, { title: "使用 Codex Sync 启动时所在的目录" }),
        actionButton("上传项目备份", "btn-primary", uploadProject)
      )
    ),
    h("div", { class: "section grid grid-2" }, stateCard, autoCard),
    libraryCard
  );

  // ---------- 项目状态 ----------
  async function refreshProjectState() {
    const seq = (stateSeq += 1);
    loadingState = true;
    renderState();
    renderAuto();
    const path = projectPath();
    try {
      const [state, auto] = await Promise.all([
        runAction("git-state", { project_path: path }),
        runAction("project-auto-backup-status", { project_path: path }),
      ]);
      if (!alive || seq !== stateSeq) return;
      projectState = asObject(state);
      autoState = asObject(auto);
      if (!path && projectState.root) {
        setProjectPath(projectState.root);
        if (!restoreInput.value.trim()) restoreInput.value = projectState.root;
      }
    } catch (e) {
      if (!alive || seq !== stateSeq) return;
      projectState = { success: false, error: String(e.message || e) };
      autoState = null;
    }
    loadingState = false;
    renderState();
    renderAuto();
  }

  async function refreshAutoState() {
    try {
      const auto = await runAction("project-auto-backup-status", { project_path: projectPath() });
      if (alive) autoState = asObject(auto);
    } catch {
      /* 保留旧状态 */
    }
    if (alive) renderAuto();
  }

  function pathValid(state) {
    return state.exists !== false && state.is_dir !== false && !state.error;
  }

  function renderState() {
    const state = asObject(projectState);
    const valid = pathValid(state);
    const isRepo = Boolean(state.is_repo);
    const statusLines = String(state.status || "")
      .split("\n")
      .filter((line) => line.trim());
    const untracked = Array.isArray(state.untracked) ? state.untracked : [];
    const tracked = statusLines.filter((line) => !line.startsWith("??")).length;

    let badge;
    if (loadingState) badge = h("span", { class: "badge" }, "检查中");
    else if (!valid) badge = h("span", { class: "badge danger" }, "路径不可用");
    else if (!isRepo) badge = h("span", { class: "badge info" }, "普通文件夹");
    else badge = h("span", { class: `badge ${state.dirty ? "warn" : "ok"}` }, state.dirty ? "有未提交改动" : "工作区干净");

    let explain = "";
    if (valid && !isRepo) explain = "不是 Git 仓库：备份时按排除规则打包文件夹里的文件。";
    else if (valid && state.dirty) explain = `${tracked} 处已跟踪的改动、${untracked.length} 个未跟踪文件，都会包含在下次备份里。`;
    else if (valid) explain = "工作区与最近一次提交一致。";

    fill(stateCard,
      h("div", { class: "card-title" }, h("span", {}, "项目状态"), badge),
      loadingState && !state.root
        ? loadingSpinner("正在读取项目状态…", { compact: true })
        : h(
            "div",
            {},
            valid
              ? facts([
                  ["备份方式", isRepo ? "Git 仓库完整快照" : "文件夹完整快照"],
                  ["分支", state.branch || "-"],
                  ["提交", state.commit ? shortId(state.commit) : "-"],
                  ["未跟踪文件", isRepo ? `${untracked.length} 个` : "-"],
                ])
              : statusLine(state.error ? `读取失败：${state.error}` : "这个路径不存在或不是文件夹，请重新选择。", "error"),
            explain ? h("p", { class: "card-desc" }, explain) : null,
            state.root ? h("p", { class: "card-desc mono", title: state.root }, state.root) : null,
            untracked.length
              ? h(
                  "details",
                  { class: "collapse" },
                  h("summary", {}, `未跟踪文件（${untracked.length} 个）`),
                  h(
                    "div",
                    { class: "collapse-body" },
                    h("p", { class: "card-desc" }, "这些文件不在排除规则里，会和项目一起备份。"),
                    h(
                      "div",
                      { class: "project-file-list" },
                      ...untracked.slice(0, UNTRACKED_PREVIEW).map((name) => h("span", { title: name }, name)),
                      untracked.length > UNTRACKED_PREVIEW ? h("span", {}, `另外 ${untracked.length - UNTRACKED_PREVIEW} 个…`) : null
                    )
                  )
                )
              : null
          )
    );
  }

  // ---------- 自动备份 ----------
  function nextRealtimeText(state) {
    const last = asObject(state.last);
    const interval = Number(state.realtime_interval_seconds) || 300;
    if (!last.last_backup_at) return `约每 ${Math.max(1, Math.round(interval / 60))} 分钟检查一次`;
    const due = new Date(/Z|[+-]\d{2}:\d{2}$/.test(last.last_backup_at) ? last.last_backup_at : `${last.last_backup_at}Z`).getTime() + interval * 1000;
    const seconds = (due - Date.now()) / 1000;
    if (!Number.isFinite(seconds)) return "-";
    return seconds <= 0 ? "下个检查周期" : formatCountdown(seconds);
  }

  function renderAuto() {
    const state = asObject(autoState);
    const loaded = autoState != null;
    const supported = loaded && state.auto_supported !== false && !isFailed(state);
    const isRepo = Boolean(state.is_repo);
    const hookOn = Boolean(state.enabled_for_git_commit);
    const realtimeOn = Boolean(state.in_realtime);
    const stopOn = Boolean(state.codex_stop_enabled);
    const last = asObject(state.last);
    const lastResult = asObject(last.last_result);
    const enabledCount = [hookOn, realtimeOn, stopOn].filter(Boolean).length;
    const interval = Math.max(1, Math.round((Number(state.realtime_interval_seconds) || 300) / 60));

    let badge;
    if (!loaded || loadingState) badge = h("span", { class: "badge" }, "检查中");
    else if (!supported) badge = h("span", { class: "badge warn" }, "不可用");
    else badge = h("span", { class: `badge ${enabledCount ? "ok" : ""}` }, enabledCount ? `已开启 ${enabledCount} 项` : "未开启");

    const hookSwitch = switchControl({
      checked: hookOn,
      disabled: !supported || !isRepo,
      label: "Git 提交后自动备份",
      onChange: (checked, input) =>
        toggle(input, checked ? "project-auto-backup-install-git-hook" : "project-auto-backup-uninstall-git-hook", checked ? "已开启：每次 Git 提交后自动备份" : "已关闭 Git 提交后自动备份"),
    });
    const realtimeSwitch = switchControl({
      checked: realtimeOn,
      disabled: !supported,
      label: "实时备份",
      onChange: (checked, input) =>
        toggle(input, checked ? "realtime-backup-add" : "realtime-backup-remove", checked ? `已开启实时备份：后台约每 ${interval} 分钟检查一次` : "已关闭实时备份"),
    });

    const lastText = last.last_backup_at
      ? `${formatDateTime(last.last_backup_at)}（${TRIGGER_LABELS[last.last_reason] || last.last_reason || "自动"}，${lastResult.success === false ? "失败" : "成功"}）`
      : "还没有自动备份过";
    const queueCount = Number(state.queue_count || 0);

    fill(autoCard,
      h("div", { class: "card-title" }, h("span", {}, "自动备份"), badge),
      h("p", { class: "card-desc" }, "开启后会在后台自动生成完整快照，每一份都可以单独恢复；没有改动时自动跳过。"),
      h(
        "div",
        {},
        switchRow("Git 提交后备份", isRepo || !loaded ? "每次在这个仓库里提交后备份一次" : "只有 Git 仓库支持", hookSwitch.el),
        switchRow("实时备份", `后台约每 ${interval} 分钟检查一次，包含未提交的改动`, realtimeSwitch.el),
        switchRow(
          "Codex 关闭时备份",
          stopOn ? "已在系统设置里开启，对所有项目生效" : "在系统设置里开启，对所有项目生效",
          h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: () => navigate("settings", "policy") }, stopOn ? "查看设置" : "去开启")
        )
      ),
      loaded && supported
        ? facts([
            ["最近一次自动备份", lastText],
            realtimeOn ? ["下次实时备份", nextRealtimeText(state)] : null,
          ])
        : null,
      loaded && !supported ? statusLine(failureReason(state, "当前路径不能开启自动备份，请先选择一个存在的项目文件夹。"), "warn") : null,
      queueCount
        ? h(
            "div",
            { class: "status-line warn spread" },
            h("span", {}, `有 ${queueCount} 个项目在等待自动备份（服务器暂时不可用或间隔未到）。`),
            actionButton("立即处理", "btn-ghost btn-sm", processQueue)
          )
        : null
    );
  }

  async function toggle(input, action, okMsg) {
    input.disabled = true;
    try {
      await run(action, { payload: { project_path: projectPath() }, refresh: false, okMsg, label: "自动备份设置" });
    } catch {
      /* 已提示 */
    }
    await refreshAutoState();
  }

  async function processQueue() {
    const result = await run("project-auto-backup-process", {
      payload: { limit: 5 },
      refresh: false,
      okMsg: "等待中的自动备份已处理",
      label: "处理自动备份",
    });
    if (!alive) return;
    await refreshAutoState();
    if (!isFailed(result)) loadBackups();
  }

  // ---------- 上传 / 选择目录 ----------
  async function uploadProject() {
    if (!serverReady()) {
      showToast("还没有配置同步服务器", "warning", { action: { label: "去配置", onClick: () => navigate("settings", "server") } });
      return;
    }
    const name = projectState.root ? projectState.root.split(/[\\/]/).filter(Boolean).pop() : "项目";
    const result = await run("project-backup", {
      payload: { project_path: projectPath() },
      refresh: false,
      label: "上传项目备份",
      okMsg: `「${name}」已备份到服务器`,
    });
    if (alive && !isFailed(result)) loadBackups();
  }

  async function pickDir(current, purpose) {
    try {
      const result = await runAction("choose-project-dir", { project_path: current, purpose });
      if (result?.success && result.path) return result.path;
      if (result && !result.cancelled && result.error) showToast(result.error, "error");
    } catch (e) {
      showToast(String(e.message || e), "error");
    }
    return "";
  }

  async function chooseProjectDir() {
    const path = await pickDir(projectPath(), "project");
    if (!path || !alive) return;
    setProjectPath(path);
    await refreshProjectState();
  }

  async function useCurrentDir() {
    setProjectPath(store.getState().status?.cwd || "");
    await refreshProjectState();
  }

  async function chooseRestoreDir() {
    const path = await pickDir(restoreTarget(), "restore");
    if (path && alive) restoreInput.value = path;
  }

  // ---------- 服务器项目库 ----------
  async function loadBackups({ more = false, notify = false } = {}) {
    if (!serverReady()) {
      renderLibrary();
      return;
    }
    const current = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    loadingBackups = true;
    renderLibrary();
    let result;
    try {
      result = await runAction("list-project-backups", { limit: PAGE_SIZE, offset: more ? current.length : 0 });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive) return;
    loadingBackups = false;
    if (isFailed(result)) {
      if (more) showToast(`加载更多失败：${failureReason(result)}`, "error");
      else serverBackups = result;
    } else {
      const rows = Array.isArray(result.project_backups) ? result.project_backups : [];
      if (more) {
        const seen = new Set(current.map((item) => asObject(item).id).filter(Boolean));
        serverBackups = { ...result, project_backups: current.concat(rows.filter((item) => !seen.has(asObject(item).id))) };
      } else {
        serverBackups = result;
      }
      hasMore = rows.length >= PAGE_SIZE;
      ensureExpanded();
    }
    renderLibrary();
    if (notify && !more) {
      if (isFailed(result)) showToast(`读取项目库失败：${failureReason(result)}`, "error");
      else showToast("项目库已刷新", "success");
    }
  }

  function allBackups() {
    return Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
  }

  function ensureExpanded() {
    const groups = buildProjectGroups(allBackups());
    const valid = new Set(groups.map((group) => group.key));
    for (const key of [...expandedKeys]) if (!valid.has(key)) expandedKeys.delete(key);
    if (!expandedKeys.size && groups.length) expandedKeys.add(groups[0].key);
  }

  function renderLibrary() {
    const backups = allBackups();
    const groups = buildProjectGroups(backups);
    const failed = Boolean(serverBackups) && isFailed(serverBackups);
    const query = searchInput.value.trim().toLowerCase();
    const visible = query ? groups.filter((group) => projectGroupSearchText(group).includes(query)) : groups;

    let badge = null;
    if (loadingBackups) badge = h("span", { class: "badge" }, "读取中");
    else if (failed) badge = h("span", { class: "badge danger" }, "读取失败");
    else if (serverBackups) badge = h("span", { class: "badge info" }, `${groups.length} 个项目 · ${backups.length} 份`);

    let body;
    if (!serverReady()) body = serverMissingNotice("还没有配置同步服务器，无法读取项目库。");
    else if (failed) body = statusLine(`读取项目库失败：${failureReason(serverBackups)}`, "error");
    else if (!serverBackups) body = loadingSpinner("正在读取服务器上的项目备份…");
    else if (!backups.length) body = h("div", { class: "empty compact" }, "服务器上还没有项目备份。在上方选择项目后点「上传项目备份」。");
    else {
      body = h(
        "div",
        { class: "project-panels" },
        groups.length > 1 ? searchInput : null,
        h(
          "div",
          { class: "project-panel-list" },
          ...(visible.length ? visible.map(projectPanel) : [h("div", { class: "empty compact" }, "没有匹配的项目")])
        ),
        hasMore
          ? h(
              "div",
              { class: "list-more" },
              actionButton("加载更早的备份", "btn-ghost btn-sm", () => loadBackups({ more: true }))
            )
          : null
      );
    }

    const ready = serverReady();
    fill(libraryCard,
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "服务器项目库"),
        ready
          ? h("div", { class: "card-title-actions" }, badge, actionButton("刷新", "btn-ghost btn-sm", () => loadBackups({ notify: true })))
          : null
      ),
      h(
        "p",
        { class: "card-desc" },
        "每份备份都是完整的源码快照（不含 .git 历史），恢复后就是一个可以直接使用的项目目录，适合换一台电脑接着写。"
      ),
      ready && h(
        "div",
        { class: "project-restore-controls" },
        h(
          "div",
          { class: "field" },
          h("label", { class: "field-label" }, h("span", {}, "恢复到"), h("span", { class: "field-hint" }, "可以选一个空文件夹，也可以直接输入新路径")),
          restoreInput
        ),
        h(
          "div",
          { class: "card-actions" },
          actionButton("选择文件夹", "btn-ghost", chooseRestoreDir),
          h(
            "button",
            {
              class: "btn btn-ghost",
              type: "button",
              onClick: () => {
                restoreInput.value = projectPath() || store.getState().status?.cwd || "";
              },
            },
            "使用上面的项目目录"
          )
        )
      ),
      body
    );
  }

  function projectPanel(group) {
    const expanded = expandedKeys.has(group.key);
    const head = h(
      "button",
      { class: "project-panel-head", type: "button", "aria-expanded": expanded ? "true" : "false" },
      h("span", { class: "project-panel-caret", "aria-hidden": "true" }, expanded ? "▾" : "▸"),
      h("span", { class: "project-panel-name", title: group.rootsText || group.repoName }, group.repoName),
      h("span", { class: "project-panel-meta" }, `${group.backups.length} 份 · ${group.devices.size} 台设备 · 最新 ${formatDateTime(group.latestAt)}`)
    );
    head.addEventListener("click", () => {
      if (expandedKeys.has(group.key)) expandedKeys.delete(group.key);
      else expandedKeys.add(group.key);
      renderLibrary();
    });
    return h(
      "div",
      { class: `project-panel${expanded ? " expanded" : ""}` },
      head,
      expanded ? h("div", { class: "project-panel-body" }, projectVersions(group)) : null
    );
  }

  function projectVersions(group) {
    const hasFull = group.backups.some((backup) => backup.backup_kind === "full");
    return h(
      "div",
      { class: "project-version-stack" },
      group.rootsText ? h("div", { class: "project-roots mono cell-ellipsis", title: group.rootsText }, `原始路径：${group.rootsText}`) : null,
      hasFull
        ? h("div", { class: "card-actions" }, actionButton("恢复最新的完整备份", "btn-primary", () => restoreLatest(group)))
        : statusLine("这个项目只有补丁备份。补丁只能应用到同一个 Git 仓库上；换电脑恢复需要先有一份完整备份。", "warn"),
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "table" },
          h(
            "thead",
            {},
            h("tr", {}, h("th", {}, "类型"), h("th", {}, "设备"), h("th", {}, "分支 / 提交"), h("th", {}, "上传时间"), h("th", {}, "大小"), h("th", {}, "操作"))
          ),
          h("tbody", {}, ...group.backups.map(backupRow))
        )
      )
    );
  }

  function backupRow(backup) {
    const time = backup.received_at || backup.created_at || "";
    const branch = backup.branch || "-";
    const commit = backup.commit_sha ? shortId(backup.commit_sha) : "-";
    return h(
      "tr",
      {},
      h(
        "td",
        {},
        h(
          "div",
          { class: "badge-row" },
          h("span", { class: `badge ${backup.backup_kind === "full" ? "ok" : ""}` }, projectBackupKindLabel(backup)),
          h("span", { class: "mono text-dim", title: backup.id || "" }, shortId(backup.id))
        )
      ),
      h("td", { class: "cell-ellipsis", title: backup.device_id || "" }, backup.device_id || "-"),
      h("td", { class: "cell-ellipsis", title: `${branch} / ${commit}` }, `${branch} / ${commit}`),
      h("td", { title: time ? `UTC ${time}` : "" }, formatDateTime(time)),
      h("td", {}, formatBytes(backup.size_bytes)),
      h(
        "td",
        {},
        h(
          "div",
          { class: "row-actions" },
          actionButton("预览", "btn-ghost btn-sm", () => previewRestore(backup), { title: "下载并查看恢复后会新建、覆盖哪些文件" }),
          actionButton("恢复", "btn-ghost btn-sm", () => restoreBackup(backup))
        )
      )
    );
  }

  // ---------- 恢复 ----------
  function requireTarget() {
    const target = restoreTarget();
    if (!target) showToast("请先填写或选择要恢复到的文件夹", "warning");
    return target;
  }

  function previewBody(preview, target) {
    if (preview.restore_type === "patch") {
      const untracked = Array.isArray(preview.untracked) ? preview.untracked : [];
      return h(
        "div",
        {},
        h("p", { class: "dialog-message" }, `恢复到：${target}`),
        statusLine(
          preview.patch_ok ? "补丁可以干净地应用到目标目录。" : `补丁无法应用到目标目录：${preview.error || preview.patch_stderr || "git apply --check 失败"}`,
          preview.patch_ok ? "ok" : "error"
        ),
        untracked.length
          ? h(
              "div",
              {},
              h("p", { class: "card-desc" }, `还会写入 ${preview.untracked_count || untracked.length} 个未跟踪文件：`),
              h("div", { class: "plan-list" }, ...untracked.map((path) => h("div", { class: "plan-row" }, h("span", { class: "badge" }, "文件"), h("span", { class: "plan-path", title: path }, path), h("span", {}, ""))))
            )
          : null
      );
    }
    const planned = Array.isArray(preview.planned) ? preview.planned : [];
    const total = Number(preview.file_count || planned.length);
    const overwrite = Number(preview.overwrite_count || 0);
    const skipped = Array.isArray(preview.skipped) ? preview.skipped : [];
    return h(
      "div",
      {},
      h("p", { class: "dialog-message" }, `恢复到：${target}`),
      h(
        "div",
        { class: "plan-summary" },
        h("span", { class: "badge info" }, `共 ${total} 个文件`),
        h("span", { class: "badge ok" }, `新建 ${Math.max(0, total - overwrite)}`),
        h("span", { class: `badge ${overwrite ? "warn" : ""}` }, `覆盖 ${overwrite}`),
        skipped.length ? h("span", { class: "badge danger" }, `跳过 ${skipped.length}`) : null
      ),
      overwrite ? statusLine("目标文件夹里已有同名文件。恢复前会先把目标文件夹当前的内容备份一份，再覆盖这些文件。", "warn") : null,
      planned.length
        ? h(
            "div",
            { class: "plan-list" },
            ...planned.map((item) => {
              const [label, tone] = PLAN_STATUS[item.status] || [item.status, ""];
              return h(
                "div",
                { class: "plan-row" },
                h("span", { class: `badge ${tone}` }, label),
                h("span", { class: "plan-path", title: item.path }, item.path),
                h("span", { class: "text-dim" }, formatBytes(item.bytes))
              );
            })
          )
        : h("div", { class: "empty compact" }, "这个备份里没有文件"),
      total > planned.length ? h("p", { class: "card-desc" }, `只列出前 ${planned.length} 个文件。`) : null
    );
  }

  async function previewRestore(backup) {
    const target = requireTarget();
    if (!target) return;
    let result;
    try {
      result = await runAction("preview-project-restore", { backup_id: backup.id, project_path: target });
    } catch (e) {
      showToast(`生成预览失败：${e.message || e}`, "error");
      return;
    }
    if (!alive) return;
    const preview = asObject(result.preview);
    if (isFailed(result) && !preview.restore_type) {
      showToast(`生成预览失败：${failureReason(result)}`, "error");
      return;
    }
    const canRestore = preview.restore_type !== "patch" || Boolean(preview.patch_ok);
    const choice = await showDialog({
      title: `恢复预览 · ${projectBackupKindLabel(backup)}`,
      body: previewBody(preview, target),
      wide: true,
      actions: [
        { label: "关闭", value: false, class: "btn-ghost", autofocus: !canRestore },
        canRestore ? { label: "恢复这个版本", value: true, class: "btn-primary" } : null,
      ].filter(Boolean),
      dismissValue: false,
    });
    if (choice && alive) await restoreBackup(backup, { confirmed: true, target });
  }

  async function restoreBackup(backup, { confirmed = false, target = "" } = {}) {
    const dest = target || requireTarget();
    if (!dest) return;
    const patch = backup.backup_kind !== "full";
    if (!confirmed) {
      const ok = await confirmDialog({
        title: "恢复这个项目备份？",
        message: `把「${backup.repo_name || "项目"}」${formatDateTime(backup.received_at || backup.created_at)} 的${projectBackupKindLabel(backup)}恢复到：${dest}`,
        details: [
          "目标文件夹里已有文件时，会先把当前内容备份一份，再覆盖同名文件",
          "目标文件夹里多出来的文件不会被删除",
          patch ? "这是补丁备份，只能应用到同一个 Git 仓库上" : null,
        ],
        confirmLabel: "恢复",
        danger: true,
      });
      if (!ok) return;
    }
    const result = await run("restore-project-backup", {
      payload: { backup_id: backup.id, confirm_backup_id: backup.id, project_path: dest, overwrite: true },
      refresh: false,
      label: "恢复项目",
      okMsg: (r) => `已恢复 ${asObject(r.restore).restored_count ?? 0} 个文件到 ${dest}`,
    });
    if (alive && !isFailed(result) && samePath(dest, projectPath())) refreshProjectState();
  }

  async function restoreLatest(group) {
    const dest = requireTarget();
    if (!dest) return;
    const ok = await confirmDialog({
      title: `恢复「${group.repoName}」的最新完整备份？`,
      message: `会下载这个项目在服务器上最新的一份完整备份，恢复到：${dest}`,
      details: ["目标文件夹里已有文件时，会先把当前内容备份一份，再覆盖同名文件", "目标文件夹里多出来的文件不会被删除"],
      confirmLabel: "恢复",
      danger: true,
    });
    if (!ok) return;
    const result = await run("restore-latest-project-backup", {
      payload: { repo_name: group.repoName, project_path: dest, overwrite: true },
      refresh: false,
      label: "恢复项目",
      okMsg: (r) => `已恢复 ${asObject(r.restore).restored_count ?? 0} 个文件到 ${dest}`,
    });
    if (alive && !isFailed(result) && samePath(dest, projectPath())) refreshProjectState();
  }

  renderState();
  renderAuto();
  renderLibrary();
  refreshProjectState();
  loadBackups();

  const unsubCwd = store.select(
    (s) => s.status?.cwd,
    (cwd) => {
      if (projectPath() || !cwd) return;
      setProjectPath(cwd);
      if (!restoreInput.value.trim()) restoreInput.value = cwd;
      refreshProjectState();
    }
  );
  const unsubServer = store.select(
    (s) => s.status?.config?.server_url,
    () => loadBackups()
  );

  return () => {
    alive = false;
    unsubCwd();
    unsubServer();
  };
}

function samePath(a, b) {
  const norm = (p) => String(p || "").replace(/[\\/]+$/, "").replace(/\\/g, "/").toLowerCase();
  return Boolean(a) && norm(a) === norm(b);
}

function buildProjectGroups(items) {
  const map = new Map();
  for (const item of items) {
    const backup = asObject(item);
    const repoName = backup.repo_name || "未命名项目";
    const key = repoName.toLowerCase();
    if (!map.has(key)) {
      map.set(key, { key, repoName, backups: [], devices: new Set(), roots: new Set(), latestAt: "", latestMs: 0, rootsText: "" });
    }
    const group = map.get(key);
    group.backups.push(backup);
    if (backup.device_id) group.devices.add(backup.device_id);
    if (backup.repo_root) group.roots.add(backup.repo_root);
    const ms = backupTimeMs(backup);
    if (ms >= group.latestMs) {
      group.latestMs = ms;
      group.latestAt = backup.received_at || backup.created_at || "";
    }
  }
  const groups = [...map.values()];
  for (const group of groups) {
    group.backups.sort((a, b) => backupTimeMs(b) - backupTimeMs(a));
    const roots = [...group.roots];
    group.rootsText = roots.slice(0, 3).join(" | ");
    if (roots.length > 3) group.rootsText += ` | 另 ${roots.length - 3} 个路径`;
  }
  groups.sort((a, b) => b.latestMs - a.latestMs || a.repoName.localeCompare(b.repoName));
  return groups;
}

function backupTimeMs(backup) {
  const text = String(backup.received_at || backup.created_at || "").trim();
  if (!text) return 0;
  const normalized = /^\d{4}-\d{2}-\d{2}T/.test(text) && !/(Z|[+-]\d{2}:\d{2})$/.test(text) ? `${text}Z` : text;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : 0;
}

function projectGroupSearchText(group) {
  return [group.repoName, [...group.devices].join(" "), [...group.roots].join(" ")].join(" ").toLowerCase();
}

function projectBackupKindLabel(item) {
  const backup = asObject(item);
  const mode = backup.source_mode || "";
  if (mode === "git_full") return "完整备份";
  if (mode === "filesystem") return "完整备份（文件夹）";
  if (mode === "git_patch") return "未提交改动补丁";
  if (mode === "git_commit") return "提交补丁";
  if (backup.backup_kind === "full") return "完整备份";
  return backup.backup_kind === "patch" ? "补丁" : "备份";
}
