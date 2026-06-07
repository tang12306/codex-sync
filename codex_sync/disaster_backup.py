from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from .config import AppConfig
from .paths import app_dir, codex_home, ensure_app_dirs
from .util import safe_filename, utc_now, write_json


EXACT_FILES = {
    ".codex-global-state.json",
    "AGENTS.md",
    "AGENTS.override.md",
    "config.toml",
    "history.jsonl",
    "hooks.json",
    "models_cache.json",
    "session_index.jsonl",
}

FILE_PREFIXES = (
    "goals_",
    "logs_",
    "memories_",
    "state_",
)

FILE_SUFFIXES = (
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
)

DIRECTORIES = {
    "memories",
    "sessions",
    "skills",
    "threads",
}

DENY_NAMES = {
    ".env",
    "auth.json",
    "cap_sid",
    "id_ed25519",
    "id_rsa",
}

DENY_SUFFIXES = {
    ".crt",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
}

DENY_PARTS = {
    ".sandbox",
    ".sandbox-bin",
    ".sandbox-secrets",
    ".tmp",
    "browser",
    "cache",
    "computer-use",
    "node_repl",
    "process_manager",
    "tmp",
    "vendor_imports",
}


def disaster_backup_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "disaster-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _latest_manifest() -> dict[str, Any] | None:
    manifests = sorted(disaster_backup_dir().glob("*.manifest.json"), reverse=True)
    if not manifests:
        return None
    try:
        return json.loads(manifests[0].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _iso_to_epoch(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


def should_create_backup(config: AppConfig, force: bool = False) -> tuple[bool, str]:
    if force:
        return True, "force"
    if not config.disaster_backup_enabled:
        return False, "disabled"
    latest = _latest_manifest()
    if not latest:
        return True, "no previous backup"
    try:
        age = _iso_to_epoch(utc_now()) - _iso_to_epoch(str(latest.get("created_at")))
    except (TypeError, ValueError):
        return True, "invalid previous manifest"
    if age >= config.disaster_backup_min_interval_seconds:
        return True, f"previous backup is {int(age)}s old"
    return False, f"latest backup is {int(age)}s old"


def _safe_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in DENY_PARTS for part in rel.parts):
        return False
    if path.name in DENY_NAMES:
        return False
    if path.suffix.lower() in DENY_SUFFIXES:
        return False
    return True


def _top_level_file_allowed(path: Path) -> bool:
    name = path.name
    if name in EXACT_FILES:
        return True
    return any(name.startswith(prefix) for prefix in FILE_PREFIXES) and any(name.endswith(suffix) for suffix in FILE_SUFFIXES)


def _iter_backup_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for child in root.iterdir():
        if child.is_file() and _top_level_file_allowed(child) and _safe_file(child, root):
            files.append(child)
        elif child.is_dir() and child.name in DIRECTORIES:
            for file in child.rglob("*"):
                if file.is_file() and _safe_file(file, root):
                    files.append(file)
    return files


def create_disaster_backup(config: AppConfig, reason: str = "manual", force: bool = False) -> dict[str, Any]:
    allowed, why = should_create_backup(config, force=force)
    latest = _latest_manifest()
    if not allowed:
        return {"created": False, "reason": why, "latest": latest}

    root = codex_home()
    stamp = utc_now().replace(":", "").replace("+", "Z")
    archive = disaster_backup_dir() / f"{stamp}-{safe_filename(reason)}.zip"
    manifest_path = archive.with_suffix(".manifest.json")
    files = _iter_backup_files(root)
    included: list[str] = []
    skipped: list[dict[str, Any]] = []
    total = 0

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in files:
            size = file.stat().st_size
            rel = file.relative_to(root)
            if size > config.disaster_backup_max_file_bytes:
                skipped.append({"path": str(rel), "reason": "file too large", "bytes": size})
                continue
            if total + size > config.disaster_backup_max_total_bytes:
                skipped.append({"path": str(rel), "reason": "backup size limit", "bytes": size})
                continue
            try:
                zf.write(file, arcname=f"codex/{rel.as_posix()}")
                included.append(str(rel))
                total += size
            except OSError as exc:
                skipped.append({"path": str(rel), "reason": str(exc), "bytes": size})

    manifest = {
        "created": True,
        "created_at": utc_now(),
        "reason": reason,
        "codex_home": str(root),
        "archive": str(archive),
        "included_count": len(included),
        "included_bytes": total,
        "included": included,
        "skipped": skipped,
        "excluded_by_policy": sorted(DENY_NAMES),
        "note": "This is a local disaster backup. It may contain conversation content and is not uploaded by codex-sync.",
    }
    write_json(manifest_path, manifest)
    return manifest
