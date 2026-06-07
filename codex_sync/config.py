from __future__ import annotations

import json
import socket
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import config_path, ensure_app_dirs


@dataclass
class AppConfig:
    server_url: str = ""
    api_token: str = ""
    device_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
    sync_interval_seconds: int = 180
    max_untracked_copy_bytes: int = 256 * 1024
    redact_enabled: bool = True
    include_history_tail: bool = True
    include_memories: bool = False
    upload_raw_config_files: bool = False
    disaster_backup_enabled: bool = True
    disaster_backup_min_interval_seconds: int = 24 * 60 * 60
    disaster_backup_max_file_bytes: int = 100 * 1024 * 1024
    disaster_backup_max_total_bytes: int = 512 * 1024 * 1024
    full_backup_enabled: bool = True
    full_backup_include_config: bool = False
    full_backup_include_memories: bool = False
    full_backup_allow_plaintext_upload: bool = False
    full_backup_max_file_bytes: int = 256 * 1024 * 1024
    full_backup_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    full_backup_quiet_seconds: int = 60
    full_backup_retention_count: int = 20
    full_backup_retention_max_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        base = cls()
        allowed = asdict(base).keys()
        merged = {key: data.get(key, getattr(base, key)) for key in allowed}
        return cls(**merged)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["api_token"] = "<configured>" if self.api_token else ""
        return data


def load_config() -> AppConfig:
    ensure_app_dirs()
    path = config_path()
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(".json.bak")
        path.replace(backup)
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    return AppConfig.from_dict(data)


def save_config(config: AppConfig) -> Path:
    ensure_app_dirs()
    path = config_path()
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
    return path
