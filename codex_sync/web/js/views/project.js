// 项目备份：指定项目快照上传到自建同步服务器。
import { h, asObject, shortId } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { consoleCard, makeRun, actionButton, formatDateTime } from "../ui.js";

export function mount(root, store) {
  const run = makeRun(store);
  const { card: outCard, setOutput } = consoleCard("项目备份详情");
  const projectBackupPageSize = 200;
  let projectStatus = asObject(store.getState().status?.git);
  let selectedPath = projectStatus.root || projectStatus.selected_path || store.getState().status?.cwd || "";
  let serverBackups = null;
  let selectedProjectKey = "";
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
  });
  const restorePathInput = h("input", {
    class: "input project-path-input",
    type: "text",
    placeholder: "输入要恢复到的本机目录；新目录可直接手动填写",
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
      ensureSelectedProject();
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
        setOutput(result);
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
      setOutput(serverBackups);
      showToast(next.length ? `已加载更多：${next.length} 份` : "没有更多项目备份", "success");
      ensureSelectedProject();
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

  const chooseRestoreDir = async () => {
    try {
      const result = await runAction("choose-project-dir", { project_path: restoreTargetPath() });
      setOutput(result);
      if (result?.success && result.path) {
        restorePathInput.value = result.path;
        showToast("已选择恢复目标文件夹", "success");
        renderServerBackups();
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

  const details = h(
    "div",
    { class: "grid grid-2" },
    h(
      "div",
      { class: "card" },
      h("div", { class: "card-title" }, "备份内容"),
      h("div", { class: "concept-hidden-list" },
        item("Git 项目", "手动上传保存完整安全文件快照；自动备份保存提交或工作区补丁。"),
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
    h("div", { class: "section grid grid-2" }, projectStatusCard, autoBackupCard),
    h("div", { class: "section" }, serverBackupsCard),
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
        fact("模式", valid ? (isRepo ? "完整基线 + 增量补丁" : "完整目录快照") : "-"),
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

  function renderAutoBackupStatus() {
    const state = asObject(autoBackupStatus);
    const isRepo = Boolean(state.is_repo);
    const enabled = Boolean(state.enabled_for_git_commit);
    const last = asObject(state.last);
    autoBackupCard.replaceChildren(
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "自动备份"),
        h("span", { class: `badge ${loadingAutoBackup ? "" : !isRepo ? "warn" : enabled ? "ok" : ""}` }, loadingAutoBackup ? "检查中" : !isRepo ? "仅 Git 项目" : enabled ? "已开启" : "未开启")
      ),
      h("p", { class: "card-desc" }, "为当前 Git 项目安装 post-commit hook：每次提交后入队并后台上传提交补丁，不阻塞提交。"),
      h(
        "div",
        { class: "result-facts" },
        fact("Git 提交触发", enabled ? "已安装" : "未安装"),
        fact("待处理队列", state.queue_count != null ? `${state.queue_count} 个` : "-"),
        fact("Codex 关闭触发", state.codex_stop_enabled ? "已在设置中开启" : "未开启"),
        fact("最近备份", formatDateTime(last.last_backup_at))
      ),
      !isRepo ? h("div", { class: "status-line warn" }, "自动项目备份当前只支持 Git 仓库。非 Git 目录请使用手动上传。") : null,
      h(
        "div",
        { class: "card-actions" },
        actionButton("安装提交后自动备份", "btn-primary", installGitAutoBackup),
        actionButton("关闭提交后自动备份", "btn-ghost", uninstallGitAutoBackup),
        actionButton("处理待备份队列", "btn-ghost", processAutoBackupQueue),
        actionButton("刷新自动备份状态", "btn-ghost", () => refreshAutoBackupStatus(true))
      )
    );
  }

  function renderServerBackups() {
    const backups = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    const groups = buildProjectGroups(backups);
    if (!selectedProjectKey && groups.length) selectedProjectKey = groups[0].key;
    const selected = groups.find((group) => group.key === selectedProjectKey) || groups[0] || null;
    const query = projectSearchInput.value.trim().toLowerCase();
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
      h("p", { class: "card-desc" }, "从云端项目列表选择项目，再选择备份版本恢复到这台电脑。完整基线可恢复到新目录；补丁备份需要目标目录是同一 Git 项目。"),
      h(
        "div",
        { class: "project-restore-controls" },
        h(
          "div",
          { class: "field project-restore-field" },
          h("label", { class: "field-label" }, h("span", {}, "恢复目标目录"), h("span", { class: "field-hint" }, "可手动输入新目录")),
          restorePathInput
        ),
        h("div", { class: "card-actions" },
          actionButton("选择目标文件夹", "btn-ghost", chooseRestoreDir),
          actionButton("使用当前项目目录", "btn-ghost", useCurrentAsRestoreTarget),
          actionButton("刷新项目库", "btn-primary", refreshServerBackups),
          serverBackupsHasMore ? actionButton(loadingMoreBackups ? "加载中…" : "加载更多", "btn-ghost", loadMoreServerBackups) : null
        )
      ),
      serverBackups?.error
        ? h("div", { class: "status-line error" }, serverBackups.error)
        : backups.length
          ? h(
              "div",
              { class: "project-library-grid" },
              h(
                "div",
                { class: "project-list-pane" },
                h("div", { class: "field" }, h("label", { class: "field-label" }, h("span", {}, "项目搜索")), projectSearchInput),
                h(
                  "div",
                  { class: "project-list" },
                  visibleGroups.length
                    ? visibleGroups.map((group) => projectGroupButton(group))
                    : h("div", { class: "empty compact" }, "没有匹配的项目")
                )
              ),
              h(
                "div",
                { class: "project-version-pane" },
                selected
                  ? renderProjectVersions(selected)
                  : h("div", { class: "empty compact" }, "请选择一个项目")
              )
            )
          : h("div", { class: "empty compact" }, loadingBackups ? "正在读取服务器备份…" : "还没有加载服务器备份，点击“刷新服务器备份”。")
    );
  }

  function projectGroupButton(group) {
    const active = group.key === selectedProjectKey;
    const button = h(
      "button",
      { class: `project-list-item ${active ? "active" : ""}`, type: "button" },
      h("strong", {}, group.repoName),
      h("span", {}, `${group.backups.length} 份 · ${group.devices.size} 台设备 · 最新 ${formatDateTime(group.latestAt)}`),
      h("small", { title: group.rootsText }, group.rootsText || "原始路径未知")
    );
    button.addEventListener("click", () => {
      selectedProjectKey = group.key;
      renderServerBackups();
    });
    return button;
  }

  function renderProjectVersions(group) {
    const hasFull = group.backups.some((backup) => asObject(backup).backup_kind === "full");
    return h(
      "div",
      { class: "project-version-stack" },
      h(
        "div",
        { class: "project-selected-summary" },
        h("div", {}, h("strong", {}, group.repoName), h("span", {}, `${group.backups.length} 份备份 · ${Array.from(group.devices).join("、") || "未知设备"}`)),
        h("div", { class: "mono cell-ellipsis", title: group.rootsText }, group.rootsText || "-")
      ),
      !hasFull
        ? h("div", { class: "status-line warn" }, "该项目当前列表里只有补丁备份。恢复到新电脑时，建议先选择一份完整基线；补丁只适合同一 Git 仓库继续应用。")
        : null,
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

  function ensureSelectedProject() {
    const backups = Array.isArray(serverBackups?.project_backups) ? serverBackups.project_backups : [];
    const groups = buildProjectGroups(backups);
    if (!groups.length) {
      selectedProjectKey = "";
      return;
    }
    if (!groups.some((group) => group.key === selectedProjectKey)) {
      selectedProjectKey = groups[0].key;
    }
  }

  renderProjectStatus();
  renderAutoBackupStatus();
  renderServerBackups();
  refreshProjectStatus(false);
  refreshServerBackups(false);
  setOutput(store.getState().console);
  const unsub = store.select((s) => s.console, () => setOutput(store.getState().console));
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
