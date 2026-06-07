// 极简 hash 路由。

export const ROUTES = ["overview", "conversations", "project", "settings"];
export const DEFAULT_ROUTE = "overview";

export function currentRoute() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return ROUTES.includes(hash) ? hash : DEFAULT_ROUTE;
}

export function navigate(route) {
  window.location.hash = `#/${route}`;
}

// 注册路由变化回调，返回一个可手动触发当前路由的函数（用于首帧）。
export function onRouteChange(handler) {
  const fire = () => handler(currentRoute());
  window.addEventListener("hashchange", fire);
  return fire;
}
