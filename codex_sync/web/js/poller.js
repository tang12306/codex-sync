// 分层数据调度：
// - status：setTimeout 递归轮询（非 setInterval，避免请求慢于间隔时堆积），
//   页面隐藏(visibilitychange)时暂停、回到前台立即补拉；失败只记 statusError，保留上一帧 status。
// - snapshots：按需拉取（进入同步区或手动刷新），带 TTL 缓存，不进 status 轮询，不每 5 秒打远端。

import { getStatus, runAction } from "./api.js";

const SNAPSHOT_TTL_MS = 30000;

export function startStatusPolling(store, intervalMs = 5000) {
  let timer = null;

  async function tick() {
    await refreshStatus(store);
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (document.visibilityState === "visible") {
      timer = setTimeout(tick, intervalMs);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      tick();
    } else {
      clearTimeout(timer);
    }
  });

  tick();
  return () => clearTimeout(timer);
}

// 拉取一次 status 写入 store（顶栏「刷新」、动作完成后复用）。
export async function refreshStatus(store) {
  try {
    const status = await getStatus();
    store.setState({ status, statusError: null });
  } catch (err) {
    store.setState({ statusError: String(err.message || err) });
  }
}

// 拉取远端快照列表写入 store.snapshots。force=true 跳过 TTL/缓存。
export async function fetchSnapshots(store, { force = false } = {}) {
  const { snapshots } = store.getState();
  const fresh = snapshots.loadedAt && Date.now() - snapshots.loadedAt < SNAPSHOT_TTL_MS;
  if (!force && (snapshots.loading || fresh)) return;
  store.setState({ snapshots: { ...snapshots, loading: true, error: null } });
  try {
    const result = await runAction("list-snapshots");
    const items = Array.isArray(result.snapshots) ? result.snapshots : [];
    store.setState({
      snapshots: { items, loading: false, error: result.error || null, loadedAt: Date.now() },
    });
  } catch (err) {
    store.setState({
      snapshots: { ...store.getState().snapshots, loading: false, error: String(err.message || err) },
    });
  }
}
