from __future__ import annotations

import json
import hashlib
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .git_backup import _is_safe_untracked, _project_root, create_project_backup_package, git_root, git_state, upload_project_backup
from .paths import app_dir, ensure_app_dirs
from .util import pythonw_executable, run_cmd, safe_filename, utc_now, write_json


HOOK_BEGIN = "# >>> codex-sync project auto backup >>>"
HOOK_END = "# <<< codex-sync project auto backup <<<"


def queue_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "project-auto-backup-queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    ensure_app_dirs()
    return app_dir() / "project-auto-backup-state.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_state() -> dict[str, Any]:
    data = _read_json(state_path())
    data.setdefault("projects", {})
    return data


def _write_state(data: dict[str, Any]) -> None:
    data.setdefault("projects", {})
    write_json(state_path(), data)


def _project_key(root: Path) -> str:
    return safe_filename(str(root).lower().replace(":", ""))


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _seconds_since(value: str) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()


def _git_commit(root: Path, ref: str = "HEAD") -> str:
    code, out, _ = run_cmd(["git", "rev-parse", ref], cwd=root)
    return out if code == 0 else ""


def _auto_project_root(cwd: str | Path | None) -> tuple[Path | None, bool]:
    git = git_root(cwd)
    if git:
        return git, True
    try:
        root = _project_root(cwd)
    except OSError:
        return None, False
    if not root.exists() or not root.is_dir():
        return None, False
    return root, False


def _filesystem_content_id(root: Path, config: AppConfig) -> str:
    digest = hashlib.sha256()
    copied = 0
    skipped = 0
    for src in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix() if item.exists() else str(item)):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(root)
            stat = src.stat()
        except OSError:
            skipped += 1
            continue
        rel_posix = rel.as_posix()
        if not _is_safe_untracked(rel) or stat.st_size > config.max_untracked_copy_bytes:
            skipped += 1
            continue
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        try:
            with src.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            skipped += 1
            continue
        digest.update(b"\0")
        copied += 1
    return f"filesystem:{copied}:{skipped}:{digest.hexdigest()}"


def _content_id(root: Path, mode: str, config: AppConfig, commit_ref: str | None = None, is_repo: bool | None = None) -> str:
    if is_repo is False or not git_root(root):
        return _filesystem_content_id(root, config)
    commit = _git_commit(root, commit_ref or "HEAD")
    if mode == "git_commit":
        return f"commit:{commit}"
    status = git_state(root, max_age=0).get("status", "")
    return f"worktree:{commit}:{status}"


def _mode_for_reason(reason: str) -> str:
    if reason == "codex-stop":
        return "full"
    return "git_commit" if reason == "git-post-commit" else "worktree"


def _queue_path(item_id: str) -> Path:
    return queue_dir() / f"{utc_now().replace(':', '').replace('+', 'Z')}-{safe_filename(item_id)}.json"


def enqueue_project_auto_backup(cwd: str | Path | None, config: AppConfig, *, reason: str = "manual", mode: str | None = None) -> dict[str, Any]:
    root, is_repo = _auto_project_root(cwd)
    if not root:
        return {"success": True, "skipped": True, "reason": "project directory not found", "cwd": str(cwd or Path.cwd())}
    mode = mode or _mode_for_reason(reason)
    if not is_repo:
        mode = "full"
    commit = _git_commit(root) if is_repo else ""
    item = {
        "id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "device_id": config.device_id,
        "cwd": str(root),
        "is_repo": is_repo,
        "reason": reason,
        "mode": mode,
        "commit": commit,
        "content_id": _content_id(root, mode, config, commit_ref=commit if mode == "git_commit" else None, is_repo=is_repo),
    }
    path = _queue_path(item["id"])
    write_json(path, item)
    return {"success": True, "queued": True, "path": str(path), "item": item}


def _queue_items() -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(queue_dir().glob("*.json")):
        data = _read_json(path)
        if data:
            items.append((path, data))
    return items


def _should_skip(root: Path, item: dict[str, Any], config: AppConfig) -> dict[str, Any] | None:
    state = _read_state()
    project = state.get("projects", {}).get(_project_key(root), {})
    content_id = str(item.get("content_id") or _content_id(root, str(item.get("mode") or "worktree"), config, item.get("commit"), is_repo=item.get("is_repo")))
    if project.get("last_content_id") == content_id:
        return {"success": True, "skipped": True, "reason": "project content already backed up", "content_id": content_id}
    age = _seconds_since(str(project.get("last_backup_at") or ""))
    min_interval = max(0, int(config.project_auto_backup_min_interval_seconds or 0))
    if age is not None and age < min_interval:
        return {
            "success": False,
            "deferred": True,
            "reason": "project auto backup throttled",
            "retry_after_seconds": int(min_interval - age),
            "content_id": content_id,
        }
    return None


def _record_success(root: Path, item: dict[str, Any], result: dict[str, Any]) -> None:
    state = _read_state()
    projects = state.setdefault("projects", {})
    previous = projects.get(_project_key(root), {})
    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    manifest = package.get("manifest") if isinstance(package.get("manifest"), dict) else {}
    full_backup_id = package.get("backup_id") if manifest.get("backup_kind") == "full" else previous.get("last_full_backup_id")
    projects[_project_key(root)] = {
        "root": str(root),
        "last_backup_at": utc_now(),
        "last_content_id": str(item.get("content_id") or ""),
        "last_reason": item.get("reason"),
        "last_backup_id": package.get("backup_id"),
        "last_full_backup_id": full_backup_id,
        "last_result": {
            "success": result.get("success"),
            "archive": package.get("archive"),
            "upload": result.get("upload"),
        },
    }
    _write_state(state)


def _mark_attempt(path: Path, item: dict[str, Any], result: dict[str, Any]) -> None:
    item["attempts"] = int(item.get("attempts") or 0) + 1
    item["last_attempt_at"] = utc_now()
    item["last_error"] = result.get("error") or result.get("reason") or "auto backup failed"
    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    if package.get("archive") and package.get("manifest"):
        item["package"] = {"archive": package.get("archive"), "manifest": package.get("manifest")}
    write_json(path, item)


def _upload_existing_package(config: AppConfig, item: dict[str, Any]) -> dict[str, Any] | None:
    package = item.get("package") if isinstance(item.get("package"), dict) else None
    if not package:
        return None
    archive = package.get("archive")
    manifest = package.get("manifest")
    if not archive or not isinstance(manifest, dict) or not Path(str(archive)).exists():
        return None
    uploaded = upload_project_backup(config, archive, manifest)
    return {"success": bool(uploaded.get("success")), "package": package, "upload": uploaded, "error": uploaded.get("error")}


def run_project_auto_backup_item(config: AppConfig, item: dict[str, Any]) -> dict[str, Any]:
    root, is_repo = _auto_project_root(item.get("cwd"))
    if not root:
        return {"success": True, "skipped": True, "reason": "project directory no longer exists", "item": item}
    item["is_repo"] = is_repo
    skip = _should_skip(root, item, config)
    if skip:
        return skip
    existing = _upload_existing_package(config, item)
    if existing is not None:
        return existing
    mode = str(item.get("mode") or _mode_for_reason(str(item.get("reason") or "")))
    if not is_repo:
        mode = "full"
    project = _read_state().get("projects", {}).get(_project_key(root), {})
    if is_repo and not project.get("last_full_backup_id"):
        mode = "full"
    result = backup_project_to_server_for_auto(root, config, mode=mode, reason=str(item.get("reason") or "auto"), commit_ref=item.get("commit"))
    return result


def backup_project_to_server_for_auto(
    cwd: str | Path | None,
    config: AppConfig,
    *,
    mode: str = "worktree",
    reason: str = "auto",
    commit_ref: str | None = None,
) -> dict[str, Any]:
    package = create_project_backup_package(cwd, config, mode=mode, trigger_reason=reason, commit_ref=commit_ref)
    uploaded = upload_project_backup(config, package["archive"], package["manifest"])
    result = {"success": bool(uploaded.get("success")), "package": package, "upload": uploaded}
    if not result["success"]:
        result["error"] = uploaded.get("error") or "project auto backup upload failed"
    return result


def process_project_auto_backup_queue(config: AppConfig, *, limit: int = 5) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    remaining = 0
    for path, item in _queue_items():
        if len(processed) >= limit:
            remaining += 1
            continue
        result = run_project_auto_backup_item(config, item)
        root, _ = _auto_project_root(item.get("cwd"))
        if result.get("success"):
            if root and not result.get("skipped"):
                _record_success(root, item, result)
            try:
                path.unlink()
            except OSError:
                pass
        elif result.get("deferred"):
            item["last_deferred_at"] = utc_now()
            item["last_deferred_reason"] = result.get("reason")
            write_json(path, item)
        else:
            _mark_attempt(path, item, result)
        processed.append({"queue_file": str(path), "item": item, "result": result})
    remaining += max(0, len(_queue_items()) - 0)
    return {"success": True, "processed": processed, "processed_count": len(processed), "remaining_count": len(_queue_items())}


def _quote_sh(path: str | Path) -> str:
    return "'" + str(path).replace("\\", "/").replace("'", "'\"'\"'") + "'"


def _strip_managed_block(text: str) -> str:
    start = text.find(HOOK_BEGIN)
    end = text.find(HOOK_END)
    if start >= 0 and end >= start:
        end += len(HOOK_END)
        while end < len(text) and text[end] in "\r\n":
            end += 1
        return (text[:start].rstrip() + "\n" + text[end:].lstrip()).strip() + "\n"
    return text


def _hook_script() -> str:
    runner = Path(__file__).resolve().parent / "hook_runner.py"
    lines = [
        HOOK_BEGIN,
        'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
        f"{_quote_sh(pythonw_executable())} {_quote_sh(runner)} project-auto-backup --reason git-post-commit --cwd \"$REPO_ROOT\" --process >/dev/null 2>&1 &",
        HOOK_END,
    ]
    return "\n".join(lines)


def _hook_path(cwd: str | Path | None) -> Path:
    root = git_root(cwd)
    if not root:
        raise RuntimeError("Project Git hook can only be installed inside a Git repository.")
    return root / ".git" / "hooks" / "post-commit"


def install_project_git_hook(cwd: str | Path | None, config: AppConfig | None = None) -> dict[str, Any]:
    path = _hook_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    body = _strip_managed_block(existing)
    if not body.strip():
        body = "#!/bin/sh\n"
    elif not body.startswith("#!"):
        body = "#!/bin/sh\n" + body
    body = body.rstrip() + "\n\n" + _hook_script() + "\n"
    path.write_text(body, encoding="utf-8")
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass
    return {"success": True, "installed": True, "path": str(path), "repo": str(git_root(cwd) or path.parent.parent.parent)}


def uninstall_project_git_hook(cwd: str | Path | None) -> dict[str, Any]:
    path = _hook_path(cwd)
    if not path.exists():
        return {"success": True, "installed": False, "path": str(path)}
    body = _strip_managed_block(path.read_text(encoding="utf-8", errors="replace"))
    if body.strip() in ("", "#!/bin/sh"):
        try:
            path.unlink()
        except OSError:
            path.write_text("", encoding="utf-8")
    else:
        path.write_text(body, encoding="utf-8")
    return {"success": True, "installed": False, "path": str(path)}


def project_auto_backup_status(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    root, is_repo = _auto_project_root(cwd)
    if not root:
        return {
            "success": True,
            "is_repo": False,
            "auto_supported": False,
            "enabled_for_git_commit": False,
            "queue_count": len(_queue_items()),
            "codex_stop_enabled": bool(config.project_auto_backup_on_codex_stop),
            "min_interval_seconds": config.project_auto_backup_min_interval_seconds,
        }
    if not is_repo:
        state = _read_state().get("projects", {}).get(_project_key(root), {})
        return {
            "success": True,
            "is_repo": False,
            "auto_supported": True,
            "root": str(root),
            "enabled_for_git_commit": False,
            "queue_count": len(_queue_items()),
            "codex_stop_enabled": bool(config.project_auto_backup_on_codex_stop),
            "min_interval_seconds": config.project_auto_backup_min_interval_seconds,
            "last": state,
        }
    hook = root / ".git" / "hooks" / "post-commit"
    text = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else ""
    state = _read_state().get("projects", {}).get(_project_key(root), {})
    return {
        "success": True,
        "is_repo": True,
        "auto_supported": True,
        "root": str(root),
        "hook_path": str(hook),
        "enabled_for_git_commit": HOOK_BEGIN in text and HOOK_END in text,
        "queue_count": len(_queue_items()),
        "codex_stop_enabled": bool(config.project_auto_backup_on_codex_stop),
        "min_interval_seconds": config.project_auto_backup_min_interval_seconds,
        "last": state,
    }


def spawn_project_auto_backup(cwd: str | Path | None, *, reason: str = "manual") -> dict[str, Any]:
    runner = Path(__file__).resolve().parent / "hook_runner.py"
    args = [
        pythonw_executable(),
        str(runner),
        "project-auto-backup",
        "--reason",
        reason,
        "--cwd",
        str(cwd or Path.cwd()),
        "--process",
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(cwd or Path.cwd()))
    except Exception as exc:
        return {"success": False, "error": str(exc), "args": args}
    return {"success": True, "spawned": True, "cwd": str(cwd or Path.cwd()), "reason": reason}
