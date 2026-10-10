// 应用入口：建 store、挂常驻日志面板、绑顶栏、注册路由、启动轮询与连接检查。
// 视图按路由动态 import，加载失败时显示错误占位，不影响整体。
import { createStore, initialState } from "./store.js";
import { startStatusPolling, refreshStatus } from "./poller.js";
import { DEFAULT_ROUTE, currentRoute, currentSubRoute, navigate, onRouteChange } from "./router.js";
import { runAction } from "./api.js";
import { showToast } from "./toast.js";
import { $ } from "./dom.js";
import { isDialogOpen } from "./dialog.js";
import { checkConnection } from "./connection.js";
import { failureReason, formatDateTime, isFailed } from "./format.js";

const store = createStore(initialState());

const VIEW_META = {
  overview: ["首页", "本机同步状态与常用任务"],
  conversations: ["对话与渠道", "浏览、搜索和导出对话，整理不同渠道的对话记录"],
  project: ["项目备份", "把项目代码备份到同步服务器，并按版本恢复"],
  backups: ["对话备份与恢复", "Windows / WSL 完整对话备份、云端备份与导入恢复"],
  settings: ["系统设置", "服务器连接、备份策略、后台任务与应用安装"],
};

const CONNECTION_RECHECK_MS = 60 * 1000; // 每分钟看一次，实际请求受 5 分钟缓存限制

const viewRoot = $("view-root");
let currentUnmount = null;
let renderSeq = 0;
const moduleCache = new Map();

async function loadView(route) {
  if (!moduleCache.has(route)) {
    const pending = import(`./views/${route}.js`);
    moduleCache.set(route, pending);
    pending.catch(() => moduleCache.delete(route));
  }
  return moduleCache.get(route);
}

async function renderRoute(route) {
  const seq = (renderSeq += 1);
  for (const item of document.querySelectorAll(".nav-item")) {
    const active = item.dataset.route === route;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const [title, sub] = VIEW_META[route] || VIEW_META[DEFAULT_ROUTE];
  $("view-title").textContent = title;
  $("view-sub").textContent = sub;
  document.title = `${title} · Codex Sync`;

  if (currentUnmount) {
    try {
      currentUnmount();
    } catch {
      /* ignore */
    }
    currentUnmount = null;
  }
  viewRoot.replaceChildren();
  viewRoot.scrollTop = 0;
  store.setState({ route });

  try {
    const mod = await loadView(route);
    if (seq !== renderSeq) return; // 期间又切换了页面
    currentUnmount = mod.mount(viewRoot, store, { sub: currentSubRoute() }) || null;
  } catch (err) {
    if (seq !== renderSeq) return;
    const div = document.createElement("div");
    div.className = "status-line error";
    div.textContent = `页面加载失败：${err.message || err}`;
    viewRoot.replaceChildren(div);
  }
}

// 常驻面板（日志）：动态挂载，加载失败时静默跳过。
async function mountPersistent(route, rootId) {
  try {
    const mod = await import(`./views/${route}.js`);
    mod.mount($(rootId), store);
  } catch {
    /* 面板不可用时不影响主界面 */
  }
}

function serverHost(url) {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function updateConnectionPill() {
  const { status, statusError, connection } = store.getState();
  const pill = $("conn-pill");
  if (!pill) return;
  const serverUrl = status?.config?.server_url || "";
  const result = connection?.result || {};
  let state = "unknown";
  let text = "正在读取状态…";
  let title = "点击重新检查服务器连接";

  if (statusError) {
    state = "down";
    text = "本地服务断开";
    title = `无法连接本机控制台服务：${statusError}`;
  } else if (!status) {
    state = "checking";
  } else if (!serverUrl) {
    text = "未配置服务器";
    title = "点击前往设置填写服务器地址";
  } else {
    const host = serverHost(serverUrl);
    const checked = connection?.checkedAt ? `，${formatDateTime(new Date(connection.checkedAt).toISOString())} 检查` : "";
    switch (connection?.state) {
      case "ok":
        state = "ok";
        text = "已连接";
        title = `服务器 ${host} 连接正常${checked}。点击重新检查`;
        break;
      case "warn":
        state = "warn";
        text = "服务器需更新";
        title = `服务器 ${host} 版本过旧，部分功能不可用${checked}。点击重新检查`;
        break;
      case "down":
        state = "down";
        text = "连接失败";
        title = `无法连接服务器 ${host}：${result.error || "未知错误"}${checked}。点击重新检查`;
        break;
      default:
        state = "checking";
        text = "检查连接…";
        title = `正在检查服务器 ${host}`;
    }
  }
  pill.dataset.state = state;
  pill.textContent = text;
  pill.title = title;
  pill.setAttribute("aria-label", `服务器连接：${text}`);
}

function updateBrand() {
  const { status } = store.getState();
  const sub = $("brand-sub");
  if (sub && status?.config?.device_id) {
    sub.textContent = status.config.device_id;
    sub.title = `设备 ID：${status.config.device_id}`;
  }
  const foot = $("sidebar-foot");
  if (foot && status?.app_version) foot.textContent = `Codex Sync · v${status.app_version}`;
}

function bindTopbar() {
  const refreshBtn = $("refresh-btn");
  refreshBtn.addEventListener("click", async () => {
    refreshBtn.disabled = true;
    const ok = await refreshStatus(store);
    refreshBtn.disabled = false;
    if (!ok) {
      showToast(`刷新失败：${store.getState().statusError || "本地服务无响应"}`, "error");
      return;
    }
    checkConnection(store, { force: true });
    window.dispatchEvent(new CustomEvent("codex-sync:refresh"));
    showToast("状态已刷新", "success");
  });

  const syncBtn = $("sync-btn");
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncBtn.classList.add("is-busy");
    try {
      const result = await runAction("sync-now");
      if (isFailed(result)) showToast(`同步失败：${failureReason(result)}`, "error");
      else showToast("同步完成", "success");
      await refreshStatus(store);
    } catch (e) {
      showToast(`同步失败：${e.message || e}`, "error");
    } finally {
      syncBtn.disabled = false;
      syncBtn.classList.remove("is-busy");
    }
  });

  $("conn-pill").addEventListener("click", async () => {
    const { status, statusError } = store.getState();
    if (statusError) {
      const ok = await refreshStatus(store);
      if (!ok) showToast("本机控制台服务仍无响应，请重新打开 Codex Sync", "error");
      return;
    }
    if (!status?.config?.server_url) {
      navigate("settings", "server");
      return;
    }
    const result = await checkConnection(store, { force: true });
    if (result?.compatible) showToast("服务器连接正常", "success");
    else if (result?.success && result?.needs_update) showToast("服务器可以连接，但版本过旧，请在系统设置里更新部署", "warning");
    else if (result) showToast(`服务器连接失败：${result.error || "未知错误"}`, "error");
  });

  $("logs-toggle").addEventListener("click", () => {
    const { logs } = store.getState();
    store.setState({ logs: { ...logs, panelOpen: !logs.panelOpen } });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || isDialogOpen()) return;
    const st = store.getState();
    if (st.logs.panelOpen) store.setState({ logs: { ...st.logs, panelOpen: false } });
  });
}

// 启动
mountPersistent("logs", "logs-root");
bindTopbar();
store.select((s) => s.status, () => {
  updateConnectionPill();
  updateBrand();
});
store.select((s) => s.statusError, updateConnectionPill);
store.select((s) => s.connection, updateConnectionPill);
store.select((s) => s.status?.config?.server_url, () => checkConnection(store));
store.select(
  (s) => s.logs.panelOpen,
  (open) => $("logs-toggle").setAttribute("aria-expanded", open ? "true" : "false")
);
setInterval(() => {
  if (document.visibilityState === "visible" && store.getState().status) checkConnection(store);
}, CONNECTION_RECHECK_MS);
onRouteChange((route) => renderRoute(route));
renderRoute(currentRoute());
updateConnectionPill();
startStatusPolling(store);
