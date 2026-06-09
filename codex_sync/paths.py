from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = ".codex-sync"


def home_dir() -> Path:
    return Path.home()


def app_dir() -> Path:
    override = os.environ.get("CODEX_SYNC_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return home_dir() / APP_DIR_NAME


def ensure_app_dirs() -> Path:
    root = app_dir()
    for child in (
        root,
        root / "events",
        root / "snapshots",
        root / "logs",
        root / "wsl",
    ):
        child.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return app_dir() / "config.json"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return home_dir() / ".codex"
