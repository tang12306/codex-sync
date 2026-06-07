from __future__ import annotations

import time
from typing import Callable

from .config import AppConfig
from .full_backup import get_remote_device_state, scan_full_backup_changes
from .project_auto_backup import process_project_auto_backup_queue
from .server import flush_outbox, sync_once
from .util import utc_now


def run_daemon(config: AppConfig, stop_check: Callable[[], bool] | None = None, log: Callable[[str], None] | None = None) -> None:
    interval = max(30, int(config.sync_interval_seconds or 180))
    while True:
        if stop_check and stop_check():
            return
        try:
            result = sync_once(config, skip_unchanged=True)
            if log:
                log(f"{utc_now()} sync_once: {result}")
            flushed = flush_outbox(config)
            if log:
                log(f"{utc_now()} flush_outbox: {flushed}")
            if config.full_backup_enabled:
                full_backup = scan_full_backup_changes(config, create_package=True, notify_dirty=True, check_remote=True)
                if log:
                    log(f"{utc_now()} full_backup_scan: {full_backup}")
                remote_state = get_remote_device_state(config)
                if log:
                    log(f"{utc_now()} device_state: {remote_state}")
            project_auto_backup = process_project_auto_backup_queue(config, limit=3)
            if log:
                log(f"{utc_now()} project_auto_backup: {project_auto_backup}")
        except Exception as exc:  # noqa: BLE001 - daemon must keep running
            if log:
                log(f"{utc_now()} daemon error: {exc}")
        for _ in range(interval):
            if stop_check and stop_check():
                return
            time.sleep(1)
