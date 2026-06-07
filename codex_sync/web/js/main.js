// 应用入口：建 store、挂常驻面板(日志/抽屉)、绑顶栏、注册路由、启动轮询。
// 视图按路由动态 import，未实现的视图优雅降级为占位，不影响整体加载。
import { createStore, initialState } from "./store.js";
import { startStatusPolling, refreshStatus } from "./poller.js";
import { DEFAULT_ROUTE, currentRoute, onRouteChange } from "./router.js";
import { runAction } from "./api.js";
import { showToast } from "./toast.js";
import { $ } from "./dom.js";

const store = createStore(initialState());

const VIEW_META = {
  overview: ["首页", "数据同步与灾备状态"],
  conversations: ["对话与备份", "管理 Windows/WSL 的完整对话备份、导入迁移、渠道合并与浏览"],
  project: ["项目备份", "选择项目目录并上传代码快照到同步服务器"],
  settings: ["系统设置", "配置服务器凭证、调整备份策略、控制后台任务与高级故障诊断"],
};

const viewRoot = $("view-root");
let currentUnmount = null;
const moduleCache = new Map();

async function loadView(route) {
  if (!moduleCache.has(route)) {
    moduleCache.set(route, import(`./views/${route}.js`));
  }
  return moduleCache.get(route);
}

async function renderRoute(route) {
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.dataset.route === route);
  }
  const [title, sub] = VIEW_META[route] || VIEW_META[DEFAULT_ROUTE];
  $("view-title").textContent = title;
  $("view-sub").textContent = sub;

  if (currentUnmount) {
    try {
      currentUnmount();
    } catch {
      /* ignore */
    }
    currentUnmount = null;
  }
  viewRoot.replaceChildren();
  store.setState({ route });

  try {
    const mod = await loadView(route);
    currentUnmount = mod.mount(viewRoot, store) || null;
  } catch (err) {
    const div = document.createElement("div");
    div.className = "empty";
    div.textContent = `视图加载失败：${err.message || err}`;
    viewRoot.replaceChildren(div);
  }
}

// 常驻面板（日志、抽屉）：动态挂载，未实现时静默跳过。
async function mountPersistent(route, rootId) {
  try {
    const mod = await import(`./views/${route}.js`);
    mod.mount($(rootId), store);
  } catch {
    /* 该面板尚未实现 */
  }
}

function updateTopbar() {
  const { status, statusError } = store.getState();
  const pill = $("conn-pill");
  if (pill) {
    if (status?.config?.server_url) {
      pill.dataset.state = "ok";
      pill.textContent = "已配置";
    } else if (statusError && !status) {
      pill.dataset.state = "down";
      pill.textContent = "连接失败";
    } else {
      pill.dataset.state = "unknown";
      pill.textContent = "未配置服务器";
    }
  }
  const sub = $("brand-sub");
  if (sub && status?.config?.device_id) sub.textContent = status.config.device_id;
}

function bindTopbar() {
  $("refresh-btn").addEventListener("click", () => {
    refreshStatus(store)
      .then(() => showToast("状态已刷新", "success"))
      .catch((e) => showToast(String(e.message || e), "error"));
  });

  $("sync-btn").addEventListener("click", async () => {
    const btn = $("sync-btn");
    btn.disabled = true;
    try {
      const result = await runAction("sync-now");
      store.setState({ console: result });
      showToast(result.error ? `同步失败：${result.error}` : "同步完成", result.error ? "error" : "success");
      await refreshStatus(store);
    } catch (e) {
      showToast(String(e.message || e), "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("logs-toggle").addEventListener("click", () => {
    const { logs } = store.getState();
    store.setState({ logs: { ...logs, panelOpen: !logs.panelOpen } });
  });

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const st = store.getState();
    if (st.drawer.open) store.setState({ drawer: { ...st.drawer, open: false } });
    else if (st.logs.panelOpen) store.setState({ logs: { ...st.logs, panelOpen: false } });
  });
}

// 启动
mountPersistent("logs", "logs-root");
mountPersistent("drawer", "drawer-root");
bindTopbar();
store.select((s) => s.status, updateTopbar);
store.select((s) => s.statusError, updateTopbar);
onRouteChange(renderRoute);
renderRoute(currentRoute());
startStatusPolling(store);
