// 系统设置与高级页面：整合服务器配置、备份策略、自动化自动化和高级诊断。
// 采用 display 控制面板切换，确保未保存的编辑在切换 Tab 时不丢失。
import { h, asObject, shortId } from "../dom.js";
import { saveConfig, runAction } from "../api.js";
import { refreshStatus, fetchSnapshots } from "../poller.js";
import { showToast } from "../toast.js";
import { consoleCard, makeRun, actionButton } from "../ui.js";
import { openSnapshot } from "./drawer.js";

function loadingSpinner(text = "加载中…") {
  return h("div", { class: "spinner-wrap" }, h("span", { class: "spinner" }), h("span", {}, text));
}

function setIfUnfocused(el, value) {
  if (!el || document.activeElement === el) return;
  if (el.type === "checkbox") el.checked = Boolean(value);
  else el.value = value == null ? "" : value;
}

function fact(label, value) {
  return h("div", { class: "result-fact" }, h("span", {}, label), h("b", {}, value == null || value === "" ? "-" : String(value)));
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function toUnit(value, divisor) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return "";
  const out = n / divisor;
  return Number.isInteger(out) ? String(out) : String(Number(out.toFixed(2)));
}

function fromUnit(value, multiplier, fallback) {
  const raw = String(value ?? "").trim();
  if (raw === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? Math.round(n * multiplier) : fallback;
}

export function mount(root, store) {
  const run = makeRun(store);
  const { card: outCard, setOutput } = consoleCard("系统设置详情输出");
  
  let currentTab = "server"; // server | policy | automation | diagnosis
  let dirty = false; // 用户是否修改了表单
  const markDirty = () => { dirty = true; };
  const bindDirty = (el) => {
    el.addEventListener("input", markDirty);
    el.addEventListener("change", markDirty);
    return el;
  };

  const f = {};
  const input = (key, attrs = {}) => (f[key] = bindDirty(h("input", { class: "input", ...attrs })));
  const field = (key, label, hint, attrs = {}) =>
    h(
      "div",
      { class: "field" },
      h(
        "label",
        { class: "field-label" },
        h("span", {}, label),
        hint ? h("span", { class: "field-hint" }, hint) : null
      ),
      input(key, attrs)
    );
  const toggle = (key, title, desc) => {
    const cb = (f[key] = bindDirty(h("input", { type: "checkbox" })));
    return h(
      "div",
      { class: "switch-row" },
      h("div", { class: "switch-text" }, h("b", {}, title), h("span", {}, desc)),
      h("label", { class: "switch" }, cb, h("span", { class: "slider" }))
    );
  };

  // ==================== PANEL 1: 服务器与部署 ====================
  const apiToken = (f.api_token = bindDirty(h("input", { class: "input", type: "password", placeholder: "留空保持不变" })));
  const toggleToken = h("button", { class: "input-affix", type: "button" }, "显示");
  toggleToken.addEventListener("click", () => {
    const show = apiToken.type === "password";
    apiToken.type = show ? "text" : "password";
    toggleToken.textContent = show ? "隐藏" : "显示";
  });

  const connCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "服务器连接配置"),
    field("server_url", "Sync Server URL", "远程同步服务地址", { type: "text", placeholder: "https://sync.example.com" }),
    h(
      "div",
      { class: "field" },
      h(
        "label",
        { class: "field-label" },
        h("span", {}, "API Token"),
        h("span", { class: "field-hint" }, "Bearer 身份验证令牌")
      ),
      h("div", { class: "input-wrap" }, apiToken, toggleToken)
    ),
    field("device_id", "Device ID", "这台机器在云端的唯一设备标识", { type: "text" })
  );

  const { card: deployOut, setOutput: setDeployOut } = consoleCard("部署终端输出");
  const deployFields = {};
  const deployInput = (key, attrs = {}) => (deployFields[key] = h("input", { class: "input", ...attrs }));
  const deployField = (key, label, hint, attrs = {}) =>
    h(
      "div",
      { class: "field" },
      h(
        "label",
        { class: "field-label" },
        h("span", {}, label),
        hint ? h("span", { class: "field-hint" }, hint) : null
      ),
      deployInput(key, attrs)
    );
  const deployNginx = (deployFields.nginx_enabled = h("input", { type: "checkbox" }));
  const deployPassword = deployInput("ssh_password", { type: "password", placeholder: "只用于本次操作，不保存" });
  const deployPasswordToggle = h("button", { class: "input-affix", type: "button" }, "显示");
  deployPasswordToggle.addEventListener("click", () => {
    const show = deployPassword.type === "password";
    deployPassword.type = show ? "text" : "password";
    deployPasswordToggle.textContent = show ? "隐藏" : "显示";
  });
  let compatData = null;
  let compatLoading = false;
  const compatBody = h("div", { class: "result-stack" });
  let appUpdateData = null;
  let appUpdateLoading = false;
  const appUpdateBody = h("div", { class: "result-stack" });

  function splitSshTarget(target) {
    const text = String(target || "").trim();
    if (!text) return { user: "root", host: "" };
    const at = text.lastIndexOf("@");
    if (at > 0) return { user: text.slice(0, at), host: text.slice(at + 1) };
    return { user: "", host: text };
  }

  function composeSshTarget() {
    const user = deployFields.ssh_user.value.trim();
    const host = deployFields.ssh_host.value.trim();
    if (!host) return "";
    return user ? `${user}@${host}` : host;
  }

  function deployPayload(includePassword = true) {
    const host = deployFields.ssh_host.value.trim();
    const nginxName = deployFields.nginx_server_name.value.trim();
    return {
      ssh_target: composeSshTarget(),
      ssh_port: Number(deployFields.ssh_port.value) || 22,
      ssh_password: includePassword ? deployFields.ssh_password.value : "",
      remote_dir: deployFields.remote_dir.value.trim() || "/opt/codex-sync-server",
      service_name: deployFields.service_name.value.trim() || "codex-sync-server",
      python: deployFields.python.value.trim() || "python3",
      bind_host: deployFields.bind_host.value.trim() || "127.0.0.1",
      bind_port: Number(deployFields.bind_port.value) || 8888,
      data_dir: deployFields.data_dir.value.trim() || "/var/lib/codex-sync",
      token_file: deployFields.token_file.value.trim() || "/etc/codex-sync/server-token",
      max_body_bytes: fromUnit(deployFields.max_body_mb.value, 1024 * 1024, 536870912),
      nginx_enabled: Boolean(deployFields.nginx_enabled.checked),
      nginx_server_name: nginxName || (deployFields.nginx_enabled.checked ? host : ""),
      client_max_body_size: deployFields.client_max_body_size.value.trim() || "512m",
    };
  }

  function syncDeployFields(cfg = {}) {
    const data = asObject(cfg);
    const target = splitSshTarget(data.ssh_target || "");
    setIfUnfocused(deployFields.ssh_user, target.user);
    setIfUnfocused(deployFields.ssh_host, target.host);
    setIfUnfocused(deployFields.ssh_port, data.ssh_port || 22);
    setIfUnfocused(deployFields.remote_dir, data.remote_dir || "/opt/codex-sync-server");
    setIfUnfocused(deployFields.service_name, data.service_name || "codex-sync-server");
    setIfUnfocused(deployFields.python, data.python || "python3");
    setIfUnfocused(deployFields.bind_host, data.bind_host || "127.0.0.1");
    setIfUnfocused(deployFields.bind_port, data.bind_port || 8888);
    setIfUnfocused(deployFields.data_dir, data.data_dir || "/var/lib/codex-sync");
    setIfUnfocused(deployFields.token_file, data.token_file || "/etc/codex-sync/server-token");
    setIfUnfocused(deployFields.max_body_mb, toUnit(data.max_body_bytes || 536870912, 1024 * 1024));
    setIfUnfocused(deployFields.nginx_server_name, data.nginx_server_name || "");
    setIfUnfocused(deployFields.client_max_body_size, data.client_max_body_size || "512m");
    deployFields.nginx_enabled.checked = Boolean(data.nginx_enabled);
  }

  async function loadDeployConfig() {
    try {
      const result = await runAction("deploy-config");
      syncDeployFields(result.config);
    } catch (e) {
      setDeployOut({ error: String(e.message || e) });
    }
  }

  async function saveDeployConfig() {
    try {
      const result = await runAction("save-deploy-config", deployPayload(false));
      setDeployOut(result);
      showToast("部署配置已保存", "success");
    } catch (e) {
      setDeployOut({ error: String(e.message || e) });
      showToast(String(e.message || e), "error");
    }
  }

  async function checkServerCompatibility() {
    compatLoading = true;
    renderCompatibility();
    try {
      compatData = await runAction("server-compatibility");
      if (compatData.needs_update) showToast("服务器端需要更新部署", "warning");
      else if (compatData.compatible) showToast("服务器版本兼容", "success");
      else showToast(compatData.error || "服务器版本检查失败", "error");
    } catch (e) {
      compatData = { success: false, compatible: false, error: String(e.message || e) };
      showToast(String(e.message || e), "error");
    } finally {
      compatLoading = false;
      renderCompatibility();
    }
  }

  async function checkAppUpdate(force = false, notify = true) {
    appUpdateLoading = true;
    renderAppUpdate();
    try {
      appUpdateData = await runAction("app-update-check", { force });
      if (notify) {
        if (appUpdateData.update_available) showToast(`发现 Codex Sync ${appUpdateData.latest_version}，可下载新版安装包`, "warning");
        else if (appUpdateData.success) showToast("本地应用已经是最新版本", "success");
        else showToast(appUpdateData.error || "应用更新检查失败", "error");
      }
    } catch (e) {
      appUpdateData = { success: false, error: String(e.message || e) };
      if (notify) showToast(String(e.message || e), "error");
    } finally {
      appUpdateLoading = false;
      renderAppUpdate();
    }
  }

  const deployCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "云端服务器一键部署"),
    h("p", { class: "card-desc" }, "通过 SSH 自动安装/升级 sync_server.py 至远程云主机。密码仅用于本次操作，不写入配置文件。"),
    h(
      "div",
      { class: "grid grid-2" },
      deployField("ssh_user", "用户名", "通常使用 root；使用 SSH 配置别名时可留空", { type: "text", placeholder: "root" }),
      deployField("ssh_host", "服务器 IP / 域名", "云服务器公网 IP、域名，或 SSH 配置别名", { type: "text", placeholder: "1.2.3.4" }),
      h(
        "div",
        { class: "field" },
        h("label", { class: "field-label" }, h("span", {}, "密码"), h("span", { class: "field-hint" }, "只用于本次操作；留空则使用 SSH key")),
        h("div", { class: "input-wrap" }, deployPassword, deployPasswordToggle)
      )
    ),
    h(
      "details",
      { class: "collapse" },
      h("summary", {}, "高级选项"),
      h(
        "div",
        { class: "collapse-body grid grid-2" },
        deployField("ssh_port", "SSH 端口", "默认 22", { type: "number", min: "1", step: "1" }),
        deployField("remote_dir", "远程程序目录", "sync_server.py 安装位置", { type: "text" }),
        deployField("service_name", "systemd 服务名", "会生成 <name>.service", { type: "text" }),
        deployField("python", "远程 Python", "例如 python3 或 /usr/bin/python3", { type: "text" }),
        deployField("bind_host", "监听地址", "无 nginx 且要公网访问时用 0.0.0.0", { type: "text" }),
        deployField("bind_port", "监听端口", "默认 8888", { type: "number", min: "1", step: "1" }),
        deployField("data_dir", "数据目录", "服务器端备份和数据库位置", { type: "text" }),
        deployField("token_file", "Token 文件", "服务器 API Token 存储位置", { type: "text" }),
        deployField("max_body_mb", "上传大小上限（MB）", "默认 512 MB", { type: "number", min: "1", step: "1" }),
        deployField("client_max_body_size", "Nginx 上传上限", "启用 nginx 时使用，例如 512m", { type: "text" }),
        h("div", { class: "switch-row" },
          h("div", { class: "switch-text" }, h("b", {}, "启用 nginx 反代"), h("span", {}, "安装 /api/ 反向代理到本地服务")),
          h("label", { class: "switch" }, deployNginx, h("span", { class: "slider" }))
        ),
        deployField("nginx_server_name", "Nginx 域名/IP", "启用 nginx 时默认使用上面的服务器 IP", { type: "text", placeholder: "sync.example.com" })
      )
    ),
    h(
      "div",
      { class: "card-actions" },
      actionButton("保存部署配置", "btn-ghost", saveDeployConfig),
      actionButton("查看远程服务状态", "btn-ghost", async () => {
        try {
          setDeployOut({ loading: true, message: "正在与远程服务器建立 SSH 握手并拉取 Systemd 服务状态…" });
          setDeployOut(await runAction("deploy-status", deployPayload(true)));
        } catch (e) {
          setDeployOut({ error: String(e.message || e) });
        }
      }),
      actionButton("检查服务器版本", "btn-ghost", checkServerCompatibility),
      actionButton("一键安装服务器", "btn-primary", async () => {
        if (!window.confirm("将通过 SSH 在远程服务器创建目录、安装 systemd 服务、生成 token 并启动同步服务。确认开始安装？")) return;
        setDeployOut(await run("deploy-install", { payload: { ...deployPayload(true), save_config: true, backfill_local: true }, okMsg: "服务器已安装并已回填本地连接配置", errMsg: "安装失败", label: "服务器安装" }));
        await refreshStatus(store);
        await checkServerCompatibility();
      }),
      actionButton("一键更新部署", "btn-primary", async () => {
        if (!window.confirm("将把本地 sync_server.py 编译并推送到已配置的远程服务器上重启。确认开始更新？")) return;
        setDeployOut(await run("deploy-update", { payload: { ...deployPayload(true), save_config: true }, okMsg: "远程服务已成功升级", errMsg: "部署失败", label: "服务器部署", refresh: false }));
        await checkServerCompatibility();
      })
    ),
    compatBody,
    deployOut
  );

  const downloadUpdateBtn = actionButton("下载新版安装包", "btn-primary", async () => {
    const result = await run("app-update-download", {
      payload: { force: false },
      okMsg: "新版安装包已下载",
      errMsg: "下载失败",
      label: "应用更新",
      refresh: false,
    });
    appUpdateData = result;
    renderAppUpdate();
  });
  const openUpdateBtn = actionButton("打开 GitHub 发布页", "btn-ghost", async () => {
    await run("app-update-open", {
      payload: { force: false },
      okMsg: "已打开 GitHub 发布页",
      errMsg: "打开失败",
      label: "应用更新",
      refresh: false,
    });
  });

  const appUpdateCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "本地应用更新"),
    h("p", { class: "card-desc" }, "检查 GitHub Release 中的 Windows 安装包。这里只负责提示和下载，不会在运行中替换当前 EXE。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("检查应用更新", "btn-ghost", () => checkAppUpdate(true, true)),
      downloadUpdateBtn,
      openUpdateBtn
    ),
    appUpdateBody
  );

  let retentionData = null;
  let retentionLoading = false;
  const retentionBody = h("div", { class: "result-stack" });

  async function loadServerRetention() {
    retentionLoading = true;
    renderRetention();
    try {
      retentionData = await runAction("server-retention-status");
      showToast(retentionData.success === false ? "服务器留存策略读取失败" : "服务器留存策略已更新", retentionData.success === false ? "error" : "success");
    } catch (e) {
      retentionData = { success: false, error: String(e.message || e) };
      showToast(String(e.message || e), "error");
    } finally {
      retentionLoading = false;
      renderRetention();
    }
  }

  async function pruneServerRetention() {
    if (!window.confirm("将删除服务器端超出留存策略的完整备份、项目备份和轻量快照。每个设备/项目会保留最新入口。确认执行？")) return;
    retentionLoading = true;
    renderRetention();
    try {
      retentionData = await runAction("server-retention-prune", { dry_run: false });
      const n = Number(retentionData.deleted_count || 0);
      showToast(`服务器清理完成：删除 ${n} 项`, retentionData.success === false ? "error" : "success");
    } catch (e) {
      retentionData = { success: false, error: String(e.message || e) };
      showToast(String(e.message || e), "error");
    } finally {
      retentionLoading = false;
      renderRetention();
    }
  }

  const retentionCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "服务器留存与自动清理"),
    h("p", { class: "card-desc" }, "服务器上传后会按策略自动清理；这里可以预览当前会删什么，也可以手动立即执行。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("查看清理预览", "btn-ghost", loadServerRetention),
      actionButton("立即执行清理", "btn-danger", pruneServerRetention)
    ),
    retentionBody
  );

  const panelServer = h("div", { class: "tab-panel" }, 
    h("div", { class: "grid grid-2" }, connCard, deployCard),
    h("div", { class: "grid grid-2", style: { marginTop: "20px" } }, appUpdateCard, retentionCard)
  );

  // ==================== PANEL 2: 备份策略 ====================
  const policyCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "数据同步与灾备策略"),
    toggle("disaster_backup_enabled", "灾难级自动保护", "执行敏感写入前，先将本地全部核心数据冷备份到 disaster-backups"),
    toggle("full_backup_enabled", "完整对话备份开关", "允许把包含正文的完整对话打包（用于迁移接力）"),
    toggle("full_backup_include_config", "完整包包含配置", "把本地 Agent 的预设和全局系统配置打包归档"),
    toggle("full_backup_include_memories", "完整包包含记忆库", "把本地 Agent 的 memories 长期记忆目录一并打包"),
    toggle("full_backup_allow_plaintext_upload", "允许云端明文上传", "⚠ 安全警告：在客户端加密未就绪时，允许将包含正文的完整 zip 包直接上传云端"),
    toggle("project_auto_backup_on_codex_stop", "Codex 关闭时入队项目备份", "可选：Stop hook 只记录待备份任务，后台再上传当前 Git 项目")
  );

  const advancedCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "备份细节与参数设定"),
    field("sync_interval_minutes", "Daemon 同步轮询周期（分钟）", "守护进程的后台事件上传周期", { type: "number", min: "1", step: "1" }),
    field("max_untracked_copy_mb", "项目文件单体大小上限（MB）", "项目备份中，超过此大小的未跟踪文件将被跳过", { type: "number", min: "0", step: "1" }),
    field("disaster_backup_min_interval_hours", "灾难备份冷冻周期（小时）", "多长时间内仅允许自动创建一次灾难备份，防止 IO 开销", { type: "number", min: "0", step: "1" }),
    field("full_backup_quiet_minutes", "完整备份安静期时长（分钟）", "会话内容停止变化后，等待多久再生成本地大包", { type: "number", min: "0", step: "1" }),
    field("project_auto_backup_min_interval_minutes", "项目自动备份最短间隔（分钟）", "用于 Git 提交和 Codex Stop 触发，避免频繁上传", { type: "number", min: "0", step: "1" }),
    field("full_backup_retention_count", "本地备份留存数量限制", "本地 full_backups 最大保留包数，0表示不限制", { type: "number", min: "0" }),
    field("full_backup_retention_max_gb", "本地备份留存容量限制（GB）", "本地 full_backups 最大允许占用的磁盘空间，0 表示不限制", { type: "number", min: "0", step: "0.5" })
  );

  const panelPolicy = h("div", { class: "tab-panel" }, 
    h("div", { class: "grid grid-2" }, policyCard, advancedCard)
  );

  // ==================== PANEL 3: 任务与 Hooks ====================
  const daemonBadge = h("span", { class: "badge" }, "…");
  const hooksBadge = h("span", { class: "badge" }, "…");

  const daemonCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "后台守护进程 (Daemon)"), daemonBadge),
    h("p", { class: "card-desc" }, "以前台循环或终端挂载的形式运行：按周期同步轻量快照、补发失败快照、监控内容 digest。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("启动 Daemon", "btn-primary", () => run("start-daemon", { okMsg: "Daemon 已在后台线程启动" })),
      actionButton("停止 Daemon", "btn-ghost", () => run("stop-daemon", { okMsg: "已发送停止 Daemon 信号" }))
    )
  );

  const hooksCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "Codex 编辑器 Hooks"), hooksBadge),
    h("p", { class: "card-desc" }, "在 ~/.codex/hooks.json 中安装或升级同步钩子。使 Codex 执行 Prompt / Compact 时可触发实时事件同步。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("写入 / 更新 Hooks", "btn-primary", () => run("install-hooks", { okMsg: "Hooks 配置文件已写入" }))
    )
  );

  const taskStatusLine = h("p", { class: "card-desc" }, "点「刷新任务状态」查询计划任务信息。");
  const minutesInput = h("input", { class: "input input-narrow", type: "number", min: "1", value: "3" });

  async function refreshTaskStatus() {
    try {
      const r = await runAction("task-status");
      if (!r || !r.installed) {
        taskStatusLine.textContent = "Windows 定时同步任务：当前未安装";
      } else {
        taskStatusLine.textContent =
          `定时任务：已安装（当前 ${r.status || "-"}） · 下次执行 ${r.next_run || "-"} · 上次返回结果 ${r.last_result || "-"} · 运行频率 ${r.schedule || "-"}`;
      }
    } catch (e) {
      taskStatusLine.textContent = "无法获取定时任务状态：" + (e.message || e);
    }
  }

  const taskCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "Windows 系统定时任务"),
    h("p", { class: "card-desc" }, "在 Windows 中注册一个 schtasks 定时计划任务。每隔若干分钟自动运行一次 `sync-now`，同步脏状态，且完全静默不影响编辑器。"),
    taskStatusLine,
    h(
      "div",
      { class: "card-actions" },
      h("label", { class: "checkbox-control" }, h("span", {}, "同步周期（分钟）"), minutesInput),
      actionButton("安装定时任务", "btn-primary", async () => {
        await run("install-task", { payload: { minutes: Math.max(1, Number(minutesInput.value) || 3) }, okMsg: "Windows 计划任务已部署" });
        await refreshTaskStatus();
      }),
      actionButton("注销定时任务", "btn-ghost", async () => {
        await run("uninstall-task", { okMsg: "Windows 计划任务已注销" });
        await refreshTaskStatus();
      }),
      actionButton("刷新任务状态", "btn-ghost", refreshTaskStatus)
    )
  );

  const panelAutomation = h("div", { class: "tab-panel" }, 
    h("div", { class: "grid grid-2" }, daemonCard, hooksCard),
    h("div", { class: "section", style: { marginTop: "20px" } }, taskCard)
  );

  // ==================== PANEL 4: 高级同步诊断 ====================
  const diagCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "手动同步控制"),
    h("p", { class: "card-desc" }, "直接控制同步引擎上传 Outbox 失败项，补发 Dirty 通知，或生成接续诊断 Prompt。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("立即同步", "btn-primary", () => run("sync-now", { okMsg: "同步流程执行完毕" })),
      actionButton("补发 Outbox 队列", "btn-ghost", () => run("flush-outbox", { okMsg: "Outbox 补发队列发送完毕" })),
      actionButton("补发 Dirty 通知", "btn-ghost", () => run("notify-change", { okMsg: "已强制推送脏通知" })),
      actionButton("生成接续 Prompt", "btn-ghost", () => run("resume", { refresh: false, okMsg: "本地接续上下文 Prompt 已写入" })),
      actionButton("模拟捕获 Hook 事件", "btn-ghost", () => run("test-capture", { okMsg: "已向 stdin 管道注入测试 Hook 数据" }))
    )
  );

  const tableBody = h("tbody", {});
  const snapCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "云端轻量快照历史"), actionButton("刷新快照列表", "btn-ghost btn-sm", () => fetchSnapshots(store, { force: true }))),
    h("p", { class: "card-desc" }, "快照仅记录最近的 cwd、Git 补丁大小与最近活跃时间，主要用于工作现场的紧急诊断。"),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, "发送设备"),
            h("th", {}, "项目路径"),
            h("th", {}, "上传时间"),
            h("th", {}, "快照 ID"),
            h("th", {}, "诊断操作")
          )
        ),
        tableBody
      )
    )
  );

  const panelDiagnosis = h("div", { class: "tab-panel" }, 
    h("div", { class: "section" }, diagCard),
    h("div", { class: "section" }, snapCard)
  );

  // ==================== TAB 切换逻辑 ====================
  const tabBtnContainer = h("div", { class: "sub-tabs" });
  const tabs = [
    { id: "server", label: "服务器与部署", panel: panelServer },
    { id: "policy", label: "备份参数策略", panel: panelPolicy },
    { id: "automation", label: "定时任务与 Hooks", panel: panelAutomation },
    { id: "diagnosis", label: "高级故障诊断", panel: panelDiagnosis }
  ];

  function switchTab(tabId) {
    currentTab = tabId;
    renderTabs();
    tabs.forEach(t => {
      if (t.id === tabId) {
        t.panel.style.display = "block";
      } else {
        t.panel.style.display = "none";
      }
    });

    if (tabId === "server") {
      if (!compatData && !compatLoading) checkServerCompatibility().catch(() => {});
      if (!appUpdateData && !appUpdateLoading) checkAppUpdate(false, false).catch(() => {});
    } else if (tabId === "automation") {
      refreshTaskStatus();
    } else if (tabId === "diagnosis") {
      fetchSnapshots(store, { force: false }).catch(() => {});
    }
  }

  function renderTabs() {
    tabBtnContainer.replaceChildren(
      ...tabs.map(t => h("button", {
        class: `sub-tab-btn ${t.id === currentTab ? "active" : ""}`,
        type: "button",
        onClick: () => switchTab(t.id)
      }, t.label))
    );
  }

  // ==================== 保存表单 ====================
  const saveBtn = h("button", { class: "btn btn-primary", type: "button" }, "保存所有配置修改");
  saveBtn.addEventListener("click", doSave);

  async function doSave() {
    const existing = asObject(store.getState().status?.config);
    const num = (key, def) => {
      const raw = f[key].value.trim();
      if (raw === "") return def;
      const v = Number(raw);
      return Number.isFinite(v) ? v : def;
    };
    const payload = {
      server_url: f.server_url.value.trim(),
      device_id: f.device_id.value.trim(),
      sync_interval_seconds: fromUnit(f.sync_interval_minutes.value, 60, 180),
      max_untracked_copy_bytes: fromUnit(f.max_untracked_copy_mb.value, 1024 * 1024, 262144),
      disaster_backup_enabled: f.disaster_backup_enabled.checked,
      disaster_backup_min_interval_seconds: fromUnit(f.disaster_backup_min_interval_hours.value, 60 * 60, 86400),
      full_backup_enabled: f.full_backup_enabled.checked,
      full_backup_include_config: f.full_backup_include_config.checked,
      full_backup_include_memories: f.full_backup_include_memories.checked,
      full_backup_allow_plaintext_upload: f.full_backup_allow_plaintext_upload.checked,
      project_auto_backup_on_codex_stop: f.project_auto_backup_on_codex_stop.checked,
      full_backup_quiet_seconds: fromUnit(f.full_backup_quiet_minutes.value, 60, 60),
      project_auto_backup_min_interval_seconds: fromUnit(f.project_auto_backup_min_interval_minutes.value, 60, 600),
      full_backup_retention_count: num("full_backup_retention_count", 20),
      full_backup_retention_max_bytes: fromUnit(f.full_backup_retention_max_gb.value, 1024 * 1024 * 1024, 2147483648),
    };

    const token = f.api_token.value.trim();
    if (token || !existing.api_token_configured) payload.api_token = token;

    saveBtn.disabled = true;
    try {
      await saveConfig(payload);
      f.api_token.value = "";
      dirty = false; // 重置 dirty
      await refreshStatus(store);
      showToast("系统配置已成功保存", "success");
    } catch (e) {
      showToast(String(e.message || e), "error");
    } finally {
      saveBtn.disabled = false;
    }
  }

  // ==================== 装载结构 ====================
  root.replaceChildren(
    tabBtnContainer,
    panelServer,
    panelPolicy,
    panelAutomation,
    panelDiagnosis,
    h("div", { class: "form-actions", style: { marginTop: "20px" } }, saveBtn),
    h("div", { class: "section", style: { marginTop: "20px" } }, outCard)
  );

  // --- 初始化同步与状态刷新 ---
  const syncFields = () => {
    if (dirty) return; // 编辑中不覆盖
    const cfg = asObject(store.getState().status?.config);
    setIfUnfocused(f.server_url, cfg.server_url);
    setIfUnfocused(f.device_id, cfg.device_id);
    setIfUnfocused(f.sync_interval_minutes, toUnit(cfg.sync_interval_seconds, 60));
    setIfUnfocused(f.max_untracked_copy_mb, toUnit(cfg.max_untracked_copy_bytes, 1024 * 1024));
    setIfUnfocused(f.disaster_backup_enabled, cfg.disaster_backup_enabled);
    setIfUnfocused(f.disaster_backup_min_interval_hours, toUnit(cfg.disaster_backup_min_interval_seconds, 60 * 60));
    setIfUnfocused(f.full_backup_enabled, cfg.full_backup_enabled);
    setIfUnfocused(f.full_backup_include_config, cfg.full_backup_include_config);
    setIfUnfocused(f.full_backup_include_memories, cfg.full_backup_include_memories);
    setIfUnfocused(f.full_backup_allow_plaintext_upload, cfg.full_backup_allow_plaintext_upload);
    setIfUnfocused(f.project_auto_backup_on_codex_stop, cfg.project_auto_backup_on_codex_stop);
    setIfUnfocused(f.full_backup_quiet_minutes, toUnit(cfg.full_backup_quiet_seconds, 60));
    setIfUnfocused(f.project_auto_backup_min_interval_minutes, toUnit(cfg.project_auto_backup_min_interval_seconds, 60));
    setIfUnfocused(f.full_backup_retention_count, cfg.full_backup_retention_count);
    setIfUnfocused(f.full_backup_retention_max_gb, toUnit(cfg.full_backup_retention_max_bytes, 1024 * 1024 * 1024));
    if (document.activeElement !== apiToken) {
      apiToken.placeholder = cfg.api_token_configured ? "已保存 Token，留空保持不变" : "请输入 API Token 进行认证";
    }
  };

  const syncBadges = () => {
    const st = store.getState().status;
    const running = Boolean(st?.daemon_running);
    daemonBadge.textContent = running ? "运行中" : "已停止";
    daemonBadge.className = `badge ${running ? "ok" : ""}`;
    const n = Array.isArray(st?.hooks?.events) ? st.hooks.events.length : 0;
    hooksBadge.textContent = n ? `${n} 个事件` : "未安装";
    hooksBadge.className = `badge ${n ? "ok" : "warn"}`;
  };

  function renderRetention() {
    if (retentionLoading) {
      retentionBody.replaceChildren(loadingSpinner("正在读取服务器留存状态…"));
      return;
    }
    if (!retentionData) {
      retentionBody.replaceChildren(h("div", { class: "empty compact" }, "尚未读取服务器留存状态。"));
      return;
    }
    if (retentionData.success === false || retentionData.error) {
      retentionBody.replaceChildren(h("div", { class: "status-line error" }, retentionData.error || "读取失败"));
      return;
    }
    const policy = asObject(retentionData.policy);
    const full = asObject(policy.full_backups);
    const project = asObject(policy.project_backups);
    const snapshots = asObject(policy.snapshots);
    const usage = asObject(retentionData.usage);
    const planned = Number(retentionData.planned_count || 0);
    const actionCount = retentionData.dry_run ? planned : Number(retentionData.deleted_count ?? planned);
    const actionBytes = retentionData.dry_run ? retentionData.planned_bytes : (retentionData.deleted_bytes ?? retentionData.planned_bytes);
    retentionBody.replaceChildren(
      h(
        "div",
        { class: "result-facts" },
        fact("完整备份策略", `每设备/分支 ${full.keep_per_device_branch ?? "-"} 份 · ${full.max_age_days ?? "-"} 天`),
        fact("项目备份策略", `每项目/设备 ${project.keep_per_repo_device ?? "-"} 份 · ${project.max_age_days ?? "-"} 天`),
        fact("轻量快照策略", `每设备 ${snapshots.keep_per_device ?? "-"} 条 · ${snapshots.max_age_days ?? "-"} 天`),
        fact("服务器容量上限", formatBytes(policy.server_total_max_bytes)),
        fact("当前备份占用", formatBytes(usage.backup_bytes)),
        fact("当前记录", `完整 ${usage.full_backup_count || 0} · 项目 ${usage.project_backup_count || 0} · 快照 ${usage.snapshot_count || 0}`),
        fact(retentionData.dry_run ? "预览删除" : "本次删除", `${actionCount} 项 · ${formatBytes(actionBytes)}`),
        fact("已保护入口", `${usage.protected_count || 0} 项`)
      ),
      Array.isArray(retentionData.warnings) && retentionData.warnings.length
        ? h("div", { class: "status-line warn" }, retentionData.warnings.join("；"))
        : null
    );
  }

  function renderAppUpdate() {
    if (appUpdateLoading) {
      appUpdateBody.replaceChildren(loadingSpinner("正在检查 GitHub Release…"));
      downloadUpdateBtn.disabled = true;
      openUpdateBtn.disabled = true;
      return;
    }
    if (!appUpdateData) {
      appUpdateBody.replaceChildren(h("div", { class: "empty compact" }, "尚未检查本地应用版本。"));
      downloadUpdateBtn.disabled = true;
      openUpdateBtn.disabled = false;
      return;
    }
    if (appUpdateData.success === false || appUpdateData.error) {
      appUpdateBody.replaceChildren(h("div", { class: "status-line error" }, appUpdateData.error || "检查失败"));
      downloadUpdateBtn.disabled = true;
      openUpdateBtn.disabled = false;
      return;
    }
    const asset = asObject(appUpdateData.windows_asset);
    const tone = appUpdateData.update_available ? "warn" : "ok";
    downloadUpdateBtn.disabled = !asset.url;
    openUpdateBtn.disabled = false;
    appUpdateBody.replaceChildren(
      h(
        "div",
        { class: "result-section" },
        h(
          "div",
          { class: "result-section-head" },
          h("strong", {}, "GitHub 应用版本"),
          h("span", { class: `badge ${tone}` }, appUpdateData.update_available ? "发现新版本" : "已是最新")
        ),
        h(
          "div",
          { class: "result-facts" },
          fact("当前版本", appUpdateData.current_version || "-"),
          fact("最新版本", appUpdateData.latest_version || "-"),
          fact("发布时间", appUpdateData.published_at || "-"),
          fact("Windows 包", asset.name || "未找到"),
          fact("检查来源", appUpdateData.cached ? "缓存" : "GitHub")
        ),
        appUpdateData.downloaded
          ? h("div", { class: "status-line ok" }, `已下载到：${appUpdateData.path || "-"}`)
          : appUpdateData.update_available
            ? h("div", { class: "status-line warn" }, "有新版可用。下载后请退出当前应用，再解压并运行新版 EXE。")
            : h("div", { class: "status-line ok" }, "当前 EXE 与 GitHub 最新 Release 一致。")
      )
    );
  }

  function renderCompatibility() {
    if (compatLoading) {
      compatBody.replaceChildren(loadingSpinner("正在检查远程服务器 API 版本…"));
      return;
    }
    if (!compatData) {
      compatBody.replaceChildren(h("div", { class: "empty compact" }, "尚未检查远程服务器版本。"));
      return;
    }
    const server = asObject(compatData.server);
    const expected = asObject(compatData.expected);
    const features = Array.isArray(expected.required_features) ? expected.required_features : [];
    const missing = Array.isArray(compatData.missing_features) ? compatData.missing_features : [];
    const tone = compatData.compatible ? "ok" : compatData.needs_update ? "warn" : "danger";
    compatBody.replaceChildren(
      h(
        "div",
        { class: "result-section" },
        h(
          "div",
          { class: "result-section-head" },
          h("strong", {}, "远程服务器版本"),
          h("span", { class: `badge ${tone}` }, compatData.compatible ? "兼容" : compatData.needs_update ? "需要更新" : "检查失败")
        ),
        h(
          "div",
          { class: "result-facts" },
          fact("远程 API", server.api_version != null ? server.api_version : "-"),
          fact("本地期望 API", expected.api_version != null ? expected.api_version : "-"),
          fact("远程版本", server.server_version || "-"),
          fact("启动时间", server.started_at || "-"),
          fact("必需功能", `${features.length} 项`),
          fact("缺失功能", missing.length ? missing.join(", ") : "无")
        ),
        compatData.needs_update
          ? h("div", { class: "status-line warn" }, "本地桌面功能比远程服务器更新。请执行“一键更新部署”后再使用服务器留存、项目备份或 WSL 云备份等新功能。")
          : compatData.error
            ? h("div", { class: "status-line error" }, compatData.error)
            : null
      )
    );
  }

  const renderTable = () => {
    const { items, loading, error } = store.getState().snapshots;
    if (loading && !items.length) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, loadingSpinner("正在拉取远程快照…"))));
      return;
    }
    if (error) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, `拉取失败：${error}`)));
      return;
    }
    if (!items.length) {
      tableBody.replaceChildren(h("tr", {}, h("td", { colspan: "5", class: "table-msg" }, "暂无快照历史")));
      return;
    }
    tableBody.replaceChildren(...items.map(snap => {
      const snapObj = asObject(snap);
      const mini = (label, intent) => {
        const b = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, label);
        b.addEventListener("click", () => openSnapshot(store, snapObj, intent));
        return b;
      };
      return h("tr", {},
        h("td", {}, snapObj.device_id || "-"),
        h("td", { class: "mono cell-ellipsis", title: snapObj.cwd || "" }, snapObj.cwd || "-"),
        h("td", {}, snapObj.created_at || "-"),
        h("td", { class: "mono", title: snapObj.id || "" }, snapObj.id ? shortId(snapObj.id) : "-"),
        h("td", {}, h("div", { class: "row-actions" }, mini("详情", "detail"), mini("接续", "resume"), mini("还原", "restore")))
      );
    }));
  };

  syncFields();
  syncBadges();
  loadDeployConfig();
  renderRetention();
  renderAppUpdate();
  renderCompatibility();
  renderTabs();
  switchTab(currentTab);

  setOutput(store.getState().console);

  const uConfig = store.select(s => s.status?.config, syncFields);
  const uStatus = store.select(s => s.status, syncBadges);
  const uSnapshots = store.select(s => s.snapshots, renderTable);
  const uCon = store.select(s => s.console, () => setOutput(store.getState().console));

  return () => {
    uConfig();
    uStatus();
    uSnapshots();
    uCon();
  };
}
