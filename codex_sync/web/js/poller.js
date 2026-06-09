// 分层数据调度：
// - status：setTimeout 递归轮询（非 setInterval，避免请求慢于间隔时堆积），
//   页面隐藏(visibilitychange)时暂停、回到前台立即补拉；失败只记 statusError，保留上一帧 status。

import { getStatus } from "./api.js";

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
