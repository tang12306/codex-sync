// 系统设置：服务器连接与部署、备份策略、安装与自动化、后台与诊断。
// 四个分区同时挂在页面上、只切换显示，切换分区不会丢掉还没保存的修改。
import { h, asObject, fill } from "../dom.js";
import { saveConfig, runAction } from "../api.js";
import { refreshStatus } from "../poller.js";
import { showToast } from "../toast.js";
import { setLeaveGuard } from "../router.js";
import { checkConnection } from "../connection.js";
import { makeRun, actionButton, createSubTabs, switchControl, switchRow } from "../ui.js";
import { confirmDialog, showDialog } from "../dialog.js";
import { facts, failureReason, formatDateTime, isFailed, loadingSpinner, statusLine } from "../format.js";

const THEME_KEY = "codex-sync-theme";
const MB = 1024 * 1024;
const GB = 1024 * MB;

const TABS = [
  { key: "server", label: "服务器与部署" },
  { key: "policy", label: "备份策略" },
  { key: "automation", label: "安装与自动化" },
  { key: "diagnosis", label: "后台与诊断" },
];
// 只有这两个分区里有需要「保存设置」的表单项
const FORM_TABS = new Set(["server", "policy"]);

const ENC_SOURCE_LABELS = { env: "环境变量", config: "加密口令", key_file: "私有密钥文件", generated_key_file: "自动生成的私有密钥文件" };
const SERVICE_STATE_LABELS = { active: "运行中", inactive: "已停止", failed: "启动失败", activating: "正在启动", deactivating: "正在停止" };

// 数值参数：界面上用分钟 / 小时 / MB / GB 显示，保存时换算回配置里的秒和字节。
const NUMBER_FIELDS = [
  { key: "sync_interval_minutes", config: "sync_interval_seconds", scale: 60, min: 1, step: "1", label: "后台同步周期（分钟）", hint: "后台服务多久检查一次有没有新内容要上传" },
  { key: "full_backup_quiet_minutes", config: "full_backup_quiet_seconds", scale: 60, min: 0, step: "1", label: "完整备份安静期（分钟）", hint: "对话停止变化多久之后再打包，避免边写边打包" },
  { key: "realtime_backup_interval_minutes", config: "realtime_backup_interval_seconds", scale: 60, min: 1, step: "1", label: "实时备份检查间隔（分钟）", hint: "开启了实时备份的项目，多久检查一次改动" },
  { key: "project_auto_backup_min_interval_minutes", config: "project_auto_backup_min_interval_seconds", scale: 60, min: 0, step: "1", label: "项目自动备份最短间隔（分钟）", hint: "Git 提交、Codex 关闭触发的备份之间至少隔多久" },
  { key: "disaster_backup_min_interval_hours", config: "disaster_backup_min_interval_seconds", scale: 3600, min: 0, step: "1", label: "灾难备份最短间隔（小时）", hint: "这段时间内最多自动生成一次灾难备份" },
  { key: "max_untracked_copy_mb", config: "max_untracked_copy_bytes", scale: MB, min: 0, step: "1", label: "项目单个文件上限（MB）", hint: "项目备份时，超过这个大小的文件会被跳过" },
  { key: "full_backup_retention_count", config: "full_backup_retention_count", scale: 1, min: 0, step: "1", label: "本地完整备份保留份数", hint: "超出后自动删除最旧的本地备份包，0 表示不限制" },
  { key: "full_backup_retention_max_gb", config: "full_backup_retention_max_bytes", scale: GB, min: 0, step: "0.5", label: "本地完整备份容量上限（GB）", hint: "所有本地备份包合计最多占用多少空间，0 表示不限制" },
];

function toUnit(value, divisor) {
  const n = Number(value);
  if (value == null || value === "" || !Number.isFinite(n)) return "";
  const out = n / divisor;
  return Number.isInteger(out) ? String(out) : String(Number(out.toFixed(2)));
}

function readTheme() {
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  try {
    if (theme === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* 存不了也照样切换本次显示 */
  }
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

function fieldLabel(label, hint) {
  return h("label", { class: "field-label" }, h("span", {}, label), hint ? h("span", { class: "field-hint" }, hint) : null);
}

// 密码框 + 显示/隐藏按钮
function secretInput(attrs = {}) {
  const input = h("input", { class: "input", type: "password", autocomplete: "off", spellcheck: "false", ...attrs });
  const toggle = h("button", { class: "input-affix", type: "button" }, "显示");
  toggle.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    toggle.textContent = show ? "隐藏" : "显示";
  });
  return { input, wrap: h("div", { class: "input-wrap" }, input, toggle) };
}

export function mount(root, store, { sub = "" } = {}) {
  const run = makeRun(store);
  let alive = true;
  const config = () => asObject(store.getState().status?.config);

  // ==================== 表单状态 ====================
  const f = {};
  let dirty = false;
  let releaseGuard = null;

  const bindDirty = (el) => {
    el.addEventListener("input", () => setDirty(true));
    el.addEventListener("change", () => setDirty(true));
    return el;
  };
  const field = (key, label, hint, attrs = {}) => {
    f[key] = bindDirty(h("input", { class: "input", spellcheck: "false", ...attrs }));
    return h("div", { class: "field" }, fieldLabel(label, hint), f[key]);
  };
  const toggle = (key, title, desc) => {
    const control = switchControl({ label: title, onChange: () => setDirty(true) });
    f[key] = control.input;
    return switchRow(title, desc, control.el);
  };

  const confirmLeave = () =>
    confirmDialog({
      title: "设置还没有保存",
      message: "离开这个页面后，刚才修改的设置会丢失。",
      confirmLabel: "不保存，直接离开",
      cancelLabel: "继续编辑",
      danger: true,
    });

  // ==================== 服务器与部署 ====================
  const token = secretInput({ placeholder: "留空保持不变" });
  f.api_token = bindDirty(token.input);
  const connResult = h("div", { class: "conn-test", "aria-live": "polite" });

  const connCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "服务器连接"),
    h("p", { class: "card-desc" }, "填写同步服务器的地址和访问令牌。还没有服务器？可以用右侧的「一键部署」装一台。"),
    field("server_url", "服务器地址", "以 http:// 或 https:// 开头", { type: "url", placeholder: "https://sync.example.com" }),
    h("div", { class: "field" }, fieldLabel("访问令牌（API Token）", "部署服务器时自动生成"), token.wrap),
    field("device_id", "设备名称", "这台电脑在服务器上的标识，多台电脑不要重名", { type: "text" }),
    h(
      "div",
      { class: "card-actions" },
      actionButton("测试连接", "btn-ghost", testConnection, { title: "使用表单里当前填写的地址和令牌（无需先保存）" })
    ),
    connResult
  );

  async function testConnection() {
    const url = f.server_url.value.trim();
    if (!url) {
      fill(connResult, statusLine("请先填写服务器地址。", "warn"));
      f.server_url.focus();
      return;
    }
    fill(connResult, loadingSpinner("正在连接服务器…", { compact: true }));
    let result;
    try {
      result = await runAction("test-server-connection", { server_url: url, api_token: f.api_token.value.trim() });
    } catch (e) {
      result = { success: false, error: String(e.message || e) };
    }
    if (!alive) return;
    const server = asObject(result.server);
    const ms = result.latency_ms != null ? `，用时 ${result.latency_ms} ms` : "";
    let line;
    if (result.compatible) line = statusLine(`连接成功${ms}。服务器版本 ${server.server_version || server.api_version || "-"}。`, "ok");
    else if (result.needs_update) line = statusLine(`已经连上服务器${ms}，但服务器版本较旧，部分功能不可用。请在「云端服务器部署」里点「一键更新部署」。`, "warn");
    else line = statusLine(`连接失败：${failureReason(result)}`, "error");
    fill(connResult, line, dirty ? h("p", { class: "card-desc" }, "这是用表单里还没保存的内容测试的，确认无误后记得点下方的「保存设置」。") : null);
  }

  // ----- 服务器版本 -----
  let compatData = null;
  let compatLoading = false;
  const compatBody = h("div", { class: "result-stack" });

  async function checkServerCompatibility({ notify = false } = {}) {
    if (!config().server_url) {
      compatData = null;
      renderCompatibility();
      if (notify) showToast("还没有保存服务器地址", "warning");
      return;
    }
    compatLoading = true;
    renderCompatibility();
    try {
      compatData = await runAction("server-compatibility");
    } catch (e) {
      compatData = { success: false, compatible: false, error: String(e.message || e) };
    }
    compatLoading = false;
    if (!alive) return;
    renderCompatibility();
    if (notify) {
      if (compatData.compatible) showToast("服务器版本兼容", "success");
      else if (compatData.needs_update) showToast("服务器需要更新部署", "warning");
      else showToast(`服务器版本检查失败：${failureReason(compatData)}`, "error");
    }
  }

  function renderCompatibility() {
    if (compatLoading) {
      fill(compatBody, loadingSpinner("正在检查服务器版本…", { compact: true }));
      return;
    }
    if (!config().server_url) {
      fill(compatBody, h("div", { class: "empty compact" }, "保存服务器地址后会自动检查。"));
      return;
    }
    if (!compatData) {
      fill(compatBody, h("div", { class: "empty compact" }, "还没有检查服务器版本。"));
      return;
    }
    const server = asObject(compatData.server);
    const expected = asObject(compatData.expected);
    const missing = Array.isArray(compatData.missing_features) ? compatData.missing_features : [];
    const tone = compatData.compatible ? "ok" : compatData.needs_update ? "warn" : "danger";
    fill(compatBody,
      h(
        "div",
        { class: "result-section" },
        h(
          "div",
          { class: "result-section-head" },
          h("strong", {}, "检查结果"),
          h("span", { class: `badge ${tone}` }, compatData.compatible ? "兼容" : compatData.needs_update ? "需要更新" : "检查失败")
        ),
        facts([
          ["服务器 API 版本", server.api_version ?? "-"],
          ["本机需要的版本", expected.api_version ?? "-"],
          ["服务器程序版本", server.server_version || "-"],
          ["服务器启动于", server.started_at ? formatDateTime(server.started_at) : "-"],
          missing.length ? ["缺少的功能", missing.join("、")] : null,
        ]),
        compatData.needs_update
          ? statusLine("本机程序比服务器新。请点「一键更新部署」，否则服务器留存、项目备份等功能可能用不了。", "warn")
          : isFailed(compatData)
            ? statusLine(failureReason(compatData), "error")
            : null
      )
    );
  }

  const compatCard = h(
    "div",
    { class: "card" },
    h(
      "div",
      { class: "card-title" },
      h("span", {}, "服务器版本"),
      actionButton("重新检查", "btn-ghost btn-sm", () => checkServerCompatibility({ notify: true }))
    ),
    h("p", { class: "card-desc" }, "检查服务器上的程序是否支持本机用到的全部功能。"),
    compatBody
  );

  // ----- 一键部署 -----
  const deployFields = {};
  const deployField = (key, label, hint, attrs = {}) => {
    deployFields[key] = h("input", { class: "input", spellcheck: "false", ...attrs });
    return h("div", { class: "field" }, fieldLabel(label, hint), deployFields[key]);
  };
  const sshPassword = secretInput({ placeholder: "只用于本次操作，不会保存" });
  deployFields.ssh_password = sshPassword.input;
  const nginxSwitch = switchControl({ label: "启用 nginx 反向代理" });
  deployFields.nginx_enabled = nginxSwitch.input;

  function splitSshTarget(target) {
    const text = String(target || "").trim();
    if (!text) return { user: "root", host: "" };
    const at = text.lastIndexOf("@");
    if (at > 0) return { user: text.slice(0, at), host: text.slice(at + 1) };
    return { user: "", host: text };
  }

  function deployPayload(includePassword = true) {
    const user = deployFields.ssh_user.value.trim();
    const host = deployFields.ssh_host.value.trim();
    const maxMb = Number(deployFields.max_body_mb.value);
    return {
      ssh_target: host ? (user ? `${user}@${host}` : host) : "",
      ssh_port: Number(deployFields.ssh_port.value) || 22,
      ssh_password: includePassword ? deployFields.ssh_password.value : "",
      remote_dir: deployFields.remote_dir.value.trim() || "/opt/codex-sync-server",
      service_name: deployFields.service_name.value.trim() || "codex-sync-server",
      python: deployFields.python.value.trim() || "python3",
      bind_host: deployFields.bind_host.value.trim() || "127.0.0.1",
      bind_port: Number(deployFields.bind_port.value) || 8888,
      data_dir: deployFields.data_dir.value.trim() || "/var/lib/codex-sync",
      token_file: deployFields.token_file.value.trim() || "/etc/codex-sync/server-token",
      max_body_bytes: Number.isFinite(maxMb) && maxMb > 0 ? Math.round(maxMb * MB) : 512 * MB,
      nginx_enabled: deployFields.nginx_enabled.checked,
      nginx_server_name: deployFields.nginx_server_name.value.trim() || (deployFields.nginx_enabled.checked ? host : ""),
      client_max_body_size: deployFields.client_max_body_size.value.trim() || "512m",
    };
  }

  function requireHost() {
    if (deployFields.ssh_host.value.trim()) return true;
    showToast("请先填写服务器 IP 或域名", "warning");
    deployFields.ssh_host.focus();
    return false;
  }

  function fillDeployFields(cfg = {}) {
    const data = asObject(cfg);
    const target = splitSshTarget(data.ssh_target);
    const values = {
      ssh_user: target.user,
      ssh_host: target.host,
      ssh_port: data.ssh_port || 22,
      remote_dir: data.remote_dir || "/opt/codex-sync-server",
      service_name: data.service_name || "codex-sync-server",
      python: data.python || "python3",
      bind_host: data.bind_host || "127.0.0.1",
      bind_port: data.bind_port || 8888,
      data_dir: data.data_dir || "/var/lib/codex-sync",
      token_file: data.token_file || "/etc/codex-sync/server-token",
      max_body_mb: toUnit(data.max_body_bytes || 512 * MB, MB),
      nginx_server_name: data.nginx_server_name || "",
      client_max_body_size: data.client_max_body_size || "512m",
    };
    for (const [key, value] of Object.entries(values)) deployFields[key].value = value == null ? "" : String(value);
    deployFields.nginx_enabled.checked = Boolean(data.nginx_enabled);
  }

  async function loadDeployConfig() {
    try {
      const result = await runAction("deploy-config");
      if (alive) fillDeployFields(result.config);
    } catch (e) {
      if (alive) showToast(`读取部署配置失败：${e.message || e}`, "error");
    }
  }

  async function showDeployStatus() {
    if (!requireHost()) return;
    let result;
    try {
      result = await runAction("deploy-status", deployPayload(true));
    } catch (e) {
      showToast(`查询失败：${e.message || e}`, "error");
      return;
    }
    if (!alive) return;
    if (isFailed(result)) {
      showToast(`查询失败：${failureReason(result)}`, "error");
      return;
    }
    const running = result.active === "active";
    const healthy = running && result.endpoint_ok;
    await showDialog({
      title: "远程服务状态",
      body: h(
        "div",
        {},
        facts([
          ["SSH 目标", result.ssh_target || "-"],
          ["系统服务", result.service || "-"],
          ["运行状态", SERVICE_STATE_LABELS[result.active] || result.active || "未知"],
          ["接口探测", `${result.endpoint || "-"} → HTTP ${result.endpoint_http || "无响应"}`],
          ["服务端程序", result.in_sync ? "与本机版本一致" : "与本机版本不同"],
        ]),
        healthy
          ? statusLine(result.in_sync ? "服务运行正常。" : "服务运行正常，但服务端程序和本机不同，建议「一键更新部署」。", result.in_sync ? "ok" : "warn")
          : statusLine(running ? "服务在运行，但接口没有正常响应，请检查防火墙或 nginx 配置。" : "服务没有在运行。可以尝试「一键更新部署」重新启动。", "error")
      ),
      actions: [{ label: "关闭", value: true, class: "btn-primary", autofocus: true }],
    });
  }

  async function installServer() {
    if (!requireHost()) return;
    const payload = deployPayload(true);
    const ok = await confirmDialog({
      title: "在远程服务器上安装同步服务？",
      message: `将通过 SSH 连接 ${payload.ssh_target}，并完成以下操作：`,
      details: [
        `创建程序目录 ${payload.remote_dir} 和数据目录 ${payload.data_dir}`,
        `安装并启动系统服务 ${payload.service_name}.service`,
        "生成访问令牌，并自动填到本机的服务器连接设置里",
        payload.nginx_enabled ? `配置 nginx 反向代理（${payload.nginx_server_name || "未填域名"}）` : null,
      ],
      confirmLabel: "开始安装",
    });
    if (!ok) return;
    const result = await run("deploy-install", {
      payload: { ...payload, save_config: true, backfill_local: true },
      okMsg: "服务器已安装，连接信息已自动填好",
      errMsg: "安装失败",
      label: "安装服务器",
    });
    if (!alive || isFailed(result)) return;
    fillServerFields();
    checkConnection(store, { force: true });
    await checkServerCompatibility();
  }

  async function updateServer() {
    if (!requireHost()) return;
    const payload = deployPayload(true);
    const ok = await confirmDialog({
      title: "更新远程服务器上的程序？",
      message: `将把本机的同步服务程序上传到 ${payload.ssh_target} 并重启服务。`,
      details: ["服务器上已有的备份和数据不会受影响", "重启期间（通常几秒钟）同步请求会暂时失败"],
      confirmLabel: "开始更新",
    });
    if (!ok) return;
    const result = await run("deploy-update", {
      payload: { ...payload, save_config: true },
      okMsg: "服务器程序已更新",
      errMsg: "更新失败",
      label: "更新部署",
      refresh: false,
    });
    if (alive && !isFailed(result)) await checkServerCompatibility();
  }

  const deployCard = h(
    "div",
    { class: "card section" },
    h("div", { class: "card-title" }, "云端服务器部署"),
    h("p", { class: "card-desc" }, "通过 SSH 在你自己的 Linux 云主机上安装或升级同步服务。SSH 密码只用于本次操作，不会写入任何文件。"),
    h(
      "div",
      { class: "grid grid-3" },
      deployField("ssh_host", "服务器 IP / 域名", "也可以填 SSH 配置里的别名", { type: "text", placeholder: "1.2.3.4" }),
      deployField("ssh_user", "SSH 用户名", "通常是 root；用 SSH 别名时可留空", { type: "text", placeholder: "root" }),
      h("div", { class: "field" }, fieldLabel("SSH 密码", "留空则使用 SSH 密钥登录"), sshPassword.wrap)
    ),
    h(
      "details",
      { class: "collapse" },
      h("summary", {}, "高级选项（端口、目录、nginx 等）"),
      h(
        "div",
        { class: "collapse-body" },
        h(
          "div",
          { class: "grid grid-3" },
          deployField("ssh_port", "SSH 端口", "默认 22", { type: "number", min: "1", step: "1" }),
          deployField("remote_dir", "程序目录", "同步服务程序的安装位置", { type: "text" }),
          deployField("data_dir", "数据目录", "服务器上保存备份和数据库的位置", { type: "text" }),
          deployField("service_name", "系统服务名", "会生成 <名称>.service", { type: "text" }),
          deployField("python", "Python 路径", "例如 python3 或 /usr/bin/python3", { type: "text" }),
          deployField("token_file", "令牌文件", "服务器保存访问令牌的位置", { type: "text" }),
          deployField("bind_host", "监听地址", "不用 nginx 又要公网访问时填 0.0.0.0", { type: "text" }),
          deployField("bind_port", "监听端口", "默认 8888", { type: "number", min: "1", step: "1" }),
          deployField("max_body_mb", "上传大小上限（MB）", "默认 512", { type: "number", min: "1", step: "1" })
        ),
        switchRow("启用 nginx 反向代理", "把 /api/ 转发到同步服务，适合已有域名和 HTTPS 的情况", nginxSwitch.el),
        h(
          "div",
          { class: "grid grid-3" },
          deployField("nginx_server_name", "nginx 域名", "留空则使用上面的服务器地址", { type: "text", placeholder: "sync.example.com" }),
          deployField("client_max_body_size", "nginx 上传上限", "例如 512m", { type: "text" })
        )
      )
    ),
    h(
      "div",
      { class: "card-actions" },
      actionButton("一键安装服务器", "btn-primary", installServer),
      actionButton("一键更新部署", "btn-ghost", updateServer),
      actionButton("查看远程服务状态", "btn-ghost", showDeployStatus),
      h("span", { class: "divider", "aria-hidden": "true" }),
      actionButton("保存部署参数", "btn-ghost", async () => {
        await run("save-deploy-config", { payload: deployPayload(false), refresh: false, okMsg: "部署参数已保存（不含密码）", label: "保存部署参数" });
      })
    )
  );

  const panelServer = h("div", {}, h("div", { class: "section grid grid-2" }, connCard, compatCard), deployCard);

  // ==================== 备份策略 ====================
  const backupCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "备份开关"),
    toggle("full_backup_enabled", "生成完整对话备份", "把包含正文的对话打包，用于换电脑后接着用"),
    toggle("full_backup_auto_upload", "自动上传完整备份", "对话安静一段时间后，后台自动上传到服务器，不用手动点"),
    toggle("full_backup_include_config", "包含 Codex 配置", "把 config.toml 等全局配置一起打包"),
    toggle("full_backup_include_memories", "包含记忆库", "把 memories 目录一起打包"),
    toggle("project_auto_backup_on_codex_stop", "Codex 关闭时备份当前项目", "每次 Codex 停止，把当前项目（含未提交改动）加入备份队列"),
    toggle("disaster_backup_enabled", "写入前自动做灾难备份", "导入对话、恢复备份、合并渠道之前，先把本机 Codex 数据完整备份一份")
  );

  const passphrase = secretInput({ placeholder: "留空保持不变" });
  f.full_backup_encryption_passphrase = bindDirty(passphrase.input);
  const encStatus = h("div", { class: "enc-status" });

  const encryptionCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "完整备份加密"),
    h("p", { class: "card-desc" }, "上传前在本机加密，服务器上只保存密文。在另一台电脑填入相同的口令，就能解密恢复。"),
    toggle("full_backup_encryption_enabled", "加密完整备份", "强烈建议保持开启"),
    toggle("full_backup_allow_plaintext_upload", "允许明文上传", "只在关闭加密时生效；正文会以明文形式保存在服务器上"),
    h(
      "div",
      { class: "field" },
      fieldLabel("加密口令", "设置后会自动开启加密、关闭明文上传"),
      passphrase.wrap
    ),
    encStatus,
    h(
      "details",
      { class: "collapse" },
      h("summary", {}, "不想用口令？改用密钥文件"),
      h(
        "div",
        { class: "collapse-body" },
        h("p", { class: "card-desc" }, "没有设置口令时，本机会自动生成一个私有密钥。把它导出，再在另一台电脑上导入，也能互相解密。密钥文件是明文，请只通过可信的方式传输。"),
        h(
          "div",
          { class: "card-actions" },
          actionButton("导出本机密钥", "btn-ghost", () => run("export-encryption-key", { okMsg: "密钥已导出，请妥善保管", label: "导出密钥" })),
          actionButton("从文件导入密钥", "btn-ghost", async () => {
            const ok = await confirmDialog({
              title: "导入密钥文件？",
              message: "导入后，本机会改用这个密钥加密和解密完整备份。",
              details: ["之前用旧密钥加密的备份，需要旧密钥才能解密", "设置了加密口令时，口令优先于密钥文件"],
              confirmLabel: "选择文件并导入",
            });
            if (ok) await run("import-encryption-key", { okMsg: "密钥已导入", label: "导入密钥" });
          })
        )
      )
    )
  );

  function renderEncStatus() {
    const cfg = config();
    const enc = asObject(cfg.full_backup_encryption);
    const enabled = cfg.full_backup_encryption_enabled !== false;
    let badge;
    if (!enabled) badge = h("span", { class: `badge ${cfg.full_backup_allow_plaintext_upload ? "danger" : "warn"}` }, cfg.full_backup_allow_plaintext_upload ? "未加密，允许明文上传" : "未加密，不会上传");
    else if (enc.configured) badge = h("span", { class: "badge ok" }, "已就绪");
    else badge = h("span", { class: "badge warn" }, "还没有密钥");
    fill(encStatus,
      badge,
      enc.configured ? h("span", { class: "enc-meta" }, `来源：${ENC_SOURCE_LABELS[enc.source] || enc.source || "-"}`) : null,
      enc.configured && enc.key_id ? h("span", { class: "enc-meta mono" }, `指纹：${enc.key_id}`) : null,
      h(
        "span",
        { class: "enc-hint" },
        enc.configured
          ? "在另一台电脑上设置相同的口令后，两边的指纹一致就说明可以互相解密。"
          : "填入口令并保存后，这里会显示密钥指纹，用于在两台电脑之间核对。"
      )
    );
  }

  const paramsCard = h(
    "div",
    { class: "card section" },
    h("div", { class: "card-title" }, "参数"),
    h(
      "div",
      { class: "grid grid-2" },
      ...NUMBER_FIELDS.map((spec) => field(spec.key, spec.label, spec.hint, { type: "number", min: String(spec.min), step: spec.step, inputmode: "decimal" }))
    )
  );

  const themeSelect = h(
    "select",
    { class: "input", "aria-label": "界面主题" },
    h("option", { value: "system" }, "跟随系统"),
    h("option", { value: "light" }, "浅色"),
    h("option", { value: "dark" }, "深色")
  );
  themeSelect.value = readTheme();
  themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));

  f.desktop_close_behavior = bindDirty(
    h(
      "select",
      { class: "input" },
      h("option", { value: "ask" }, "每次询问"),
      h("option", { value: "minimize_to_tray" }, "最小化到托盘"),
      h("option", { value: "exit" }, "直接退出")
    )
  );

  const appBehaviorCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "桌面应用"),
    h("div", { class: "field" }, fieldLabel("界面主题", "立即生效，只保存在这台电脑"), themeSelect),
    h(
      "div",
      { class: "field" },
      fieldLabel("点击窗口关闭按钮时", "最小化到托盘会继续在后台同步；直接退出会停止本地服务"),
      f.desktop_close_behavior
    )
  );

  const panelPolicy = h(
    "div",
    {},
    h("div", { class: "section grid grid-2" }, backupCard, encryptionCard),
    paramsCard,
    h("div", { class: "section grid grid-2" }, appBehaviorCard)
  );

  // ==================== 安装与自动化 ====================
  const hooksBadge = h("span", { class: "badge" }, "…");
  const hooksBody = h("div", { class: "result-stack" });
  const hooksCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "Codex Hooks"), hooksBadge),
    h("p", { class: "card-desc" }, "在 Codex 的 hooks.json 里写入同步钩子。装好后，Codex 每次结束回复或关闭时都会通知 Codex Sync 检查备份。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("写入 / 更新 Hooks", "btn-primary", () => run("install-hooks", { okMsg: "Hooks 已写入", label: "写入 Hooks" }))
    ),
    hooksBody
  );

  function renderHooks() {
    const hooks = asObject(store.getState().status?.hooks);
    const events = Array.isArray(hooks.events) ? hooks.events : [];
    hooksBadge.textContent = events.length ? `已安装 · ${events.length} 个事件` : "未安装";
    hooksBadge.className = `badge ${events.length ? "ok" : "warn"}`;
    fill(hooksBody,
      ...[
        hooks.path ? h("p", { class: "card-desc mono", title: hooks.path }, hooks.path) : null,
        hooks.valid_json === false ? statusLine("hooks.json 不是有效的 JSON，写入前请先检查这个文件。", "error") : null,
        events.length ? h("p", { class: "card-desc" }, `已挂接：${events.join("、")}`) : null,
      ].filter(Boolean)
    );
  }

  // ----- Windows 定时任务 -----
  let taskData = null;
  let taskLoading = false;
  const taskBody = h("div", { class: "result-stack" });
  const minutesInput = h("input", { class: "input input-narrow", type: "number", min: "1", step: "1", value: "3", "aria-label": "同步周期（分钟）" });

  async function refreshTaskStatus({ notify = false } = {}) {
    taskLoading = true;
    renderTask();
    try {
      taskData = await runAction("task-status");
    } catch (e) {
      taskData = { installed: false, error: String(e.message || e) };
    }
    taskLoading = false;
    if (!alive) return;
    renderTask();
    if (notify) showToast("定时任务状态已刷新", "success");
  }

  function renderTask() {
    if (taskLoading && !taskData) {
      fill(taskBody, loadingSpinner("正在读取定时任务…", { compact: true }));
      return;
    }
    const data = asObject(taskData);
    if (!taskData) {
      taskBody.replaceChildren();
      return;
    }
    if (!data.installed) {
      fill(taskBody,
        data.code == null && data.error ? statusLine(`读取失败：${data.error}`, "error") : statusLine("还没有安装定时任务。", "info")
      );
      return;
    }
    const lastResult = String(data.last_result ?? "").trim();
    fill(taskBody,
      facts([
        ["状态", data.state || data.status || "已安装"],
        ["运行频率", data.schedule || "-"],
        ["下次运行", data.next_run || "-"],
        ["上次运行", data.last_run || "-"],
        ["上次结果", lastResult === "0" ? "成功" : lastResult || "-"],
      ])
    );
  }

  const taskCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "Windows 定时任务"), actionButton("刷新", "btn-ghost btn-sm", () => refreshTaskStatus({ notify: true }))),
    h("p", { class: "card-desc" }, "注册一个系统计划任务，即使没打开 Codex Sync，也会每隔几分钟在后台静默同步一次。"),
    h(
      "div",
      { class: "card-actions" },
      h("label", { class: "inline-field" }, h("span", {}, "每"), minutesInput, h("span", {}, "分钟")),
      actionButton("安装定时任务", "btn-primary", async () => {
        const minutes = Math.max(1, Math.round(Number(minutesInput.value) || 3));
        await run("install-task", { payload: { minutes }, okMsg: `定时任务已安装，每 ${minutes} 分钟同步一次`, label: "安装定时任务", refresh: false });
        await refreshTaskStatus();
      }),
      actionButton("卸载", "btn-ghost", async () => {
        const ok = await confirmDialog({ title: "卸载定时任务？", message: "卸载后，只有在 Codex Sync 打开时才会自动同步。", confirmLabel: "卸载" });
        if (!ok) return;
        await run("uninstall-task", { okMsg: "定时任务已卸载", label: "卸载定时任务", refresh: false });
        await refreshTaskStatus();
      })
    ),
    taskBody
  );

  // ----- 安装与开机自启 -----
  // 安装、升级、卸载由 CodexSyncSetup 安装程序负责，这里只展示安装状态和切换开机自启
  let appInstallData = null;
  let appInstallLoading = false;
  const appInstallBody = h("div", { class: "result-stack" });

  async function refreshAppInstall({ notify = false } = {}) {
    appInstallLoading = true;
    renderAppInstall();
    try {
      appInstallData = await runAction("app-install-status");
      if (notify) showToast("安装状态已刷新", "success");
    } catch (e) {
      appInstallData = { success: false, error: String(e.message || e) };
      if (notify) showToast(`读取安装状态失败：${e.message || e}`, "error");
    }
    appInstallLoading = false;
    if (alive) renderAppInstall();
  }

  async function toggleStartup() {
    const data = asObject(appInstallData || store.getState().status?.app_install);
    if (asObject(data.startup).enabled) {
      await run("app-startup-disable", { okMsg: "开机自启已关闭", label: "关闭开机自启" });
    } else {
      await run("app-startup-enable", { okMsg: "开机自启已开启", label: "开启开机自启" });
    }
    if (alive) await refreshAppInstall();
  }

  const startupToggleBtn = actionButton("开启开机自启", "btn-ghost", toggleStartup);
  const appInstallActions = h("div", { class: "card-actions" }, startupToggleBtn);

  function renderAppInstall() {
    if (appInstallLoading && !appInstallData) {
      fill(appInstallBody, loadingSpinner("正在读取安装状态…", { compact: true }));
      return;
    }
    const data = asObject(appInstallData || store.getState().status?.app_install);
    const startup = asObject(data.startup);
    const supported = data.supported !== false;
    const installedHere = Boolean(data.installed);
    const installedAvailable = Boolean(data.installed_available);
    startupToggleBtn.textContent = startup.enabled ? "关闭开机自启" : "开启开机自启";
    startupToggleBtn.hidden = !supported || !(installedAvailable || startup.enabled);
    appInstallActions.hidden = startupToggleBtn.hidden;

    if (data.error) {
      fill(appInstallBody, statusLine(data.error, "error"));
      return;
    }
    if (!supported) {
      fill(appInstallBody, statusLine("安装和开机自启只支持 Windows 版程序。", "info"));
      return;
    }
    const runMode = installedHere ? "已安装（从安装目录运行）" : installedAvailable ? "已安装，但当前从其他位置运行" : "未安装";
    const startupText = startup.enabled ? (startup.target_exists === false ? "已开启，但目标程序不存在" : "已开启") : "未开启";
    let note = null;
    if (!installedAvailable && !data.packaged) note = statusLine("当前是源码 / 开发模式运行。要安装到本机，请运行 CodexSyncSetup 安装程序。", "info");
    else if (!installedAvailable) note = statusLine("没有找到安装信息。请运行 CodexSyncSetup 安装程序，安装后才能开启开机自启。", "info");
    else if (!installedHere) note = statusLine("已经安装过。建议从开始菜单或桌面快捷方式启动，而不是继续用当前位置的程序。", "warn");
    else if (startup.enabled && startup.target_exists === false) note = statusLine("开机自启指向的程序不存在了，请关闭后重新开启。", "warn");
    fill(appInstallBody,
      ...[
        facts([
          ["安装状态", runMode],
          data.installed_version ? ["安装版本", data.installed_version] : null,
          ["安装目录", data.install_dir || "-"],
          ["开机自启", startupText],
          ["当前程序", data.current_executable || "-"],
        ]),
        note,
        installedAvailable ? statusLine("要卸载，请在 Windows“设置 → 应用 → 已安装的应用”里找到 Codex Sync。", "info") : null,
      ].filter(Boolean)
    );
  }

  const appInstallCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "安装与开机自启"), actionButton("刷新", "btn-ghost btn-sm", () => refreshAppInstall({ notify: true }))),
    h("p", { class: "card-desc" }, "安装、升级和卸载都由 CodexSyncSetup 安装程序完成；这里显示安装状态，并可以随时开关开机自启。"),
    appInstallActions,
    appInstallBody
  );

  // ----- 应用更新 -----
  let appUpdateData = null;
  let appUpdateLoading = false;
  const appUpdateBody = h("div", { class: "result-stack" });

  async function checkAppUpdate({ force = false, notify = false } = {}) {
    appUpdateLoading = true;
    renderAppUpdate();
    try {
      appUpdateData = await runAction("app-update-check", { force });
    } catch (e) {
      appUpdateData = { success: false, error: String(e.message || e) };
    }
    appUpdateLoading = false;
    if (!alive) return;
    renderAppUpdate();
    if (notify) {
      if (appUpdateData.update_available) showToast(`发现新版本 ${appUpdateData.latest_version}`, "warning");
      else if (!isFailed(appUpdateData)) showToast("已经是最新版本", "success");
      else showToast(`检查更新失败：${failureReason(appUpdateData)}`, "error");
    }
  }

  const downloadUpdateBtn = actionButton("下载新版", "btn-primary", async () => {
    const result = await run("app-update-download", { payload: { force: false }, okMsg: "新版安装包已下载", errMsg: "下载失败", label: "下载新版", refresh: false });
    if (alive && result) {
      appUpdateData = { ...appUpdateData, ...result };
      renderAppUpdate();
    }
  });
  downloadUpdateBtn.hidden = true;

  const installUpdateBtn = actionButton("立即安装", "btn-primary", async () => {
    const ok = await confirmDialog({
      title: "安装新版本？",
      message: "会打开新版安装程序。安装开始前，它会提示关闭正在运行的 Codex Sync。",
      details: ["默认装到原来的目录，配置和备份记录都会保留", "安装完成后可以直接启动新版"],
      confirmLabel: "打开安装程序",
    });
    if (!ok) return;
    await run("app-update-install", { payload: { path: appUpdateData?.path || "" }, okMsg: "已打开安装程序", errMsg: "无法启动安装程序", label: "立即安装", refresh: false });
  });
  installUpdateBtn.hidden = true;

  function renderAppUpdate() {
    if (appUpdateLoading) {
      fill(appUpdateBody, loadingSpinner("正在检查 GitHub 上的新版本…", { compact: true }));
      return;
    }
    if (!appUpdateData) {
      appUpdateBody.replaceChildren();
      downloadUpdateBtn.hidden = true;
      installUpdateBtn.hidden = true;
      return;
    }
    if (isFailed(appUpdateData) && !appUpdateData.latest_version) {
      fill(appUpdateBody, statusLine(`检查失败：${failureReason(appUpdateData)}`, "error"));
      downloadUpdateBtn.hidden = true;
      installUpdateBtn.hidden = true;
      return;
    }
    const asset = asObject(appUpdateData.windows_asset);
    downloadUpdateBtn.hidden = !(appUpdateData.update_available && asset.url) || Boolean(appUpdateData.downloaded);
    installUpdateBtn.hidden = !(appUpdateData.downloaded && appUpdateData.path);
    let note;
    if (appUpdateData.downloaded) note = statusLine(`已下载到：${appUpdateData.path || "-"}。点“立即安装”即可升级，安装程序会先关闭 Codex Sync。`, "ok");
    else if (appUpdateData.update_available) note = statusLine(asset.url ? "有新版本可用。下载后点“立即安装”即可升级。" : "有新版本，但没有找到 Windows 安装包，请打开发布页查看。", "warn");
    else note = statusLine("当前已是最新版本。", "ok");
    fill(appUpdateBody,
      facts([
        ["当前版本", appUpdateData.current_version || store.getState().status?.app_version || "-"],
        ["最新版本", appUpdateData.latest_version || "-"],
        ["发布时间", appUpdateData.published_at ? formatDateTime(appUpdateData.published_at) : "-"],
        asset.name ? ["安装包", asset.name] : null,
      ]),
      note
    );
  }

  const appUpdateCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "应用更新"),
    h("p", { class: "card-desc" }, "检查 GitHub 上有没有新版本，下载后用安装程序原地升级。"),
    h(
      "div",
      { class: "card-actions" },
      actionButton("检查更新", "btn-ghost", () => checkAppUpdate({ force: true, notify: true })),
      downloadUpdateBtn,
      installUpdateBtn,
      actionButton("打开发布页", "btn-ghost", () => run("app-update-open", { payload: { force: false }, okMsg: "已在浏览器中打开发布页", label: "打开发布页", refresh: false }))
    ),
    appUpdateBody
  );

  const panelAutomation = h(
    "div",
    {},
    h("div", { class: "section grid grid-2" }, hooksCard, taskCard),
    h("div", { class: "section grid grid-2" }, appInstallCard, appUpdateCard)
  );

  // ==================== 后台与诊断 ====================
  const daemonBadge = h("span", { class: "badge" }, "…");
  const startDaemonBtn = actionButton("启动后台服务", "btn-primary", () => run("start-daemon", { okMsg: "后台服务已启动", label: "启动后台服务" }));
  const stopDaemonBtn = actionButton("停止后台服务", "btn-ghost", async () => {
    const ok = await confirmDialog({
      title: "停止后台服务？",
      message: "停止后不会再自动上传完整备份，也不会处理项目自动备份队列，直到重新启动。",
      confirmLabel: "停止",
      danger: true,
    });
    if (ok) await run("stop-daemon", { okMsg: "后台服务已停止", label: "停止后台服务" });
  });

  const daemonCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, h("span", {}, "后台服务"), daemonBadge),
    h("p", { class: "card-desc" }, "按周期扫描并上传完整对话备份、处理项目自动备份和实时备份。关闭窗口后最小化到托盘时也会继续运行。"),
    h("div", { class: "card-actions" }, startDaemonBtn, stopDaemonBtn)
  );

  function renderDaemon() {
    const running = Boolean(store.getState().status?.daemon_running);
    daemonBadge.textContent = running ? "运行中" : "已停止";
    daemonBadge.className = `badge ${running ? "ok" : "warn"}`;
    startDaemonBtn.hidden = running;
    stopDaemonBtn.hidden = !running;
  }

  const syncCard = h(
    "div",
    { class: "card" },
    h("div", { class: "card-title" }, "手动同步"),
    h("p", { class: "card-desc" }, "立即扫描对话并上传完整备份，同时处理等待中的项目备份，不用等下一个周期。"),
    h("div", { class: "card-actions" }, actionButton("立即同步", "btn-primary", () => run("sync-now", { okMsg: "同步完成", label: "同步" }))),
    h(
      "details",
      { class: "collapse" },
      h("summary", {}, "开发者诊断"),
      h(
        "div",
        { class: "collapse-body" },
        h("p", { class: "card-desc" }, "用于排查同步问题，日常不需要使用。"),
        h(
          "div",
          { class: "card-actions" },
          actionButton("补发变更通知", "btn-ghost btn-sm", () => run("notify-change", { okMsg: "已向服务器补发变更通知", label: "补发变更通知" }), {
            title: "让服务器重新记录这台电脑有未同步的变更",
          }),
          actionButton("模拟一次 Hook 事件", "btn-ghost btn-sm", () => run("test-capture", { okMsg: "已模拟一次 Hook 事件，结果见日志", label: "模拟 Hook 事件" }), {
            title: "检查 Hook 处理流程是否正常",
          })
        )
      )
    )
  );

  const panelDiagnosis = h("div", {}, h("div", { class: "section grid grid-2" }, daemonCard, syncCard));

  // ==================== 分区切换 ====================
  const panels = { server: panelServer, policy: panelPolicy, automation: panelAutomation, diagnosis: panelDiagnosis };
  const loadedTabs = new Set();

  function showTab(key) {
    for (const [name, panel] of Object.entries(panels)) panel.hidden = name !== key;
    saveBar.hidden = !FORM_TABS.has(key);
    if (loadedTabs.has(key)) return;
    loadedTabs.add(key);
    if (key === "server") {
      loadDeployConfig();
      checkServerCompatibility();
    } else if (key === "automation") {
      refreshTaskStatus();
      refreshAppInstall();
      checkAppUpdate();
    }
  }

  const tabs = createSubTabs(TABS, { initial: sub, onChange: showTab, label: "设置分区" });

  // ==================== 保存 ====================
  const saveState = h("span", { class: "save-state", role: "status" }, "所有修改都已保存");
  const discardBtn = h("button", { class: "btn btn-ghost", type: "button", hidden: true }, "放弃修改");
  const saveBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true, title: "Ctrl + S" }, "保存设置");
  const saveBar = h("div", { class: "save-bar" }, saveState, h("div", { class: "row-actions" }, discardBtn, saveBtn));
  discardBtn.addEventListener("click", discardChanges);
  saveBtn.addEventListener("click", doSave);

  function setDirty(value) {
    if (dirty === value) return;
    dirty = value;
    saveBar.classList.toggle("dirty", value);
    saveState.textContent = value ? "有未保存的修改" : "所有修改都已保存";
    discardBtn.hidden = !value;
    saveBtn.disabled = !value;
    if (value && !releaseGuard) releaseGuard = setLeaveGuard(confirmLeave);
    if (!value && releaseGuard) {
      releaseGuard();
      releaseGuard = null;
    }
  }

  function collectPayload() {
    const existing = config();
    for (const spec of NUMBER_FIELDS) f[spec.key].removeAttribute("aria-invalid");
    f.server_url.removeAttribute("aria-invalid");

    const serverUrl = f.server_url.value.trim();
    if (serverUrl && !/^https?:\/\/\S+$/i.test(serverUrl)) {
      f.server_url.setAttribute("aria-invalid", "true");
      tabs.select("server");
      f.server_url.focus();
      showToast("服务器地址需要以 http:// 或 https:// 开头", "error");
      return null;
    }

    const payload = {
      server_url: serverUrl,
      device_id: f.device_id.value.trim(),
      disaster_backup_enabled: f.disaster_backup_enabled.checked,
      full_backup_enabled: f.full_backup_enabled.checked,
      full_backup_include_config: f.full_backup_include_config.checked,
      full_backup_include_memories: f.full_backup_include_memories.checked,
      full_backup_encryption_enabled: f.full_backup_encryption_enabled.checked,
      full_backup_auto_upload: f.full_backup_auto_upload.checked,
      full_backup_allow_plaintext_upload: f.full_backup_allow_plaintext_upload.checked,
      project_auto_backup_on_codex_stop: f.project_auto_backup_on_codex_stop.checked,
      desktop_close_behavior: f.desktop_close_behavior.value || "ask",
    };

    for (const spec of NUMBER_FIELDS) {
      const input = f[spec.key];
      const raw = input.value.trim();
      if (raw === "") {
        // 留空 = 保持原值
        if (existing[spec.config] != null) payload[spec.config] = existing[spec.config];
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n) || n < spec.min) {
        input.setAttribute("aria-invalid", "true");
        tabs.select("policy");
        input.focus();
        showToast(`「${spec.label}」需要填写不小于 ${spec.min} 的数字`, "error");
        return null;
      }
      payload[spec.config] = Math.round(n * spec.scale);
    }

    const apiToken = f.api_token.value.trim();
    if (apiToken || !existing.api_token_configured) payload.api_token = apiToken;
    const secret = f.full_backup_encryption_passphrase.value;
    if (secret) payload.full_backup_encryption_passphrase = secret;
    return payload;
  }

  let saving = false;
  async function doSave() {
    if (!dirty || saving) return;
    const payload = collectPayload();
    if (!payload) return;
    const before = config();
    const serverChanged = payload.server_url !== (before.server_url || "") || Boolean(payload.api_token);
    saving = true;
    saveBtn.disabled = true;
    saveBtn.classList.add("is-busy");
    try {
      await saveConfig(payload);
      f.api_token.value = "";
      f.full_backup_encryption_passphrase.value = "";
      setDirty(false);
      await refreshStatus(store);
      fillForm(true);
      showToast(payload.full_backup_encryption_passphrase ? "设置已保存，已启用加密口令" : "设置已保存", "success");
      if (serverChanged) {
        checkConnection(store, { force: true });
        compatData = null;
        checkServerCompatibility();
      }
    } catch (e) {
      showToast(`保存失败：${e.message || e}`, "error");
    } finally {
      saving = false;
      saveBtn.classList.remove("is-busy");
      saveBtn.disabled = !dirty;
    }
  }

  function discardChanges() {
    f.api_token.value = "";
    f.full_backup_encryption_passphrase.value = "";
    for (const spec of NUMBER_FIELDS) f[spec.key].removeAttribute("aria-invalid");
    f.server_url.removeAttribute("aria-invalid");
    setDirty(false);
    fillForm(true);
    showToast("已恢复为保存的设置", "info");
  }

  // ==================== 用配置填充表单 ====================
  const setValue = (el, value, force) => {
    if (!el || (!force && document.activeElement === el)) return;
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value == null ? "" : String(value);
  };

  function fillServerFields() {
    const cfg = config();
    setValue(f.server_url, cfg.server_url, true);
    setValue(f.device_id, cfg.device_id, true);
    f.api_token.value = "";
    f.api_token.placeholder = cfg.api_token_configured ? "已保存，留空保持不变" : "填写服务器的访问令牌";
  }

  function fillForm(force = false) {
    if (dirty && !force) return; // 编辑中不覆盖用户的输入
    const cfg = config();
    setValue(f.server_url, cfg.server_url, force);
    setValue(f.device_id, cfg.device_id, force);
    setValue(f.disaster_backup_enabled, cfg.disaster_backup_enabled, force);
    setValue(f.full_backup_enabled, cfg.full_backup_enabled, force);
    setValue(f.full_backup_include_config, cfg.full_backup_include_config, force);
    setValue(f.full_backup_include_memories, cfg.full_backup_include_memories, force);
    setValue(f.full_backup_encryption_enabled, cfg.full_backup_encryption_enabled !== false, force);
    setValue(f.full_backup_auto_upload, cfg.full_backup_auto_upload !== false, force);
    setValue(f.full_backup_allow_plaintext_upload, cfg.full_backup_allow_plaintext_upload, force);
    setValue(f.project_auto_backup_on_codex_stop, cfg.project_auto_backup_on_codex_stop, force);
    setValue(f.desktop_close_behavior, cfg.desktop_close_behavior || "ask", force);
    for (const spec of NUMBER_FIELDS) setValue(f[spec.key], toUnit(cfg[spec.config], spec.scale), force);
    f.api_token.placeholder = cfg.api_token_configured ? "已保存，留空保持不变" : "填写服务器的访问令牌";
    f.full_backup_encryption_passphrase.placeholder = cfg.full_backup_encryption_passphrase_configured
      ? "已设置口令，留空保持不变"
      : "留空则使用本机自动生成的密钥";
  }

  // ==================== 装载 ====================
  fill(root, tabs.el, panelServer, panelPolicy, panelAutomation, panelDiagnosis, saveBar);

  const onKeydown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && !saveBar.hidden) {
      event.preventDefault();
      doSave();
    }
  };
  root.addEventListener("keydown", onKeydown);

  fillForm(true);
  renderEncStatus();
  renderCompatibility();
  renderHooks();
  renderDaemon();
  renderTask();
  renderAppInstall();
  renderAppUpdate();
  showTab(tabs.active);

  const unsubs = [
    store.select((s) => s.status?.config, () => {
      fillForm();
      renderEncStatus();
    }),
    store.select((s) => s.status?.hooks, renderHooks),
    store.select((s) => s.status?.daemon_running, renderDaemon),
    store.select((s) => s.status?.app_install, () => {
      if (!appInstallData) renderAppInstall();
    }),
  ];

  return () => {
    alive = false;
    root.removeEventListener("keydown", onKeydown);
    for (const unsub of unsubs) unsub();
    if (releaseGuard) releaseGuard();
  };
}
