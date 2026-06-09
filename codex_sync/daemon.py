from __future__ import annotations

import time
from typing import Callable

from .config import AppConfig, load_config
from .full_backup import get_remote_device_state, scan_full_backup_changes
from .project_auto_backup import enqueue_project_auto_backup, process_project_auto_backup_queue
from .util import utc_now


def _enqueue_realtime_projects(config: AppConfig, realtime_last: dict[str, float], log: Callable[[str], None] | None) -> None:
    """对受监控项目，按 realtime_backup_interval_seconds 周期入队一次工作区备份（含未提交改动）。

    实际是否上传由内容去重决定（无改动则 _should_skip 跳过），避免重复传同一状态。
    """
    projects = getattr(config, "realtime_backup_projects", None) or []
    if not projects:
        return
    interval = max(60, int(getattr(config, "realtime_backup_interval_seconds", 300) or 300))
    now = time.monotonic()
    for path in projects:
        if not isinstance(path, str) or not path.strip():
            continue
        if now - realtime_last.get(path, 0.0) >= interval:
            try:
                result = enqueue_project_auto_backup(path, config, reason="realtime")
                realtime_last[path] = now
                if log:
                    log(f"{utc_now()} realtime_enqueue {path}: queued={result.get('queued')} skipped={result.get('skipped')}")
            except Exception as exc:  # noqa: BLE001 - 单个项目失败不应中断 daemon
                if log:
                    log(f"{utc_now()} realtime_enqueue error {path}: {exc}")


def run_daemon(config: AppConfig, stop_check: Callable[[], bool] | None = None, log: Callable[[str], None] | None = None) -> None:
    realtime_last: dict[str, float] = {}
    while True:
        if stop_check and stop_check():
            return
        try:
            config = load_config()  # 每轮刷新配置，让设置变更（含实时备份项目/间隔）即时生效
            if config.full_backup_enabled:
                full_backup = scan_full_backup_changes(config, create_package=True, notify_dirty=True, check_remote=True, upload=True)
                if log:
                    log(f"{utc_now()} full_backup_scan: {full_backup}")
                remote_state = get_remote_device_state(config)
                if log:
                    log(f"{utc_now()} device_state: {remote_state}")
            _enqueue_realtime_projects(config, realtime_last, log)
            project_auto_backup = process_project_auto_backup_queue(config, limit=5)
            if log:
                log(f"{utc_now()} project_auto_backup: {project_auto_backup}")
        except Exception as exc:  # noqa: BLE001 - daemon must keep running
            if log:
                log(f"{utc_now()} daemon error: {exc}")
        interval = max(30, int(config.sync_interval_seconds or 180))
        for _ in range(interval):
            if stop_check and stop_check():
                return
            time.sleep(1)
