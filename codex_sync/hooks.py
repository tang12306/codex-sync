from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .paths import codex_home
from .util import pythonw_executable


HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop")


def hooks_path() -> Path:
    return codex_home() / "hooks.json"


def _hook_invoker() -> list[str]:
    """hook 调用前缀：打包 exe 用自身（CodexSync.exe <子命令>，路径稳定）；
    源码模式用 pythonw + hook_runner.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    script = Path(__file__).resolve().parent / "hook_runner.py"
    return [pythonw_executable(), str(script)]


def _command(event: str) -> str:
    return subprocess.list2cmdline([*_hook_invoker(), "capture", "--event", event])


def _posix_command(event: str) -> str:
    return f"python3 -m codex_sync capture --event {event}"


def _hook_entry(event: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "commandWindows": _command(event),
                "command": _posix_command(event),
                "timeout": 30,
                "statusMessage": f"Codex Sync capture: {event}",
            }
        ]
    }
    if event == "SessionStart":
        entry["matcher"] = "startup|resume|clear|compact"
    if event in ("PreCompact", "PostCompact"):
        entry["matcher"] = "manual|auto"
    return entry


def _contains_codex_sync(entry: dict[str, Any]) -> bool:
    for hook in entry.get("hooks", []):
        command = (hook.get("commandWindows") or hook.get("command") or "").lower()
        # 同时匹配源码模式（codex_sync / codex-sync）与打包 exe（CodexSync.exe → codexsync）
        if "codex_sync" in command or "codex-sync" in command or "codexsync" in command:
            return True
    return False


def install_hooks() -> dict[str, Any]:
    from .config import load_config
    from .disaster_backup import create_disaster_backup

    protection = create_disaster_backup(load_config(), reason="before_install_hooks")
    path = hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        backup = path.with_suffix(".json.bak")
        if not backup.exists():
            backup.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        data = {"hooks": {}}

    hooks = data.setdefault("hooks", {})
    installed: list[str] = []
    updated: list[str] = []
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        had_existing = any(_contains_codex_sync(entry) for entry in entries)
        # 移除本工具旧的 hook 条目（命令可能已过时，如旧的 python.exe），再写入最新定义
        entries[:] = [entry for entry in entries if not _contains_codex_sync(entry)]
        entries.append(_hook_entry(event))
        (updated if had_existing else installed).append(event)

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"path": str(path), "installed": installed, "updated": updated, "preflight_backup": protection}


def uninstall_hooks(match: str | None = None) -> dict[str, Any]:
    """移除 hooks.json 里本工具的条目；match 不为空时，只移除命令里包含该字符串（如某个 exe 路径）的条目。"""
    path = hooks_path()
    if not path.exists():
        return {"success": True, "path": str(path), "removed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"success": False, "path": str(path), "removed": [], "error": "hooks.json 不是有效的 JSON，没有修改。"}
    needle = (match or "").lower()

    def ours(entry: Any) -> bool:
        if not isinstance(entry, dict) or not _contains_codex_sync(entry):
            return False
        if not needle:
            return True
        return any(needle in (hook.get("commandWindows") or hook.get("command") or "").lower() for hook in entry.get("hooks", []))

    hooks = data.get("hooks") if isinstance(data, dict) else None
    removed: list[str] = []
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            if not isinstance(entries, list):
                continue
            kept = [entry for entry in entries if not ours(entry)]
            if len(kept) == len(entries):
                continue
            removed.append(event)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if removed:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"success": True, "path": str(path), "removed": removed}


def hook_status() -> dict[str, Any]:
    path = hooks_path()
    if not path.exists():
        return {"path": str(path), "exists": False, "events": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"path": str(path), "exists": True, "valid_json": False, "events": []}
    events = []
    for event, entries in data.get("hooks", {}).items():
        if any(_contains_codex_sync(entry) for entry in entries):
            events.append(event)
    return {"path": str(path), "exists": True, "valid_json": True, "events": events}
