// 轻量响应式 store：状态 + 订阅 + 选择器。
// setState 浅合并顶层 key；select(selector, fn) 只在 selector 结果按 Object.is 变化时回调，
// 避免「status 变了但 logs 没变也重渲染 logs」这类无谓刷新。
//
// 用法约定：未变化的片段必须保持引用稳定（不要每次 setState 都重建无关对象），
// select 才能正确去重。需要监听多个片段时，对同一 render 注册多个 select。

export function createStore(initialState) {
  let state = initialState;
  const listeners = new Set();

  function getState() {
    return state;
  }

  function setState(patch) {
    const next = typeof patch === "function" ? patch(state) : patch;
    if (!next) return;
    const prev = state;
    state = { ...state, ...next };
    for (const listener of [...listeners]) listener(state, prev);
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function select(selector, listener) {
    let current = selector(state);
    return subscribe((nextState) => {
      const next = selector(nextState);
      if (!Object.is(next, current)) {
        const prev = current;
        current = next;
        listener(next, prev);
      }
    });
  }

  return { getState, setState, subscribe, select };
}

// 控制台首帧的初始 state。
export function initialState() {
  return {
    status: null,
    statusError: null,
    route: "overview",
    snapshots: { items: [], loading: false, error: null, loadedAt: null },
    drawer: {
      open: false,
      summary: null,
      detail: null,
      preview: null,
      resume: null,
      status: "",
      statusType: "info",
      busy: false,
    },
    logs: { filter: "all", search: "", autoScroll: true, panelOpen: false },
    console: null,
    overview: {
      appUpdate: null,
      appUpdateLoadedAt: 0,
      compatibility: null,
      compatibilityLoadedAt: 0,
      health: null,
      healthLoadedAt: 0,
      healthExpanded: false,
    },
  };
}
