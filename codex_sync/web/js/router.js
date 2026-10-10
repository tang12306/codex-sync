// 极简 hash 路由：#/route 或 #/route/sub（sub 是页面内的二级页签，刷新后保持）。

export const ROUTES = ["overview", "conversations", "project", "backups", "settings"];
export const DEFAULT_ROUTE = "overview";

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [route = "", sub = ""] = raw.split("/");
  if (!ROUTES.includes(route)) return { route: DEFAULT_ROUTE, sub: "" };
  let decoded = sub;
  try {
    decoded = decodeURIComponent(sub);
  } catch {
    /* 保留原样 */
  }
  return { route, sub: decoded };
}

export function currentRoute() {
  return parseHash().route;
}

export function currentSubRoute() {
  return parseHash().sub;
}

function hashFor(route, sub) {
  return sub ? `#/${route}/${encodeURIComponent(sub)}` : `#/${route}`;
}

export function navigate(route, sub = "") {
  window.location.hash = hashFor(route, sub);
}

let lastHash = window.location.hash;

// 页面内切换二级页签时更新地址栏；replaceState 不触发 hashchange，因此不会重渲染视图。
export function setSubRoute(sub) {
  const hash = hashFor(currentRoute(), sub);
  if (window.location.hash !== hash) history.replaceState(null, "", hash);
  lastHash = window.location.hash;
}

// 离开守卫：视图有未保存修改时注册，返回 Promise<boolean>（true 表示允许离开）。
let leaveGuard = null;

export function setLeaveGuard(fn) {
  leaveGuard = fn;
  return () => {
    if (leaveGuard === fn) leaveGuard = null;
  };
}

// 注册路由变化回调 handler(route, sub)，返回一个可手动触发当前路由的函数。
export function onRouteChange(handler) {
  let guarding = false;
  const fire = async () => {
    const target = window.location.hash;
    if (target === lastHash) return;
    if (leaveGuard) {
      if (guarding) return;
      guarding = true;
      // 先把地址栏还原，等用户确认后再真正跳转
      history.replaceState(null, "", lastHash);
      let allowed = false;
      try {
        allowed = await leaveGuard();
      } finally {
        guarding = false;
      }
      if (!allowed) return;
      history.replaceState(null, "", target);
    }
    lastHash = window.location.hash;
    const { route, sub } = parseHash();
    handler(route, sub);
  };
  window.addEventListener("hashchange", fire);
  return fire;
}
