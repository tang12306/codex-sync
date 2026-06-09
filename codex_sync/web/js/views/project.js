// 项目备份：指定项目快照上传到自建同步服务器。
import { h, asObject, shortId } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { makeRun, actionButton, formatDateTime } from "../ui.js";

const PROJECT_PATH_KEY = "codexSync.project.selectedPath";

function loadSavedProjectPath() {
  try {
    return window.localStorage.getItem(PROJECT_PATH_KEY) || "";
  } catch (e) {
    return "";
  }
}

function saveProjectPath(path) {
  try {
    if (path) window.localStorage.setItem(PROJECT_PATH_KEY, path);
  } catch (e) {
    /* localStorage 不可用时忽略 */
  }
}

export function mount(root, store) {
  const run = makeRun(store);
  const projectBackupPageSize = 200;
  let projectStatus = asObject(store.getState().status?.git);
  let selectedPath = loadSavedProjectPath() || projectStatus.root || projectStatus.selected_path || store.getState().status?.cwd || "";
  let serverBackups = null;
  let expandedKeys = new Set();
  let serverBackupsHasMore = false;
  let autoBackupStatus = null;
  let loadingStatus = false;
  let loadingBackups = false;
  let loadingMoreBackups = false;
  let loadingAutoBackup = false;

  const projectPathInput = h("input", {
    class: "input project-path-input",
    type: "text",
    placeholder: "留空则使用当前目录",
    value: selectedPath,
  });
  projectPathInput.addEventListener("input", () => {
    selectedPath = projectPathInput.value.trim();
    saveProjectPath(selectedPath);
  });
  const restorePathInput = h("input", {
    class: "input project-path-input",
    type: "text",
    placeholder: "选择本机文件夹后自动填入；也可粘贴或输入新目录",
    value: selectedPath,
  });
  const projectSearchInput = h("input", {
    class: "input",
    type: "search",
    placeholder: "搜索项目名、设备或原始路径",
  });
  projectSearchInput.addEventListener("input", () => renderServerBackups());

  const projectStatusCard = h("div", { class: "card" });
  const autoBackupCard = h("div", { class: "card" });
  const serverBackupsCard = h("div", { class: "card" });

  const currentProjectPath = () => projectPathInput.value.trim();
  const restoreTargetPath = () => restorePathInput.value.trim() || currentProjectPath();

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
        saveProjectPath(selectedPath);
        if (!restorePathInput.value.trim()) restorePathInput.value = projectStatus.root;
      }
      await refreshAutoBackupStatus(false);
    } finally {
      loadingStatus = false;
      renderProjectStatus();
    }
  };

  const refreshServerBackups = async (notify = true) => {
    loadingBackups = true;
    renderServerBackups();
    try {
      serverBackups = notify
        ? await run("list-project-backups", { payload: { limit: projectBackupPageSize, offset: 0 }, refresh: false, okMsg: "服务器项目库已更新" })
        : await runAction("list-project-backups", { limit: projectBackupPageSize, offset: 0 });
      const rows = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
      serverBackupsHasMore = rows.length >= projectBackupPageSize;
      ensureExpanded();
    } finally {
      loadingBackups = false;
      renderServerBackups();
    }
  };

  const loadMoreServerBackups = async () => {
    const current = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    if (!current.length) {
      await refreshServerBackups(true);
      return;
    }
    loadingMoreBackups = true;
    renderServerBackups();
    try {
      const result = await runAction("list-project-backups", { limit: projectBackupPageSize, offset: current.length });
      if (result?.success === false || result?.error) {
        showToast(result.error || "加载更多项目备份失败", "error");
        return;
      }
      const next = Array.isArray(result.project_backups) ? result.project_backups : [];
      const seen = new Set(current.map((item) => asObject(item).id).filter(Boolean));
      const merged = current.concat(next.filter((item) => {
        const id = asObject(item).id;
        return !id || !seen.has(id);
      }));
      serverBackups = { ...result, project_backups: merged, offset: 0, loaded_count: merged.length };
      serverBackupsHasMore = next.length >= projectBackupPageSize;
      showToast(next.length ? `已加载更多：${next.length} 份` : "没有更多项目备份", "success");
      ensureExpanded();
    } finally {
      loadingMoreBackups = false;
      renderServerBackups();
    }
  };

  const uploadProject = async () => {
    const result = await run("project-backup", { payload: { project_path: currentProjectPath() }, okMsg: "项目备份已上传到服务器", refresh: false });
    if (result?.success) await refreshServerBackups();
  };

  const refreshAutoBackupStatus = async (notify = true) => {
    loadingAutoBackup = true;
    renderAutoBackupStatus();
    try {
      autoBackupStatus = notify
        ? await run("project-auto-backup-status", { payload: { project_path: currentProjectPath() }, refresh: false, okMsg: "自动备份状态已更新" })
        : await runAction("project-auto-backup-status", { project_path: currentProjectPath() });
    } finally {
      loadingAutoBackup = false;
      renderAutoBackupStatus();
    }
  };

  const installGitAutoBackup = async () => {
    await run("project-auto-backup-install-git-hook", {
      payload: { project_path: currentProjectPath() },
      refresh: false,
      okMsg: "Git 提交自动备份已安装",
      errMsg: "安装失败",
      label: "项目自动备份",
    });
    await refreshAutoBackupStatus(false);
  };

  const uninstallGitAutoBackup = async () => {
    await run("project-auto-backup-uninstall-git-hook", {
      payload: { project_path: currentProjectPath() },
      refresh: false,
      okMsg: "Git 提交自动备份已关闭",
      errMsg: "关闭失败",
      label: "项目自动备份",
    });
    await refreshAutoBackupStatus(false);
  };

  const processAutoBackupQueue = async () => {
    const result = await run("project-auto-backup-process", {
      payload: { limit: 5 },
      refresh: false,
      okMsg: "自动备份队列已处理",
      errMsg: "处理失败",
      label: "项目自动备份",
    });
    if (result?.success) {
      await refreshAutoBackupStatus(false);
      await refreshServerBackups();
    }
  };

  const toggleRealtime = async (enable) => {
    await run(enable ? "realtime-backup-add" : "realtime-backup-remove", {
      payload: { project_path: currentProjectPath() },
      refresh: false,
      okMsg: enable ? "已加入实时备份：后台约每 5 分钟自动备份该项目工作区（含未提交改动）" : "已移出实时备份",
      errMsg: "操作失败",
      label: "实时备份",
    });
    await refreshAutoBackupStatus(false);
  };

  const previewProjectRestore = async (backup) => {
    const backupId = backup.id || "";
    const target = restoreTargetPath();
    if (!backupId) {
      showToast("备份 ID 缺失", "warning");
      return;
    }
    if (!target) {
      showToast("请先填写恢复目标目录", "warning");
      return;
    }
    await run("preview-project-restore", {
      payload: { backup_id: backupId, project_path: target },
      refresh: false,
      okMsg: "项目恢复预览已生成",
      errMsg: "项目恢复预览失败",
      label: "项目恢复预览",
    });
  };

  const restoreProjectBackup = async (backup) => {
    const backupId = backup.id || "";
    const target = restoreTargetPath();
    if (!backupId) {
      showToast("备份 ID 缺失", "warning");
      return;
    }
    if (!target) {
      showToast("请先填写恢复目标目录", "warning");
      return;
    }
    const kind = projectBackupKindLabel(backup);
    if (!window.confirm(`将把服务器项目备份「${kind} / ${shortId(backupId)}」恢复到：\n${target}\n\n如果目标目录已有文件，会先创建本地预飞备份，再覆盖同名文件。继续吗？`)) return;
    const result = await run("restore-project-backup", {
      payload: { backup_id: backupId, confirm_backup_id: backupId, project_path: target, overwrite: true },
      refresh: false,
      okMsg: "项目备份已恢复",
      errMsg: "项目备份恢复失败",
      label: "项目恢复",
    });
    if (result?.success) {
      await refreshProjectStatus(false);
    }
  };

  const restoreLatestProjectBackup = async (group) => {
    const repoName = group?.repoName || "";
    const target = restoreTargetPath();
    if (!repoName) {
      showToast("项目名缺失", "warning");
      return;
    }
    if (!target) {
      showToast("请先填写恢复目标目录", "warning");
      return;
    }
    if (!window.confirm(`将把项目「${repoName}」云端最新的完整版恢复到：\n${target}\n\n如果目标目录已有文件，会先创建本地预飞备份，再覆盖同名文件。继续吗？`)) return;
    const result = await run("restore-latest-project-backup", {
      payload: { repo_name: repoName, project_path: target, overwrite: true },
      refresh: false,
      okMsg: "已恢复该项目最新完整版",
      errMsg: "一键恢复失败",
      label: "一键恢复",
    });
    if (result?.success) {
      await refreshProjectStatus(false);
    }
  };

  const chooseProjectDir = async () => {
    try {
      const result = await runAction("choose-project-dir", { project_path: currentProjectPath(), purpose: "project" });
      if (result?.success && result.path) {
        projectPathInput.value = result.path;
        selectedPath = result.path;
        saveProjectPath(selectedPath);
        showToast("已选择项目文件夹", "success");
        await refreshProjectStatus();
      }
    } catch (e) {
      showToast(String(e.message || e), "error");
    }
  };

  const chooseRestoreDir = async () => {
    try {
      const result = await runAction("choose-project-dir", { project_path: restoreTargetPath(), purpose: "restore" });
      if (result?.success && result.path) {
        restorePathInput.value = result.path;
        showToast("已选择恢复目标文件夹", "success");
        renderServerBackups();
      }
    } catch (e) {
      showToast(String(e.message || e), "error");
    }
  };

  const useCurrentDir = async () => {
    const cwd = store.getState().status?.cwd || "";
    projectPathInput.value = cwd;
    selectedPath = cwd;
    saveProjectPath(selectedPath);
    await refreshProjectStatus();
  };

  const useCurrentAsRestoreTarget = async () => {
    restorePathInput.value = currentProjectPath() || store.getState().status?.cwd || "";
    renderServerBackups();
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
        "选择一个项目目录上传完整安全基线；服务器项目库可按项目选择备份版本，并恢复到本机任意目标目录。"
      )
    ),
    h(
      "div",
      { class: "task-panel-actions" },
      projectPathInput,
      actionButton("选择文件夹", "btn-ghost", chooseProjectDir),
      actionButton("使用当前目录", "btn-ghost", useCurrentDir),
      actionButton("上传完整基线", "btn-primary", uploadProject),
      actionButton("刷新项目库", "btn-ghost", refreshServerBackups),
      actionButton("检测项目状态", "btn-ghost", refreshProjectStatus)
    )
  );

  root.replaceChildren(
    h("div", { class: "section" }, primary),
    h("div", { class: "section grid grid-2" }, projectStatusCard, autoBackupCard),
    h("div", { class: "section" }, serverBackupsCard)
  );

  function renderProjectStatus() {
    const state = asObject(projectStatus);
    const isRepo = Boolean(state.is_repo);
    const exists = state.exists !== false;
    const valid = exists && state.is_dir !== false;
    const statusLines = String(state.status || "").split("\n").filter((l) => l.trim());
    const untrackedCount = Array.isArray(state.untracked) ? state.untracked.length : 0;
    const trackedCount = statusLines.filter((l) => !l.startsWith("??")).length;
    const explain = !valid
      ? ""
      : !isRepo
        ? "非 Git 目录：上传时按安全排除规则打包普通项目文件。"
        : state.dirty
          ? `${trackedCount} 处已跟踪改动 / ${untrackedCount} 个未跟踪文件，都会纳入下次备份。`
          : "工作区干净，与上次提交一致。";
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
        fact("模式", valid ? (isRepo ? "完整基线 + 增量补丁" : "完整目录快照") : "-"),
        fact("分支", state.branch || "-"),
        fact("提交", state.commit ? shortId(state.commit) : "-"),
        fact("未跟踪", Array.isArray(state.untracked) ? `${state.untracked.length} 个` : "-")
      ),
      explain ? h("p", { class: "card-desc" }, explain) : null,
      h("p", { class: "card-desc" }, state.root || currentProjectPath() || "当前目录不是 Git 仓库；上传时会按安全排除规则打包普通项目文件。"),
      !valid ? h("div", { class: "status-line error" }, "请选择一个存在的项目文件夹。") : null,
      Array.isArray(state.untracked) && state.untracked.length
        ? h(
            "details",
            { class: "collapse" },
            h("summary", {}, `未跟踪文件（${state.untracked.length} 个）`),
            h(
              "div",
              { class: "collapse-body" },
              h("p", { class: "card-desc" }, "不在安全排除规则（.git / node_modules / dist / build / 缓存 / 密钥等）内的未跟踪文件，会随备份一起保存。"),
              h("div", { class: "project-file-list" }, ...state.untracked.slice(0, 20).map((name) => h("span", {}, name)))
            )
          )
        : null
    ];
    projectStatusCard.replaceChildren(...nodes.filter(Boolean));
  }

  function renderAutoBackupStatus() {
    const state = asObject(autoBackupStatus);
    const isRepo = Boolean(state.is_repo);
    const autoSupported = state.auto_supported !== false;
    const enabled = Boolean(state.enabled_for_git_commit);
    const last = asObject(state.last);
    const intervalSec = state.realtime_interval_seconds || 300;
    const REASON_LABELS = { realtime: "实时", "codex-stop": "Codex 关闭", "git-post-commit": "Git 提交", manual: "手动" };
    let nextText = "未开启";
    if (state.in_realtime) {
      if (last.last_backup_at) {
        const diff = new Date(last.last_backup_at).getTime() + intervalSec * 1000 - Date.now();
        nextText = diff <= 0 ? "即将（下个轮询周期）" : `约 ${Math.max(1, Math.round(diff / 60000))} 分钟后`;
      } else {
        nextText = `约每 ${Math.max(1, Math.round(intervalSec / 60))} 分钟`;
      }
    }
    let resultText = "尚无";
    if (last.last_backup_at) {
      const ok = asObject(last.last_result).success;
      resultText = (ok === false ? "失败" : ok ? "成功" : "已执行") + (last.last_reason ? `（${REASON_LABELS[last.last_reason] || last.last_reason}）` : "");
    }
    const nodes = [
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "自动备份"),
        h("span", { class: `badge ${loadingAutoBackup ? "" : !autoSupported ? "warn" : enabled || (!isRepo && state.codex_stop_enabled) ? "ok" : ""}` }, loadingAutoBackup ? "检查中" : !autoSupported ? "路径不可用" : isRepo ? (enabled ? "已开启" : "未开启") : "非 Git 可入队")
      ),
      h("p", { class: "card-desc" }, "自动备份现在每次都生成完整快照（含工作树全部源码文件，按内容去重、无改动自动跳过），每一份都可独立一键恢复。可安装 Git 提交 hook、开启 Codex 关闭触发，或把项目加入实时备份。"),
      h(
        "div",
        { class: "result-facts" },
        fact("Git 提交触发", enabled ? "已安装" : "未安装"),
        fact("待处理队列", state.queue_count != null ? `${state.queue_count} 个` : "-"),
        fact("Codex 关闭触发", state.codex_stop_enabled ? "已在设置中开启" : "未开启"),
        fact("实时备份", state.in_realtime ? `已开启（约每 ${Math.max(1, Math.round(intervalSec / 60))} 分钟）` : "未开启"),
        fact("最近备份", formatDateTime(last.last_backup_at)),
        fact("实时下次", nextText),
        fact("上次结果", resultText)
      ),
      !isRepo && autoSupported ? h("div", { class: "status-line warn" }, "非 Git 目录不支持提交后 hook，但支持 Codex 关闭触发和手动入队。") : null,
      h(
        "div",
        { class: "card-actions" },
        actionButton("安装提交后自动备份", "btn-primary", installGitAutoBackup),
        actionButton("关闭提交后自动备份", "btn-ghost", uninstallGitAutoBackup),
        state.in_realtime
          ? actionButton("移出实时备份", "btn-ghost", () => toggleRealtime(false))
          : actionButton("加入实时备份", "btn-primary", () => toggleRealtime(true)),
        actionButton("处理待备份队列", "btn-ghost", processAutoBackupQueue),
        actionButton("刷新自动备份状态", "btn-ghost", () => refreshAutoBackupStatus(true))
      )
    ];
    autoBackupCard.replaceChildren(...nodes.filter(Boolean));
  }

  function renderServerBackups() {
    const backups = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    const groups = buildProjectGroups(backups);
    const query = groups.length > 1 ? projectSearchInput.value.trim().toLowerCase() : "";
    const visibleGroups = query
      ? groups.filter((group) => projectGroupSearchText(group).includes(query))
      : groups;
    serverBackupsCard.replaceChildren(
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "服务器项目库"),
        h("span", { class: `badge ${serverBackups?.error ? "danger" : backups.length ? "ok" : ""}` }, loadingBackups ? "加载中" : serverBackups?.error ? "失败" : `${groups.length} 个项目 / ${backups.length} 份`)
      ),
      h("p", { class: "card-desc" }, "选择项目，点「一键恢复最新完整版」即可把该项目云端最新完整快照恢复到目标目录。每份备份都是完整快照（含工作树源码、不含 .git 历史），恢复即得可用源码目录；换台电脑接着写足够了，需要 Git 历史可另接远程仓库。"),
      h(
        "div",
        { class: "project-restore-controls" },
        h(
          "div",
          { class: "field project-restore-field" },
          h("label", { class: "field-label" }, h("span", {}, "恢复目标目录"), h("span", { class: "field-hint" }, "优先从本机选择，可粘贴新目录")),
          restorePathInput
        ),
        h("div", { class: "card-actions" },
          actionButton("选择本机文件夹", "btn-primary", chooseRestoreDir),
          actionButton("使用当前项目目录", "btn-ghost", useCurrentAsRestoreTarget),
          actionButton("刷新项目库", "btn-ghost", refreshServerBackups),
          serverBackupsHasMore ? actionButton(loadingMoreBackups ? "加载中…" : "加载更多", "btn-ghost", loadMoreServerBackups) : null
        )
      ),
      serverBackups?.error
        ? h("div", { class: "status-line error" }, serverBackups.error)
        : backups.length
          ? h(
              "div",
              { class: "project-panels" },
              groups.length > 1
                ? h("div", { class: "field project-search-field" }, h("label", { class: "field-label" }, h("span", {}, "搜索项目")), projectSearchInput)
                : null,
              h(
                "div",
                { class: "project-panel-list" },
                visibleGroups.length
                  ? visibleGroups.map((group) => projectPanel(group))
                  : h("div", { class: "empty compact" }, "没有匹配的项目")
              )
            )
          : h("div", { class: "empty compact" }, loadingBackups ? "正在读取服务器备份…" : "还没有加载服务器备份，点击“刷新项目库”。")
    );
  }

  function projectPanel(group) {
    const expanded = expandedKeys.has(group.key);
    const head = h(
      "button",
      { class: `project-panel-head ${expanded ? "expanded" : ""}`, type: "button", "aria-expanded": expanded ? "true" : "false" },
      h("span", { class: "project-panel-caret" }, expanded ? "▾" : "▸"),
      h("span", { class: "project-panel-name", title: group.rootsText || group.repoName }, group.repoName),
      h("span", { class: "project-panel-meta" }, `${group.backups.length} 份 · ${group.devices.size} 台设备 · 最新 ${formatDateTime(group.latestAt)}`)
    );
    head.addEventListener("click", () => {
      if (expandedKeys.has(group.key)) expandedKeys.delete(group.key);
      else expandedKeys.add(group.key);
      renderServerBackups();
    });
    return h(
      "div",
      { class: `project-panel ${expanded ? "expanded" : ""}` },
      head,
      expanded ? h("div", { class: "project-panel-body" }, renderProjectVersions(group)) : null
    );
  }

  function renderProjectVersions(group) {
    const hasFull = group.backups.some((backup) => asObject(backup).backup_kind === "full");
    return h(
      "div",
      { class: "project-version-stack" },
      group.rootsText
        ? h("div", { class: "project-roots mono cell-ellipsis", title: group.rootsText }, group.rootsText)
        : null,
      !hasFull
        ? h("div", { class: "status-line warn" }, "该项目当前列表里只有补丁备份。恢复到新电脑时，建议先选择一份完整基线；补丁只适合同一 Git 仓库继续应用。")
        : h("div", { class: "card-actions" }, actionButton("⭐ 一键恢复最新完整版", "btn-primary", () => restoreLatestProjectBackup(group))),
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "table" },
          h("thead", {}, h("tr", {}, h("th", {}, "版本"), h("th", {}, "设备"), h("th", {}, "分支/提交"), h("th", {}, "本机时间"), h("th", {}, "大小"), h("th", {}, "操作"))),
          h("tbody", {}, ...group.backups.map((backup) => backupRow(backup, previewProjectRestore, restoreProjectBackup)))
        )
      )
    );
  }

  function ensureExpanded() {
    const backups = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    const groups = buildProjectGroups(backups);
    const validKeys = new Set(groups.map((group) => group.key));
    for (const key of Array.from(expandedKeys)) {
      if (!validKeys.has(key)) expandedKeys.delete(key);
    }
    if (!expandedKeys.size && groups.length) expandedKeys.add(groups[0].key);
  }

  renderProjectStatus();
  renderAutoBackupStatus();
  renderServerBackups();
  refreshProjectStatus(false);
  refreshServerBackups(false);
  const unsubStatus = store.select((s) => s.status?.cwd, () => {
    if (!projectPathInput.value.trim()) {
      const status = store.getState().status || {};
      const rootPath = status.git?.root || status.cwd || "";
      projectPathInput.value = rootPath;
      selectedPath = rootPath;
      if (!restorePathInput.value.trim()) restorePathInput.value = rootPath;
    }
  });
  return () => {
    unsubStatus();
  };
}

function fact(label, value) {
  return h("div", { class: "result-fact" }, h("span", {}, label), h("b", {}, value || "-"));
}

function buildProjectGroups(items) {
  const map = new Map();
  for (const item of items) {
    const backup = asObject(item);
    const repoName = backup.repo_name || "unknown-project";
    const key = repoName.toLowerCase();
    if (!map.has(key)) {
      map.set(key, {
        key,
        repoName,
        backups: [],
        devices: new Set(),
        roots: new Set(),
        latestAt: "",
        latestMs: 0,
        rootsText: "",
      });
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
  const groups = Array.from(map.values());
  for (const group of groups) {
    group.backups.sort((a, b) => backupTimeMs(b) - backupTimeMs(a));
    const roots = Array.from(group.roots);
    group.rootsText = roots.slice(0, 3).join(" | ");
    if (roots.length > 3) group.rootsText += ` | 另 ${roots.length - 3} 个路径`;
  }
  groups.sort((a, b) => b.latestMs - a.latestMs || a.repoName.localeCompare(b.repoName));
  return groups;
}

function backupTimeMs(backup) {
  const raw = backup.received_at || backup.created_at || "";
  const text = String(raw || "").trim();
  if (!text) return 0;
  const normalized = /^\d{4}-\d{2}-\d{2}T/.test(text) && !/(Z|[+-]\d{2}:\d{2})$/.test(text) ? `${text}Z` : text;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : 0;
}

function projectGroupSearchText(group) {
  return [
    group.repoName,
    Array.from(group.devices).join(" "),
    Array.from(group.roots).join(" "),
  ].join(" ").toLowerCase();
}

function projectBackupKindLabel(item) {
  const backup = asObject(item);
  const mode = backup.source_mode || "";
  if (mode === "git_full") return "完整基线";
  if (mode === "filesystem") return "完整目录";
  if (mode === "git_patch") return "未提交改动";
  if (mode === "git_commit") return "提交补丁";
  return backup.backup_kind === "full" ? "完整备份" : backup.backup_kind === "patch" ? "增量补丁" : "-";
}

function backupRow(item, onPreview, onRestore) {
  const backup = asObject(item);
  const time = backup.received_at || backup.created_at || "";
  const previewBtn = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "预览");
  const restoreBtn = h("button", { class: "btn btn-primary btn-sm", type: "button" }, "恢复");
  previewBtn.addEventListener("click", () => onPreview(backup));
  restoreBtn.addEventListener("click", () => onRestore(backup));
  const branch = backup.branch || "-";
  const commit = backup.commit_sha ? shortId(backup.commit_sha) : "-";
  return h(
    "tr",
    {},
    h("td", {}, h("div", { class: "row row-wrap" }, h("span", { class: `badge ${backup.backup_kind === "full" ? "ok" : ""}` }, projectBackupKindLabel(backup)), h("span", { class: "mono", title: backup.id || "" }, shortId(backup.id)))),
    h("td", { class: "cell-ellipsis", title: backup.device_id || "" }, backup.device_id || "-"),
    h("td", { class: "cell-ellipsis", title: `${branch} / ${commit}` }, `${branch} / ${commit}`),
    h("td", { title: time ? `UTC: ${time}` : "" }, formatDateTime(time)),
    h("td", {}, formatBytes(backup.size_bytes)),
    h("td", {}, h("div", { class: "row-actions" }, previewBtn, restoreBtn))
  );
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
