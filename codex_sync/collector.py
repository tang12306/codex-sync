from __future__ import annotations

import json
import os
import uuid
import hashlib
from pathlib import Path
from typing import Any

from .config import AppConfig
from .paths import app_dir, codex_home, ensure_app_dirs
from .redact import redact_json_line, redact_obj, redact_text
from .util import append_jsonl, read_tail, utc_now


def events_file() -> Path:
    stamp = utc_now()[:10]
    return app_dir() / "events" / f"{stamp}.jsonl"


def capture_event(event: str, raw_stdin: str, config: AppConfig, cwd: str | None = None) -> dict[str, Any]:
    ensure_app_dirs()
    payload: Any
    stripped = raw_stdin.strip()
    if stripped:
        try:
            payload = redact_obj(json.loads(stripped))
        except json.JSONDecodeError:
            payload = redact_text(stripped)
    else:
        payload = {}

    item = {
        "id": str(uuid.uuid4()),
        "type": "codex_hook_event",
        "event": event,
        "created_at": utc_now(),
        "device_id": config.device_id,
        "cwd": cwd or os.getcwd(),
        "payload": payload,
    }
    append_jsonl(events_file(), item)
    return item


def collect_codex_state(config: AppConfig) -> dict[str, Any]:
    home = codex_home()
    state: dict[str, Any] = {
        "codex_home": str(home),
        "exists": home.exists(),
        "has_config": (home / "config.toml").exists(),
        "has_agents": (home / "AGENTS.md").exists() or (home / "AGENTS.override.md").exists(),
        "has_hooks": (home / "hooks.json").exists(),
        "history_tail": "",
        "memories": [],
        "config_files": {},
        "configs": {}
    }
    if home.exists():
        configs: dict[str, str] = {}
        config_files: dict[str, dict[str, Any]] = {}
        for name in ("config.toml", "AGENTS.md", "AGENTS.override.md"):
            p = home / name
            if p.exists():
                try:
                    size = p.stat().st_size
                    content = p.read_text(encoding="utf-8", errors="replace") if size < 256 * 1024 else ""
                    config_files[name] = {
                        "exists": True,
                        "bytes": size,
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                        "redacted_preview": redact_text(content[:4096]) if content else "",
                    }
                    if config.upload_raw_config_files and content:
                        configs[name] = content
                except Exception:
                    continue
        state["config_files"] = config_files
        state["configs"] = configs

    if config.include_history_tail:
        raw = read_tail(home / "history.jsonl", max_bytes=64 * 1024)
        if raw:
            state["history_tail"] = "\n".join(redact_json_line(line) for line in raw.splitlines()[-40:])
    if config.include_memories:
        memory_dir = home / "memories"
        if memory_dir.exists():
            state["memories"] = [str(path.relative_to(memory_dir)) for path in memory_dir.rglob("*") if path.is_file()][:100]
    return state
