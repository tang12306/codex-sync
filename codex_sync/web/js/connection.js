// 服务器连接状态：顶栏连接指示、首页提醒、设置页共用同一份检查结果。
// 5 分钟缓存 + 并发去重，避免几个地方同时触发重复请求。
import { runAction } from "./api.js";

const CACHE_MS = 5 * 60 * 1000;
let inflight = null;

export function connectionStateOf(result) {
  if (!result) return "unknown";
  if (result.compatible) return "ok";
  if (result.needs_update && result.success) return "warn";
  return "down";
}

export async function checkConnection(store, { force = false } = {}) {
  const state = store.getState();
  const serverUrl = state.status?.config?.server_url || "";
  const conn = state.connection || {};
  if (!serverUrl) {
    store.setState({ connection: { state: "unconfigured", result: null, checkedAt: 0, serverUrl: "" } });
    return null;
  }
  const fresh = conn.result && conn.serverUrl === serverUrl && Date.now() - conn.checkedAt < CACHE_MS;
  if (!force && fresh) return conn.result;
  if (inflight) return inflight;

  // 后台定时复查时保留上次结果，只有首次检查或用户主动点击时才显示「检查中」。
  const showChecking = force || !conn.result || conn.serverUrl !== serverUrl;
  store.setState({ connection: { ...conn, state: showChecking ? "checking" : conn.state, serverUrl } });

  inflight = (async () => {
    let result;
    try {
      result = await runAction("server-compatibility");
    } catch (err) {
      result = { success: false, compatible: false, error: String(err.message || err) };
    }
    const checkedAt = Date.now();
    store.setState((s) => ({
      connection: { state: connectionStateOf(result), result, checkedAt, serverUrl },
      overview: { ...s.overview, compatibility: result, compatibilityLoadedAt: checkedAt },
    }));
    return result;
  })().finally(() => {
    inflight = null;
  });
  return inflight;
}
