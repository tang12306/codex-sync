// 对话备份与恢复：完整备份与本地灾难备份、云端备份、从备份导入对话、服务器留存。
import { h, asObject, shortId, fill } from "../dom.js";
import { runAction } from "../api.js";
import { showToast } from "../toast.js";
import { refreshStatus } from "../poller.js";
import { navigate } from "../router.js";
import { makeRun, actionButton, createSubTabs, serverMissingNotice } from "../ui.js";
import { confirmDialog, confirmCloseCodex } from "../dialog.js";
import { renderMessage } from "../markdown.js";
import { facts, failureReason, formatBytes, formatDateTime, formatEpoch, isFailed, loadingSpinner, statusLine } from "../format.js";

const CLOUD_PAGE = 12;
const LIST_PAGE = 100;
const PLAN_PREVIEW = 50;

const ENC_SOURCE_LABELS = { env: "环境变量", config: "口令", key_file: "私有密钥文件", generated_key_file: "自动生成的私有密钥文件" };
const SYNC_STATE_LABELS = { clean: "已同步", dirty: "有未上传改动", diverged: "已分叉", incremental: "增量", root: "基线" };
const RETENTION_REASONS = {
  "full backup count limit": "超出每个设备的保留份数",
  "full backup age limit": "超过保留天数",
  "project backup count limit": "超出每个项目的保留份数",
  "project backup age limit": "超过保留天数",
  "server total size limit": "超出服务器容量上限",
};
const RETENTION_WARNINGS = {
  "Retention could not reduce server storage below the total limit without deleting protected latest/head backups.":
    "即使清理掉所有可删除的备份，服务器占用仍高于容量上限（各设备、各项目最新的一份受保护，不会删除）。",
};

const RESTORE_NOTES = [
  "恢复前会自动创建一次本地灾难备份，出问题可以找回",
  "config.toml、AGENTS.md 等配置文件不会被覆盖",
  "加密备份需要本机设置了相同的加密口令，或已导入对应的密钥",
  "建议先关闭 Codex，避免恢复时文件被占用",
];

const FALLBACK_HOMES = [{ id: "windows", kind: "windows", label: "Windows", ok: true, has_codex: true }];

// 页签之间共用：切换页签时不必重新扫描环境、重新拉列表。
const backupCache = {
  homes: null,
  targetChannelsByHome: new Map(),
  cloud: null,
};

async function fetchHomes(force = false) {
  if (!force && Array.isArray(backupCache.homes)) return backupCache.homes;
  if (force) backupCache.targetChannelsByHome.clear();
  try {
    const result = await runAction("list-codex-homes");
    const homes = Array.isArray(result.homes) && result.homes.length ? result.homes : FALLBACK_HOMES;
    backupCache.homes = homes;
    return homes;
  } catch {
    return FALLBACK_HOMES;
  }
}

function usableHome(home) {
  return Boolean(home && home.ok && home.has_codex);
}

function pickHome(homes, preferred) {
  if (homes.some((home) => home.id === preferred && usableHome(home))) return preferred;
  return (homes.find(usableHome) || homes[0] || FALLBACK_HOMES[0]).id;
}

function homeLabelFor(homes, homeId) {
  const home = homes.find((item) => item.id === homeId);
  if (home) return home.label || home.id;
  if (String(homeId || "").startsWith("wsl:")) return `WSL · ${String(homeId).split(":", 2)[1]}`;
  return "Windows";
}

function backupEnvLabel(deviceId) {
  const device = String(deviceId || "");
  if (device.startsWith("wsl:")) return `WSL · ${device.slice(4) || "未知发行版"}`;
  return device ? `Windows · ${device}` : "Windows";
}

function serverConfigured(store) {
  return Boolean(store.getState().status?.config?.server_url);
}

function encryptionState(store) {
  const cfg = asObject(store.getState().status?.config);
  const enc = asObject(cfg.full_backup_encryption);
  return {
    enc,
    encrypted: enc.enabled !== false && Boolean(enc.configured),
    allowPlain: Boolean(cfg.full_backup_allow_plaintext_upload),
  };
}

// 只显示有值的条目，避免一排「-」。
function factsOf(pairs) {
  return facts(pairs.filter((pair) => pair && pair[1] != null && pair[1] !== ""));
}

function resultSection(title, ...children) {
  return h("div", { class: "result-section" }, h("div", { class: "result-section-head" }, h("strong", {}, title)), ...children);
}

export function mount(root, store, { sub } = {}) {
  const viewContainer = h("div");
  let unmountCurrent = null;
  let importPreset = "";

  const tabs = createSubTabs(
    [
      { key: "full", label: "完整备份" },
      { key: "cloud", label: "云端备份" },
      { key: "import", label: "从备份导入对话" },
      { key: "retention", label: "服务器留存" },
    ],
    { initial: sub, onChange: show, label: "备份页面分区" }
  );

  const ctx = {
    store,
    run: makeRun(store),
    // 云端备份行的「导入对话」：切到导入页并预选这个备份
    openImport(backupId) {
      importPreset = backupId || "";
      tabs.select("import");
    },
    takeImportPreset() {
      const id = importPreset;
      importPreset = "";
      return id;
    },
  };

  function show(key) {
    if (unmountCurrent) {
      try {
        unmountCurrent();
      } catch {
        /* ignore */
      }
      unmountCurrent = null;
    }
    viewContainer.replaceChildren();
    const mountTab = { full: mountFull, cloud: mountCloud, import: mountImport, retention: mountRetention }[key] || mountFull;
    unmountCurrent = mountTab(viewContainer, ctx) || null;
  }

  fill(root, tabs.el, viewContainer);
  show(tabs.active);

  return () => {
    if (unmountCurrent) unmountCurrent();
  };
}

// ==================== 完整备份 ====================
function mountFull(container, { store, run }) {
  let alive = true;
  let homes = Array.isArray(backupCache.homes) ? backupCache.homes : [];
  let selectedHome = pickHome(homes, "windows");

  const homeTabs = h("div", { class: "home-tabs", role: "group", "aria-label": "选择 Codex 环境" });
  const encInfo = h("div", { class: "enc-status" });
  const uploadBtn = actionButton("生成并上传", "btn-ghost", uploadSelected);
  const restoreBtn = actionButton("从云端恢复最新备份", "btn-danger", restoreSelected);
  const restoreDivider = h("span", { class: "divider", "aria-hidden": "true" });
  const resultWrap = h("div", { class: "result-stack", "aria-live": "polite" });
  const disasterResult = h("div", { class: "result-stack", "aria-live": "polite" });

  fill(container,
    h(
      "div",
      { class: "card section" },
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "完整对话备份"),
        h(
          "div",
          { class: "card-title-actions" },
          actionButton("重新检测环境", "btn-ghost btn-sm", () => loadHomes(true), { title: "重新扫描 Windows 和 WSL 里的 Codex" })
        )
      ),
      h(
        "p",
        { class: "card-desc" },
        "把所选环境里的全部 Codex 对话打包成一个备份包。本地备份包只保存在本机；上传到同步服务器后，可以在另一台电脑上恢复。"
      ),
      homeTabs,
      encInfo,
      h(
        "div",
        { class: "card-actions" },
        actionButton("生成本地备份包", "btn-primary", backupLocal),
        uploadBtn,
        actionButton("检查备份状态", "btn-ghost", checkSelected),
        restoreDivider,
        restoreBtn
      ),
      resultWrap
    ),
    h(
      "div",
      { class: "card section" },
      h("div", { class: "card-title" }, "本地灾难备份"),
      h(
        "p",
        { class: "card-desc" },
        "把本机 ~/.codex 的数据库和配置原样打包到 ~/.codex-sync/disaster-backups。不做脱敏、不上传，只留在本机，用于误操作后的兜底恢复。从云端恢复对话前也会自动创建一次。"
      ),
      h("div", { class: "card-actions" }, actionButton("立即创建灾难备份", "btn-ghost", createDisaster)),
      disasterResult
    )
  );

  function selected() {
    return homes.find((home) => home.id === selectedHome) || FALLBACK_HOMES[0];
  }

  function homeSubText(home) {
    if (!home.ok) return home.error || "无法访问";
    if (!home.has_codex) return "没有找到 ~/.codex";
    return home.current_provider ? `当前渠道 ${home.current_provider}` : "可以备份";
  }

  function renderHomes() {
    if (!homes.length) {
      fill(homeTabs, loadingSpinner("正在检测 Codex 环境（包括 WSL）…", { compact: true }));
      return;
    }
    fill(homeTabs,
      ...homes.map((home) => {
        const on = home.id === selectedHome;
        return h(
          "button",
          {
            class: `home-tab${on ? " active" : ""}`,
            type: "button",
            "aria-pressed": on ? "true" : "false",
            disabled: !usableHome(home),
            onClick: () => {
              if (home.id === selectedHome) return;
              selectedHome = home.id;
              resultWrap.replaceChildren();
              renderHomes();
              syncControls();
            },
          },
          h("span", { class: "home-tab-title" }, home.label || home.id),
          h("span", { class: "home-tab-sub" }, homeSubText(home))
        );
      })
    );
  }

  function syncControls() {
    const { enc, encrypted, allowPlain } = encryptionState(store);
    const toPolicy = h("button", { class: "link-btn", type: "button", onClick: () => navigate("settings", "policy") }, "去设置加密口令");
    if (encrypted) {
      fill(encInfo,
        h("span", { class: "badge ok" }, "已启用客户端加密"),
        h("span", { class: "enc-meta" }, `密钥来源：${ENC_SOURCE_LABELS[enc.source] || enc.source || "-"}`),
        h("span", { class: "enc-meta mono" }, `指纹：${enc.key_id || "-"}`),
        h("div", { class: "enc-hint" }, "上传前会在本机加密，服务器看不到内容。另一台电脑设置相同的口令（指纹一致）后即可解密恢复。")
      );
    } else {
      fill(encInfo,
        h("span", { class: "badge warn" }, enc.enabled === false ? "客户端加密已关闭" : "尚未设置加密口令"),
        h(
          "div",
          { class: "enc-hint" },
          allowPlain
            ? "当前允许明文上传：备份包会以明文保存在服务器上，建议设置加密口令。 "
            : "设置加密口令后才能把完整备份上传到服务器。 ",
          toPolicy
        )
      );
    }

    // 未加密且不允许明文时不提供上传入口，说明文字里已给出去处。
    uploadBtn.hidden = !encrypted && !allowPlain;
    uploadBtn.classList.toggle("btn-ghost", encrypted);
    uploadBtn.classList.toggle("btn-danger", !encrypted);
    uploadBtn.textContent = encrypted ? "生成并上传" : "生成并明文上传";
    uploadBtn.title = encrypted ? "生成备份包，加密后上传到同步服务器" : "备份包未加密，会以明文上传到同步服务器";

    const wsl = selected().kind === "wsl";
    restoreBtn.textContent = wsl ? "恢复最近的本地备份" : "从云端恢复最新备份";
    restoreBtn.hidden = !wsl && !serverConfigured(store);
    restoreDivider.hidden = restoreBtn.hidden;
    restoreBtn.title = wsl ? "用本机保存的最近一个 WSL 备份包覆盖该发行版的对话数据" : "下载同步服务器上最新的完整备份，覆盖本机对话数据";
  }

  async function loadHomes(force = false) {
    if (force) {
      homes = [];
      renderHomes();
    }
    homes = await fetchHomes(force);
    if (!alive) return;
    selectedHome = pickHome(homes, selectedHome);
    renderHomes();
    syncControls();
    if (force) showToast("Codex 环境已重新检测", "success");
  }

  function showResult(title, ...children) {
    fill(resultWrap, resultSection(title, ...children));
  }

  function packageFacts(result) {
    const pkg = asObject(result.package && typeof result.package === "object" ? result.package : result);
    return factsOf([
      ["备份 ID", pkg.backup_id || pkg.id ? shortId(pkg.backup_id || pkg.id) : null],
      ["文件数", pkg.included_count],
      ["数据量", pkg.included_bytes ? formatBytes(pkg.included_bytes) : null],
      ["备份包位置", pkg.archive || pkg.path],
    ]);
  }

  function describeBackup(result, label, upload) {
    const pkg = asObject(result.package && typeof result.package === "object" ? result.package : result);
    const unchanged = pkg.created === false;
    if (upload) {
      if (!result.uploaded) return `${label} 的对话没有变化，云端已是最新`;
      return unchanged ? `${label} 的对话没有变化，已上传最近的备份包` : `${label} 完整备份已生成并上传`;
    }
    return unchanged ? `${label} 的对话没有变化，沿用最近的备份包` : `${label} 本地备份包已生成`;
  }

  async function runBackup(upload) {
    const home = selected();
    const label = home.label || home.id;
    const { encrypted } = encryptionState(store);
    const common = { upload, allow_plaintext_upload: upload && !encrypted };
    const result =
      home.kind === "wsl"
        ? await run("wsl-full-backup", {
            payload: { distro: home.distro, include_config: true, include_memories: true, ...common },
            label: "生成备份包",
            okMsg: (r) => describeBackup(r, label, upload),
          })
        : await run("full-backup-now", {
            payload: common,
            label: "生成备份包",
            okMsg: (r) => describeBackup(r, label, upload),
          });
    if (!alive || isFailed(result)) return;
    showResult(upload ? "生成并上传" : "生成本地备份包", packageFacts(result));
  }

  function backupLocal() {
    return runBackup(false);
  }

  async function uploadSelected() {
    if (!serverConfigured(store)) {
      showToast("还没有配置同步服务器", "warning", { action: { label: "去配置", onClick: () => navigate("settings", "server") } });
      return;
    }
    const { encrypted, allowPlain } = encryptionState(store);
    if (!encrypted) {
      if (!allowPlain) return;
      const home = selected();
      const ok = await confirmDialog({
        title: "以明文上传完整备份？",
        message: `${home.label || home.id} 的完整备份包含全部对话内容，当前没有加密。上传后，能访问这台服务器的人都可以直接读取这些内容。`,
        details: ["设置加密口令后，之后的上传会自动加密", "只在你完全信任这台服务器时才使用明文上传"],
        confirmLabel: "明文上传",
        danger: true,
      });
      if (!ok) return;
    }
    await runBackup(true);
  }

  async function checkSelected() {
    const home = selected();
    showResult("备份状态", loadingSpinner("正在检查…", { compact: true }));
    try {
      if (home.kind === "wsl") {
        const res = await runAction("wsl-status");
        if (!alive) return;
        const distro = (Array.isArray(res.distros) ? res.distros : []).find((item) => item.distro === home.distro);
        if (!distro) {
          showResult("备份状态", statusLine(res.error || `没有找到发行版 ${home.distro}`, "error"));
          return;
        }
        showResult(
          `${home.label} 状态`,
          factsOf([
            ["发行版", distro.distro],
            ["用户主目录", distro.home],
            ["~/.codex", distro.has_codex ? "存在" : "不存在"],
            ["config.toml", distro.has_config ? "存在" : "没有"],
            ["AGENTS.md", distro.has_agents ? "存在" : "没有"],
          ]),
          distro.error || distro.warning ? statusLine(distro.error || distro.warning, distro.error ? "error" : "warn") : null
        );
        return;
      }
      const res = await runAction("full-backup-status");
      if (!alive) return;
      const local = asObject(res.local);
      const remote = asObject(res.remote);
      const device = asObject(remote.device_state);
      if (isFailed(local)) {
        showResult("备份状态", statusLine(`读取本机状态失败：${failureReason(local)}`, "error"));
        return;
      }
      const remoteOk = remote.success !== false && !remote.error;
      showResult(
        "Windows 备份状态",
        factsOf([
          ["本机对话", local.needs_upload ? "有改动尚未上传" : "与上次上传一致"],
          ["备份范围", `${local.included_count ?? "-"} 个文件 · ${formatBytes(local.included_bytes)}`],
          ["备份分支", local.branch_id],
          ["云端状态", remoteOk ? (remote.device_state ? SYNC_STATE_LABELS[device.sync_state] || device.sync_state || "-" : "还没有备份") : null],
          ["云端最新备份", remoteOk && device.head_backup_id ? shortId(device.head_backup_id) : null],
          ["云端上次备份", remoteOk && device.last_backup_at ? formatDateTime(device.last_backup_at) : null],
        ]),
        remoteOk ? null : statusLine(`无法读取云端状态：${failureReason(remote)}`, "warn")
      );
    } catch (e) {
      if (alive) showResult("备份状态", statusLine(`检查失败：${e.message || e}`, "error"));
    }
  }

  async function restoreSelected() {
    const home = selected();
    if (home.kind === "wsl") {
      const ok = await confirmDialog({
        title: `恢复 ${home.label} 的最近备份？`,
        message: "会用本机保存的最近一个 WSL 完整备份包，覆盖该发行版 ~/.codex 里的对话数据。",
        details: ["config.toml 等配置文件不会被覆盖", "建议先关闭该 WSL 里正在运行的 Codex"],
        confirmLabel: "开始恢复",
        danger: true,
      });
      if (!ok) return;
      const result = await run("wsl-restore-latest", {
        payload: { distro: home.distro, restore_config: false },
        label: "恢复备份",
        okMsg: (r) => `已恢复 ${r.restored_count ?? 0} 个文件到 ${home.label}`,
      });
      if (alive && !isFailed(result)) showResult("恢复结果", restoreFacts(result));
      return;
    }
    if (!serverConfigured(store)) {
      showToast("还没有配置同步服务器", "warning", { action: { label: "去配置", onClick: () => navigate("settings", "server") } });
      return;
    }
    const ok = await confirmDialog({
      title: "从云端恢复最新备份？",
      message: "会从同步服务器下载最新的完整对话备份，覆盖本机 ~/.codex 里的对话数据。适合换到另一台电脑后接着工作。",
      details: RESTORE_NOTES,
      confirmLabel: "下载并恢复",
      danger: true,
    });
    if (!ok) return;
    const result = await run("restore-latest-full-backup", {
      payload: { restore_config: false },
      label: "恢复云端备份",
      okMsg: (r) => `已恢复 ${r.restored_count ?? 0} 个文件（来自 ${backupEnvLabel(r.selected?.device_id)}）`,
    });
    if (alive && !isFailed(result)) showResult("恢复结果", restoreFacts(result));
  }

  async function createDisaster() {
    const result = await run("preflight-backup", {
      payload: { force: true, reason: "web_manual" },
      label: "创建灾难备份",
      okMsg: (r) => (r.created === false ? `没有创建：${r.reason || "条件不满足"}` : "灾难备份已创建"),
    });
    if (!alive || isFailed(result) || result.created === false) return;
    fill(disasterResult,
      resultSection(
        "灾难备份",
        factsOf([
          ["文件数", result.included_count],
          ["数据量", result.included_bytes ? formatBytes(result.included_bytes) : null],
          ["跳过", Array.isArray(result.skipped) && result.skipped.length ? `${result.skipped.length} 个过大或无法读取的文件` : null],
          ["备份包位置", result.archive],
        ])
      )
    );
  }

  renderHomes();
  syncControls();
  loadHomes(false);
  const unsubscribe = store.select((s) => s.status?.config, syncControls);

  return () => {
    alive = false;
    unsubscribe();
  };
}

function restoreFacts(result) {
  const source = asObject(result.selected);
  const time = source.received_at || source.created_at;
  return factsOf([
    ["恢复文件数", result.restored_count],
    ["来源", source.device_id ? backupEnvLabel(source.device_id) : null],
    ["备份时间", time ? formatDateTime(time) : null],
    ["跳过", Array.isArray(result.skipped) && result.skipped.length ? `${result.skipped.length} 个（配置文件等）` : null],
    ["恢复前的灾难备份", asObject(result.preflight_backup).archive],
  ]);
}

// ==================== 云端备份 ====================
function mountCloud(container, { store, openImport }) {
  let alive = true;
  let data = backupCache.cloud;
  let loading = false;
  let showAll = false;

  const card = h("div", { class: "card section" });
  fill(container, card);

  async function load({ notify = false } = {}) {
    if (!serverConfigured(store)) {
      render();
      return;
    }
    loading = true;
    render();
    let result;
    try {
      result = await runAction("list-full-backups");
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive) return;
    loading = false;
    data = result;
    if (!isFailed(result)) backupCache.cloud = result;
    render();
    if (notify) {
      if (isFailed(result)) showToast(`读取云端备份失败：${failureReason(result)}`, "error");
      else showToast("云端备份列表已刷新", "success");
    }
  }

  function render() {
    const backups = Array.isArray(data?.full_backups) ? data.full_backups : [];
    const failed = Boolean(data) && isFailed(data);
    let badge = null;
    if (loading) badge = h("span", { class: "badge" }, "读取中");
    else if (failed) badge = h("span", { class: "badge danger" }, "读取失败");
    else if (data) badge = h("span", { class: "badge info" }, `${backups.length} 个`);

    let body;
    if (!serverConfigured(store)) body = serverMissingNotice("还没有配置同步服务器，无法读取云端备份。");
    else if (failed) body = statusLine(`读取云端备份失败：${failureReason(data)}`, "error");
    else if (!data) body = loadingSpinner("正在读取云端备份…");
    else if (!backups.length) body = h("div", { class: "empty compact" }, "服务器上还没有完整备份。可以在「完整备份」页生成并上传。");
    else {
      const visible = showAll ? backups : backups.slice(0, CLOUD_PAGE);
      body = h(
        "div",
        {},
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "table" },
            h(
              "thead",
              {},
              h("tr", {}, h("th", {}, "来源"), h("th", {}, "上传时间"), h("th", {}, "大小"), h("th", {}, "状态"), h("th", {}, "ID"), h("th", {}, "操作"))
            ),
            h("tbody", {}, ...visible.map(row))
          )
        ),
        backups.length > CLOUD_PAGE
          ? h(
              "div",
              { class: "list-more" },
              h(
                "button",
                {
                  class: "btn btn-ghost btn-sm",
                  type: "button",
                  onClick: () => {
                    showAll = !showAll;
                    render();
                  },
                },
                showAll ? "只显示最新 12 个" : `显示全部（共 ${backups.length} 个）`
              )
            )
          : null
      );
    }

    fill(card,
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "云端完整备份"),
        h("div", { class: "card-title-actions" }, badge, actionButton("刷新", "btn-ghost btn-sm", () => load({ notify: true })))
      ),
      h(
        "p",
        { class: "card-desc" },
        "同步服务器上保存的完整对话备份，最新的在最前面。可以把某个 Windows 备份整体恢复到本机，也可以只挑其中几个对话导入。"
      ),
      body
    );
  }

  function row(item) {
    const backup = asObject(item);
    const time = backup.received_at || backup.created_at || "";
    const state = backup.sync_state || (backup.parent_backup_id ? "incremental" : "root");
    const fromWsl = String(backup.device_id || "").startsWith("wsl:");
    return h(
      "tr",
      {},
      h(
        "td",
        { class: "cell-ellipsis", title: backup.device_id || "" },
        h("div", {}, backupEnvLabel(backup.device_id)),
        h("div", { class: "cell-sub" }, backup.branch_id || "-")
      ),
      h("td", { title: time ? `UTC ${time}` : "" }, formatDateTime(time)),
      h("td", {}, formatBytes(backup.size_bytes)),
      h(
        "td",
        {},
        h(
          "div",
          { class: "badge-row" },
          h("span", { class: `badge ${backup.encrypted ? "ok" : "warn"}` }, backup.encrypted ? "已加密" : "明文"),
          h("span", { class: "badge" }, SYNC_STATE_LABELS[state] || state)
        )
      ),
      h("td", { class: "mono", title: backup.id || "" }, shortId(backup.id)),
      h(
        "td",
        {},
        h(
          "div",
          { class: "row-actions" },
          h(
            "button",
            { class: "btn btn-ghost btn-sm", type: "button", title: "挑选这个备份里的对话导入", onClick: () => openImport(backup.id) },
            "导入对话"
          ),
          fromWsl ? null : actionButton("恢复到本机", "btn-ghost btn-sm", () => restoreBackup(backup), { title: "用这个备份整体覆盖本机对话" })
        )
      )
    );
  }

  async function restoreBackup(backup) {
    const ok = await confirmDialog({
      title: "用这个备份覆盖本机对话？",
      message: `会下载 ${backupEnvLabel(backup.device_id)} 在 ${formatDateTime(backup.received_at || backup.created_at)} 上传的完整备份，覆盖本机 ~/.codex 里的对话数据。`,
      details: RESTORE_NOTES,
      confirmLabel: "下载并恢复",
      danger: true,
    });
    if (!ok) return;
    try {
      const downloaded = await runAction("download-full-backup", { backup_id: backup.id });
      if (isFailed(downloaded)) {
        showToast(`下载备份失败：${failureReason(downloaded)}`, "error");
        return;
      }
      const result = await runAction("restore-full-backup", { archive: downloaded.path, confirm_backup_id: backup.id, restore_config: false });
      if (isFailed(result)) showToast(`恢复失败：${failureReason(result)}`, "error");
      else showToast(`已恢复 ${result.restored_count ?? 0} 个文件。恢复前的数据已另存为灾难备份。`, "success");
    } catch (e) {
      showToast(`恢复失败：${e.message || e}`, "error");
    } finally {
      refreshStatus(store);
    }
  }

  render();
  load();

  return () => {
    alive = false;
  };
}

// ==================== 从备份导入对话 ====================
function mountImport(container, { store, takeImportPreset }) {
  let alive = true;
  let homes = Array.isArray(backupCache.homes) ? backupCache.homes : [];
  let backups = null;
  let convos = null;
  let detail = null;
  let detailRow = null;
  let curBackupId = takeImportPreset();
  let targetHome = pickHome(homes, "windows");
  let target = "";
  let search = "";
  let shown = LIST_PAGE;
  let listSeq = 0;
  let detailSeq = 0;
  let searchTimer = 0;
  const selected = new Set();

  const backupSel = h("select", { class: "input", "aria-label": "选择备份包" });
  const searchInput = h("input", { class: "input", type: "search", placeholder: "在备份里搜索标题、首条消息或项目路径", "aria-label": "搜索备份里的对话" });
  const homeSel = h("select", { class: "input", "aria-label": "导入到哪个环境" });
  const targetSel = h("select", { class: "input", "aria-label": "导入到哪个渠道" });
  const importBtn = actionButton("导入所选对话", "btn-primary btn-sm", doImport);
  const noticeWrap = h("div");
  const batchWrap = h("div", { class: "batch-bar" });
  const listHead = h("div", { class: "conv-pane-head" });
  const listBody = h("div", { class: "conv-pane-body" });
  const detailPane = h("div", { class: "conv-pane" });

  fill(container,
    h(
      "div",
      { class: "card section" },
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "从备份导入对话"),
        h("div", { class: "card-title-actions" }, actionButton("刷新备份列表", "btn-ghost btn-sm", () => loadBackups({ notify: true })))
      ),
      h(
        "p",
        { class: "card-desc" },
        "选一个备份包，勾选需要的对话，再选择导入到哪个环境和渠道。云端备份会先下载到本机缓存，第一次打开可能需要一点时间。"
      ),
      h("div", { class: "filter-bar" }, backupSel, searchInput),
      noticeWrap
    ),
    h("div", { class: "section" }, batchWrap),
    h("div", { class: "conv-layout" }, h("div", { class: "conv-pane" }, listHead, listBody), detailPane)
  );

  backupSel.addEventListener("change", () => {
    curBackupId = backupSel.value;
    loadConvos();
  });
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      search = searchInput.value.trim().toLowerCase();
      shown = LIST_PAGE;
      renderList();
    }, 200);
  });
  homeSel.addEventListener("change", () => {
    targetHome = homeSel.value || "windows";
    target = "";
    // 「本机已有 / 比本机新」取决于目标环境，换环境后重新比对，保留已勾选的对话
    if (curBackupId) loadConvos({ keepSelection: true });
    else fillTargets();
  });
  targetSel.addEventListener("change", () => {
    target = targetSel.value;
  });

  const homeLabel = (id) => homeLabelFor(homes, id);

  function backupItems() {
    return backups && !isFailed(backups) && Array.isArray(backups.backups) ? backups.backups : [];
  }

  function backupOptionLabel(item) {
    const source = item.source === "server" ? "云端" : item.source === "wsl" ? "WSL 本地" : "本机";
    const parts = [formatDateTime(item.created_at), backupEnvLabel(item.device_id), source];
    if (item.size_bytes) parts.push(formatBytes(item.size_bytes));
    if (item.source === "server" && item.downloaded) parts.push("已下载");
    return parts.join(" · ");
  }

  function renderBackupOptions() {
    const items = backupItems();
    let placeholder = "正在读取备份列表…";
    if (backups) placeholder = items.length ? `选择备份包（共 ${items.length} 个）` : "没有可导入的备份包";
    fill(backupSel, h("option", { value: "" }, placeholder), ...items.map((item) => h("option", { value: item.backup_id }, backupOptionLabel(item))));
    backupSel.value = items.some((item) => item.backup_id === curBackupId) ? curBackupId : "";

    const notices = [];
    if (backups && isFailed(backups)) notices.push(statusLine(`读取备份列表失败：${failureReason(backups)}`, "error"));
    else if (backups?.remote_error && !serverConfigured(store)) notices.push(serverMissingNotice("还没有配置同步服务器，只列出了本机已有的备份。"));
    else if (backups?.remote_error) notices.push(statusLine(`无法连接同步服务器，只列出了本机已有的备份：${backups.remote_error}`, "warn"));
    fill(noticeWrap, ...notices);
  }

  async function loadBackups({ notify = false } = {}) {
    backupSel.disabled = true;
    let result;
    try {
      result = await runAction("list-importable-backups");
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive) return;
    backups = result;
    backupSel.disabled = false;
    if (curBackupId && !backupItems().some((item) => item.backup_id === curBackupId)) {
      showToast("可导入列表里没有找到所选的备份包，它可能已被清理", "warning");
      curBackupId = "";
      resetConvos();
    }
    renderBackupOptions();
    if (notify) {
      if (isFailed(result)) showToast(`读取备份列表失败：${failureReason(result)}`, "error");
      else showToast("备份列表已刷新", "success");
    }
  }

  function renderHomeOptions() {
    targetHome = pickHome(homes, targetHome);
    fill(homeSel,
      ...homes.map((home) =>
        h("option", { value: home.id, disabled: !usableHome(home) }, `${home.label || home.id}${usableHome(home) ? "" : "（不可用）"}`)
      )
    );
    homeSel.value = targetHome;
  }

  async function loadHomes() {
    homes = await fetchHomes(false);
    if (!alive) return;
    renderHomeOptions();
  }

  async function loadTargetChannels(homeId) {
    if (backupCache.targetChannelsByHome.has(homeId)) return backupCache.targetChannelsByHome.get(homeId);
    const result = await runAction("list-target-channels", { target_home: homeId });
    if (!isFailed(result)) backupCache.targetChannelsByHome.set(homeId, result);
    return result;
  }

  function uniqueProviders(items) {
    return [...new Set((items || []).filter((p) => p && p !== "(unknown)"))].sort();
  }

  async function fillTargets() {
    const homeId = targetHome;
    let local;
    try {
      local = await loadTargetChannels(homeId);
    } catch (e) {
      local = { success: false, error: String(e.message || e) };
    }
    if (!alive || homeId !== targetHome) return;
    const localOk = local && !isFailed(local);
    const current = (localOk && local.current_provider) || convos?.current_provider || "";
    const localProviders = uniqueProviders([current, ...(localOk && Array.isArray(local.channels) ? local.channels : []).map((c) => c.provider)]);
    const backupProviders = uniqueProviders((convos?.conversations || []).map((c) => c.model_provider)).filter((p) => !localProviders.includes(p));

    const groups = [];
    if (localProviders.length) {
      groups.push(
        h(
          "optgroup",
          { label: `${homeLabel(homeId)} 的渠道` },
          ...localProviders.map((p) => h("option", { value: p }, p === current ? `${p}（当前）` : p))
        )
      );
    }
    if (backupProviders.length) {
      groups.push(h("optgroup", { label: "备份里的其他渠道" }, ...backupProviders.map((p) => h("option", { value: p }, p))));
    }
    const all = [...localProviders, ...backupProviders];
    fill(targetSel, ...(groups.length ? groups : [h("option", { value: "" }, "没有检测到渠道")]));
    if (!all.includes(target)) target = current || all[0] || "";
    targetSel.value = target;
  }

  function resetConvos() {
    convos = null;
    detail = null;
    detailRow = null;
    selected.clear();
    shown = LIST_PAGE;
    renderList();
    renderDetail();
    renderBatch();
  }

  async function loadConvos({ keepSelection = false } = {}) {
    const seq = (listSeq += 1);
    if (!keepSelection) {
      selected.clear();
      detail = null;
      detailRow = null;
      shown = LIST_PAGE;
      renderDetail();
    }
    if (!curBackupId) {
      resetConvos();
      fillTargets();
      return;
    }
    convos = null;
    fill(listHead, h("span", {}, "备份里的对话"));
    fill(listBody, loadingSpinner("正在读取备份包（云端备份会先下载到本机）…"));
    renderBatch();
    let result;
    try {
      result = await runAction("list-backup-conversations", { backup_id: curBackupId, target_home: targetHome });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive || seq !== listSeq) return;
    convos = result;
    if (keepSelection && !isFailed(result)) {
      const importable = new Set((result.conversations || []).filter((c) => c.has_body).map((c) => c.id));
      for (const id of [...selected]) if (!importable.has(id)) selected.delete(id);
    }
    await fillTargets();
    if (!alive || seq !== listSeq) return;
    renderList();
    renderBatch();
  }

  function allConvos() {
    return convos && !isFailed(convos) && Array.isArray(convos.conversations) ? convos.conversations : [];
  }

  function filteredConvos() {
    const list = allConvos();
    if (!search) return list;
    return list.filter((c) => `${c.title || ""}\n${c.first_user_message || ""}\n${c.preview || ""}\n${c.cwd || ""}`.toLowerCase().includes(search));
  }

  function renderList() {
    if (!curBackupId) {
      fill(listHead, h("span", {}, "备份里的对话"));
      fill(listBody, h("div", { class: "empty" }, "先在上方选择一个备份包"));
      return;
    }
    if (!convos) return; // 加载中，保留转圈
    if (isFailed(convos)) {
      fill(listHead, h("span", {}, "备份里的对话"));
      fill(listBody, statusLine(`读取备份失败：${failureReason(convos)}`, "error"));
      return;
    }
    const all = allConvos();
    const list = filteredConvos();
    const fresh = all.filter((c) => c.has_body && !c.present).length;
    fill(listHead,
      h("span", {}, search ? `匹配 ${list.length} / ${all.length} 个对话` : `共 ${all.length} 个对话`),
      h("span", {}, `可新增 ${fresh} 个`)
    );
    if (!list.length) {
      fill(listBody, h("div", { class: "empty" }, all.length ? "没有匹配的对话" : "这个备份里没有对话"));
      return;
    }
    const visible = list.slice(0, shown);
    const rest = list.length - visible.length;
    fill(listBody,
      ...visible.map(convRow),
      rest > 0
        ? h(
            "div",
            { class: "list-more" },
            h(
              "button",
              {
                class: "btn btn-ghost btn-sm",
                type: "button",
                onClick: () => {
                  shown += LIST_PAGE;
                  renderList();
                },
              },
              `显示更多（还有 ${rest} 个）`
            )
          )
        : null
    );
  }

  function convStatusBadge(c) {
    if (!c.has_body) return h("span", { class: "badge warn", title: "备份里没有这个对话的正文，无法导入" }, "缺少正文");
    if (!c.present) return h("span", { class: "badge ok" }, "本机没有");
    if (c.newer) return h("span", { class: "badge info", title: "备份里的版本比目标环境里的更新" }, "比本机新");
    return h("span", { class: "badge" }, "本机已有");
  }

  function convRow(c) {
    const box = h("input", { type: "checkbox", "aria-label": `选择 ${c.title || c.id}`, disabled: !c.has_body });
    box.checked = selected.has(c.id);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(c.id);
      else selected.delete(c.id);
      renderBatch();
    });
    const preview = c.first_user_message || c.preview || "";
    return h(
      "div",
      { class: `conv-row${detailRow?.id === c.id ? " active" : ""}`, dataset: { id: c.id } },
      box,
      h(
        "div",
        { class: "conv-main" },
        h("button", { class: "conv-title", type: "button", title: c.title || "", onClick: () => openDetail(c) }, c.title || "(无标题)"),
        h(
          "div",
          { class: "conv-meta" },
          h("span", { class: "badge" }, c.model_provider || "未知渠道"),
          convStatusBadge(c),
          c.archived ? h("span", { class: "badge" }, "已归档") : null,
          c.updated_at ? h("span", {}, formatEpoch(c.updated_at)) : null
        ),
        preview ? h("div", { class: "conv-preview", title: preview }, preview) : null
      )
    );
  }

  function markActive() {
    for (const el of listBody.querySelectorAll(".conv-row")) el.classList.toggle("active", el.dataset.id === detailRow?.id);
  }

  async function openDetail(c) {
    detailRow = c;
    markActive();
    const seq = (detailSeq += 1);
    fill(detailPane, h("div", { class: "conv-pane-body" }, loadingSpinner("正在读取对话正文…")));
    let result;
    try {
      result = await runAction("read-backup-conversation", { backup_id: curBackupId, thread_id: c.id });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive || seq !== detailSeq) return;
    detail = result;
    renderDetail();
  }

  function renderDetail() {
    if (!detail || !detailRow) {
      fill(detailPane, h("div", { class: "conv-pane-body" }, h("div", { class: "empty" }, "点左侧的对话标题查看正文")));
      return;
    }
    if (isFailed(detail)) {
      fill(detailPane, h("div", { class: "conv-pane-body" }, statusLine(`读取正文失败：${failureReason(detail)}`, "error")));
      return;
    }
    const t = asObject(detail.thread);
    const messages = (detail.messages || []).map(renderMessage);
    const cwd = t.cwd || detailRow.cwd;
    fill(detailPane,
      h(
        "div",
        { class: "detail-head" },
        h("div", { class: "detail-title" }, t.title || detailRow.title || "(无标题)"),
        h(
          "div",
          { class: "detail-meta" },
          [t.model_provider || detailRow.model_provider, `${messages.length} 条消息`, formatEpoch(t.updated_at || detailRow.updated_at)].filter(Boolean).join(" · ")
        ),
        cwd ? h("div", { class: "detail-meta mono", title: cwd }, cwd) : null
      ),
      h(
        "div",
        { class: "conv-pane-body" },
        h(
          "div",
          { class: "msg-list" },
          ...(messages.length ? messages : [h("div", { class: "empty compact" }, "这个对话没有可显示的消息")]),
          detail.truncated ? statusLine("正文较长，只显示了前面一部分。", "info") : null
        )
      )
    );
  }

  function renderBatch() {
    const importable = filteredConvos().filter((c) => c.has_body);
    fill(batchWrap,
      h("span", { class: "batch-count" }, `已选 ${selected.size}`),
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm",
          type: "button",
          disabled: !importable.length,
          onClick: () => {
            for (const c of importable) selected.add(c.id);
            renderList();
            renderBatch();
          },
        },
        search ? `全选匹配的（${importable.length}）` : `全选（${importable.length}）`
      ),
      h(
        "button",
        {
          class: "btn btn-ghost btn-sm",
          type: "button",
          disabled: !selected.size,
          onClick: () => {
            selected.clear();
            renderList();
            renderBatch();
          },
        },
        "清空选择"
      ),
      h("span", { class: "divider", "aria-hidden": "true" }),
      h("span", { class: "batch-label" }, "导入到"),
      homeSel,
      h("span", { class: "batch-label" }, "渠道"),
      targetSel,
      importBtn
    );
  }

  async function doImport() {
    if (!curBackupId) {
      showToast("请先选择备份包", "warning");
      return;
    }
    if (!selected.size) {
      showToast("请先勾选要导入的对话", "warning");
      return;
    }
    const ids = [...selected];
    const payload = { backup_id: curBackupId, thread_ids: ids, target: target || targetSel.value || "", target_home: targetHome };
    try {
      let result = await runAction("import-conversations", payload);
      if (result && result.needs_close) {
        if (!(await confirmCloseCodex({ wsl: targetHome.startsWith("wsl:") }))) return;
        result = await runAction("import-conversations", { ...payload, close_codex: true });
      }
      if (!alive) return;
      if (isFailed(result)) {
        showToast(`导入失败：${failureReason(result)}`, "error");
        return;
      }
      showToast(`已导入 ${result.imported ?? ids.length} 个对话到 ${homeLabel(targetHome)} · ${result.target || payload.target || "当前渠道"}`, "success");
      backupCache.targetChannelsByHome.delete(targetHome);
      await loadConvos();
    } catch (e) {
      showToast(`导入失败：${e.message || e}`, "error");
    } finally {
      refreshStatus(store);
    }
  }

  renderHomeOptions();
  renderBackupOptions();
  renderList();
  renderDetail();
  renderBatch();
  (async () => {
    await Promise.all([loadHomes(), loadBackups()]);
    if (!alive) return;
    if (curBackupId) loadConvos();
    else fillTargets();
  })();

  return () => {
    alive = false;
    clearTimeout(searchTimer);
  };
}

// ==================== 服务器留存 ====================
function mountRetention(container, { store }) {
  let alive = true;
  let data = null;
  let loading = false;

  const body = h("div", { class: "result-stack", "aria-live": "polite" });
  fill(container,
    h(
      "div",
      { class: "card section" },
      h(
        "div",
        { class: "card-title" },
        h("span", {}, "服务器留存与清理"),
        h("div", { class: "card-title-actions" }, actionButton("刷新", "btn-ghost btn-sm", () => load({ notify: true })))
      ),
      h(
        "p",
        { class: "card-desc" },
        "服务器每次收到新备份后，会按下面的策略自动清理旧备份；每个设备、每个项目最新的一份始终保留。这里可以查看当前占用，也可以立即清理超出策略的备份。"
      ),
      body,
      h("div", { class: "card-actions" }, actionButton("立即清理旧备份", "btn-danger", prune))
    )
  );

  function keepText(count, days, unit) {
    const keep = Number(count) > 0 ? `每个${unit}保留 ${count} 份` : "不限份数";
    const age = Number(days) > 0 ? `最长 ${days} 天` : "不限时间";
    return `${keep}，${age}`;
  }

  function planTable(planned) {
    const visible = planned.slice(0, PLAN_PREVIEW);
    return h(
      "details",
      { class: "collapse" },
      h("summary", {}, `查看可清理的备份（${planned.length} 个）`),
      h(
        "div",
        { class: "collapse-body" },
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "table" },
            h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "来源"), h("th", {}, "上传时间"), h("th", {}, "大小"), h("th", {}, "原因"))),
            h(
              "tbody",
              {},
              ...visible.map((item) => {
                const full = item.kind === "full_backup";
                const time = item.received_at || item.created_at;
                return h(
                  "tr",
                  {},
                  h("td", {}, full ? "完整备份" : "项目备份"),
                  h(
                    "td",
                    { class: "cell-ellipsis", title: item.device_id || "" },
                    full ? backupEnvLabel(item.device_id) : item.repo_name || "-",
                    full ? null : h("div", { class: "cell-sub" }, item.device_id || "-")
                  ),
                  h("td", {}, formatDateTime(time)),
                  h("td", {}, formatBytes(item.size_bytes)),
                  h("td", {}, (item.reasons || []).map((reason) => RETENTION_REASONS[reason] || reason).join("；") || "-")
                );
              })
            )
          )
        ),
        planned.length > visible.length ? h("div", { class: "table-msg" }, `只列出前 ${PLAN_PREVIEW} 个，共 ${planned.length} 个。`) : null
      )
    );
  }

  function render() {
    if (!serverConfigured(store)) {
      fill(body, serverMissingNotice("还没有配置同步服务器。"));
      return;
    }
    if (loading && !data) {
      fill(body, loadingSpinner("正在读取服务器留存状态…"));
      return;
    }
    if (!data) {
      body.replaceChildren();
      return;
    }
    if (isFailed(data)) {
      fill(body, statusLine(`读取留存状态失败：${failureReason(data)}`, "error"));
      return;
    }
    const policy = asObject(data.policy);
    const full = asObject(policy.full_backups);
    const project = asObject(policy.project_backups);
    const usage = asObject(data.usage);
    const planned = Array.isArray(data.planned) ? data.planned : [];
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    fill(body,
      facts([
        ["完整备份保留", keepText(full.keep_per_device_branch, full.max_age_days, "设备")],
        ["项目备份保留", keepText(project.keep_per_repo_device, project.max_age_days, "项目")],
        ["服务器容量上限", Number(policy.server_total_max_bytes) > 0 ? formatBytes(policy.server_total_max_bytes) : "不限"],
        ["当前占用", `${formatBytes(usage.backup_bytes)}（完整 ${usage.full_backup_count || 0} 个 · 项目 ${usage.project_backup_count || 0} 个）`],
        ["受保护的最新备份", `${usage.protected_count || 0} 个`],
        ["可清理", planned.length ? `${planned.length} 个 · ${formatBytes(data.planned_bytes)}` : "没有"],
      ]),
      warnings.length ? statusLine(warnings.map((w) => RETENTION_WARNINGS[w] || w).join("；"), "warn") : null,
      planned.length ? planTable(planned) : null
    );
  }

  async function load({ notify = false } = {}) {
    if (!serverConfigured(store)) {
      render();
      return;
    }
    loading = true;
    render();
    let result;
    try {
      result = await runAction("server-retention-status");
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive) return;
    loading = false;
    data = result;
    render();
    if (notify) {
      if (isFailed(result)) showToast(`读取留存状态失败：${failureReason(result)}`, "error");
      else showToast("留存状态已刷新", "success");
    }
  }

  // 先做一次预演拿到清理清单，确认后再真正删除。
  async function prune() {
    if (!serverConfigured(store)) {
      showToast("还没有配置同步服务器", "warning", { action: { label: "去配置", onClick: () => navigate("settings", "server") } });
      return;
    }
    let preview;
    try {
      preview = await runAction("server-retention-prune", { dry_run: true });
    } catch (e) {
      showToast(`读取清理计划失败：${e.message || e}`, "error");
      return;
    }
    if (!alive) return;
    data = preview;
    render();
    if (isFailed(preview)) {
      showToast(`读取清理计划失败：${failureReason(preview)}`, "error");
      return;
    }
    const planned = Array.isArray(preview.planned) ? preview.planned : [];
    if (!planned.length) {
      showToast("没有超出留存策略的备份，不需要清理", "info");
      return;
    }
    const fullCount = planned.filter((item) => item.kind === "full_backup").length;
    const projectCount = planned.length - fullCount;
    const ok = await confirmDialog({
      title: "清理服务器上的旧备份？",
      message: `将从服务器永久删除 ${planned.length} 个备份，释放约 ${formatBytes(preview.planned_bytes)}。删除后无法找回。`,
      details: [
        fullCount ? `完整对话备份 ${fullCount} 个` : null,
        projectCount ? `项目备份 ${projectCount} 个` : null,
        "每个设备、每个项目最新的一份不会被删除",
      ],
      confirmLabel: "永久删除",
      danger: true,
    });
    if (!ok || !alive) return;
    try {
      const result = await runAction("server-retention-prune", { dry_run: false });
      const deleted = Number(result.deleted_count || 0);
      if (isFailed(result)) {
        showToast(
          deleted ? `部分备份没有删掉：已删除 ${deleted} 个，其余失败（${failureReason(result, "服务器删除文件失败")}）` : `清理失败：${failureReason(result)}`,
          "error"
        );
      } else {
        showToast(`已删除 ${deleted} 个旧备份，释放 ${formatBytes(result.deleted_bytes)}`, "success");
      }
    } catch (e) {
      showToast(`清理失败：${e.message || e}`, "error");
    }
    await load();
  }

  render();
  load();

  return () => {
    alive = false;
  };
}
