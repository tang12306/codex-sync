from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .disaster_backup import create_disaster_backup
from .paths import app_dir, codex_home, ensure_app_dirs
from .server import DIRECT_OPENER
from .util import safe_filename, utc_now, write_json


EXACT_FILES = {
    ".codex-global-state.json",
    "history.jsonl",
    "session_index.jsonl",
}

FILE_PREFIXES = (
    "goals_",
    "logs_",
    "state_",
)

FILE_SUFFIXES = (
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
)

DIRECTORIES = {
    "sessions",
    "threads",
}

OPTIONAL_CONFIG_FILES = {
    "AGENTS.md",
    "AGENTS.override.md",
    "config.toml",
    "hooks.json",
}

OPTIONAL_DIRECTORIES = {
    "memories",
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


def full_backup_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "full-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    path = full_backup_dir() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return full_backup_dir() / "state.json"


def _read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write_state(data: dict[str, Any]) -> None:
    write_json(state_path(), data)


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_seconds(since: Any) -> int:
    parsed = _parse_utc(since)
    if parsed is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _branch_id(config: AppConfig, state: dict[str, Any] | None = None) -> str:
    state = state or _read_state()
    value = str(state.get("branch_id") or "").strip()
    if value:
        return value
    return f"{config.device_id}:main"


def _remember_branch(config: AppConfig, state: dict[str, Any]) -> dict[str, Any]:
    if not state.get("branch_id"):
        state["branch_id"] = _branch_id(config, state)
    return state


def _manifest_path_for_archive(archive: Path) -> Path:
    if archive.name.endswith(".zip.enc"):
        return archive.with_name(archive.name[:-len(".zip.enc")] + ".manifest.json")
    return archive.with_suffix(".manifest.json")


def prune_local_full_backups(config: AppConfig) -> dict[str, Any]:
    root = full_backup_dir()
    archives = sorted(
        [path for path in root.iterdir() if path.is_file() and (path.name.endswith(".zip") or path.name.endswith(".zip.enc"))],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    max_count = max(0, int(config.full_backup_retention_count or 0))
    max_bytes = max(0, int(config.full_backup_retention_max_bytes or 0))
    kept = 0
    kept_bytes = 0
    removed: list[dict[str, Any]] = []

    for archive in archives:
        try:
            size = archive.stat().st_size
        except OSError:
            continue
        over_count = bool(max_count and kept >= max_count)
        over_bytes = bool(max_bytes and kept_bytes + size > max_bytes)
        if over_count or over_bytes:
            entry = {"archive": str(archive), "bytes": size, "reason": "retention limit"}
            try:
                archive.unlink()
                manifest = _manifest_path_for_archive(archive)
                manifest.unlink(missing_ok=True)
                entry["removed"] = True
            except OSError as exc:
                entry["removed"] = False
                entry["error"] = str(exc)
            removed.append(entry)
            continue
        kept += 1
        kept_bytes += size

    return {"kept": kept, "kept_bytes": kept_bytes, "removed": removed}


def _safe_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(part in DENY_PARTS for part in rel.parts):
        return False
    if path.name in DENY_NAMES:
        return False
    if path.suffix.lower() in DENY_SUFFIXES:
        return False
    return True


def _top_level_file_allowed(path: Path, config: AppConfig) -> bool:
    name = path.name
    if name in EXACT_FILES:
        return True
    if config.full_backup_include_config and name in OPTIONAL_CONFIG_FILES:
        return True
    return any(name.startswith(prefix) for prefix in FILE_PREFIXES) and any(name.endswith(suffix) for suffix in FILE_SUFFIXES)


def _iter_candidate_files(root: Path, config: AppConfig) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    allowed_dirs = set(DIRECTORIES)
    if config.full_backup_include_memories:
        allowed_dirs.update(OPTIONAL_DIRECTORIES)
    for child in root.iterdir():
        if child.is_file() and _top_level_file_allowed(child, config) and _safe_file(child, root):
            files.append(child)
        elif child.is_dir() and child.name in allowed_dirs:
            for file in child.rglob("*"):
                if file.is_file() and _safe_file(file, root):
                    files.append(file)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_path(path: Path) -> str:
    return _hash_file(path)


def _manifest_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_full_backup_manifest(config: AppConfig) -> dict[str, Any]:
    root = codex_home()
    state = _remember_branch(config, _read_state())
    included: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = 0

    for file in _iter_candidate_files(root, config):
        rel = file.relative_to(root).as_posix()
        try:
            stat = file.stat()
        except OSError as exc:
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        size = stat.st_size
        if size > config.full_backup_max_file_bytes:
            skipped.append({"path": rel, "reason": "file too large", "bytes": size})
            continue
        if total + size > config.full_backup_max_total_bytes:
            skipped.append({"path": rel, "reason": "backup size limit", "bytes": size})
            continue
        try:
            sha256 = _hash_file(file)
        except OSError as exc:
            skipped.append({"path": rel, "reason": str(exc), "bytes": size})
            continue
        included.append(
            {
                "path": rel,
                "bytes": size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256,
            }
        )
        total += size

    digest = _manifest_digest(included)
    backup_id = str(uuid.uuid4())
    return {
        "id": backup_id,
        "type": "codex_full_backup",
        "format_version": 1,
        "created_at": utc_now(),
        "device_id": config.device_id,
        "branch_id": _branch_id(config, state),
        "parent_backup_id": str(state.get("last_uploaded_backup_id") or ""),
        "codex_home": str(root),
        "encrypted": False,
        "encryption": "none",
        "content_digest": digest,
        "included_count": len(included),
        "included_bytes": total,
        "files": included,
        "skipped": skipped,
        "excluded_by_policy": sorted(DENY_NAMES),
        "include_config": config.full_backup_include_config,
        "include_memories": config.full_backup_include_memories,
    }


def create_full_backup_package(config: AppConfig, force: bool = False, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or build_full_backup_manifest(config)
    prior = _read_state()
    if not force and prior.get("last_content_digest") == manifest["content_digest"]:
        return {
            "created": False,
            "reason": "no changes",
            "content_digest": manifest["content_digest"],
            "last_backup_id": prior.get("last_backup_id"),
            "last_archive": prior.get("last_archive"),
            "manifest": manifest,
        }

    archive_name = f"{manifest['created_at'].replace(':', '').replace('+', 'Z')}-{safe_filename(manifest['device_id'])}-{manifest['id']}.zip"
    archive = full_backup_dir() / archive_name
    root = codex_home()
    manifest["archive_name"] = archive.name

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for item in manifest["files"]:
            rel = str(item["path"])
            source = root / rel
            if not source.exists() or not _safe_file(source, root):
                continue
            zf.write(source, arcname=f"codex/{rel}")

    archive_sha256 = _hash_path(archive)
    archive_bytes = archive.stat().st_size
    manifest_path = _manifest_path_for_archive(archive)
    manifest["archive"] = str(archive)
    manifest["archive_sha256"] = archive_sha256
    manifest["archive_bytes"] = archive_bytes
    write_json(manifest_path, manifest)
    new_state = {
        **_remember_branch(config, prior),
        "branch_id": manifest["branch_id"],
        "last_backup_id": manifest["id"],
        "last_content_digest": manifest["content_digest"],
        "last_archive": str(archive),
        "last_created_at": manifest["created_at"],
    }
    for key in ("pending_content_digest", "pending_since"):
        new_state.pop(key, None)
    _write_state(new_state)
    retention = prune_local_full_backups(config)
    return {
        "created": True,
        "backup_id": manifest["id"],
        "archive": str(archive),
        "manifest": str(manifest_path),
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "included_count": manifest["included_count"],
        "included_bytes": manifest["included_bytes"],
        "skipped": manifest["skipped"],
        "encrypted": False,
        "retention": retention,
    }


def _server_url(config: AppConfig, path: str) -> str:
    if not config.server_url:
        raise ValueError("server_url is empty")
    return config.server_url.rstrip("/") + path


def _headers(config: AppConfig) -> dict[str, str]:
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    return headers


def _metadata_header(metadata: dict[str, Any]) -> str:
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def notify_codex_changed(
    config: AppConfig,
    event_id: str | None = None,
    event: str | None = None,
    reason: str = "codex_hook",
    cwd: str | None = None,
    include_digest: bool = False,
    content_digest: str | None = None,
    changed_at: str | None = None,
) -> dict[str, Any]:
    state = _remember_branch(config, _read_state())
    payload: dict[str, Any] = {
        "device_id": config.device_id,
        "event_id": event_id or str(uuid.uuid4()),
        "event": event,
        "reason": reason,
        "changed_at": changed_at or utc_now(),
        "branch_id": _branch_id(config, state),
        "parent_backup_id": state.get("last_uploaded_backup_id") or "",
        "cwd": cwd or os.getcwd(),
    }
    if include_digest:
        payload["content_digest"] = content_digest or build_full_backup_manifest(config)["content_digest"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _headers(config)
    headers["Content-Type"] = "application/json"
    try:
        request = urllib.request.Request(_server_url(config, "/api/changes"), data=body, headers=headers, method="POST")
        with DIRECT_OPENER.open(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - hook path must not fail hard
        result = {"success": False, "error": str(exc), "event_id": payload["event_id"]}
    state.update({"last_dirty_event_id": payload["event_id"], "last_dirty_at": payload["changed_at"]})
    if result.get("device_state"):
        state["last_server_device_state"] = result["device_state"]
    if result.get("success") and payload.get("content_digest"):
        state["last_notified_content_digest"] = payload["content_digest"]
    _write_state(state)
    return result


def get_remote_device_state(config: AppConfig, device_id: str | None = None) -> dict[str, Any]:
    target = quote(device_id or config.device_id, safe="")
    try:
        request = urllib.request.Request(_server_url(config, f"/api/device-state?device_id={target}"), headers=_headers(config), method="GET")
        with DIRECT_OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"success": False, "error": str(exc), "device_id": device_id or config.device_id}


def list_remote_devices(config: AppConfig) -> dict[str, Any]:
    """拉取服务器上所有设备的同步状态（GET /api/devices）。"""
    try:
        request = urllib.request.Request(_server_url(config, "/api/devices"), headers=_headers(config), method="GET")
        with DIRECT_OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def summarize_sync_health(config: AppConfig) -> dict[str, Any]:
    """聚合本机未上传状态 + 远端各设备同步状态，供「概览」展示与提示。

    本机 needs_upload 用已存的 state.json 比对（不重算 manifest，避免重负载）；
    远端经 /api/devices 拉所有设备，挑出 dirty/diverged 的作为「未完成同步」。
    """
    state = _read_state()
    last_digest = str(state.get("last_content_digest") or "")
    uploaded_digest = str(state.get("last_uploaded_content_digest") or "")
    local = {
        "device_id": config.device_id,
        "needs_upload": bool(last_digest and last_digest != uploaded_digest),
        "last_uploaded_at": state.get("last_uploaded_at"),
        "last_dirty_at": state.get("last_dirty_at"),
        "diverged": bool(state.get("diverged_from_remote_head")),
        "branch_id": _branch_id(config, state),
    }
    remote = list_remote_devices(config)
    if not remote.get("success"):
        return {"success": False, "error": remote.get("error", "无法获取远端设备列表"), "local": local}
    devices = remote.get("devices", []) or []
    this_id = config.device_id
    pending: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict) or device.get("device_id") == this_id:
            continue
        if device.get("dirty") or device.get("sync_state") in ("dirty", "diverged"):
            pending.append(
                {
                    "device_id": device.get("device_id"),
                    "sync_state": device.get("sync_state"),
                    "dirty": bool(device.get("dirty")),
                    "last_change_at": device.get("last_change_at"),
                    "last_backup_at": device.get("last_backup_at"),
                }
            )
    return {
        "success": True,
        "this_device_id": this_id,
        "local": local,
        "devices": devices,
        "pending_devices": pending,
    }


def _new_branch_id(config: AppConfig, remote_head: str) -> str:
    stamp = utc_now().replace(":", "").replace("+00:00", "Z")
    suffix = safe_filename((remote_head or uuid.uuid4().hex)[:12])
    return f"{config.device_id}:branch-{stamp}-{suffix}"


def reconcile_remote_head(
    config: AppConfig,
    manifest: dict[str, Any],
    remote_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = _remember_branch(config, _read_state())
    remote_response = remote_response if remote_response is not None else get_remote_device_state(config)
    remote_state = remote_response.get("device_state") if isinstance(remote_response, dict) else None
    result: dict[str, Any] = {"checked": True, "remote": remote_response, "action": "none"}
    if not isinstance(remote_state, dict):
        return result

    remote_head = str(remote_state.get("head_backup_id") or "")
    remote_digest = str(remote_state.get("head_content_digest") or "")
    remote_branch = str(remote_state.get("branch_id") or "")
    local_parent = str(state.get("last_uploaded_backup_id") or "")
    content_digest = str(manifest.get("content_digest") or "")
    if not remote_head:
        return result

    if content_digest and remote_digest == content_digest:
        state.update(
            {
                "branch_id": remote_branch or _branch_id(config, state),
                "last_uploaded_backup_id": remote_head,
                "last_uploaded_content_digest": content_digest,
                "last_uploaded_at": remote_state.get("last_backup_at") or utc_now(),
                "last_server_device_state": remote_state,
            }
        )
        for key in ("diverged_from_remote_head", "diverged_at", "diverged_previous_branch_id"):
            state.pop(key, None)
        _write_state(state)
        manifest["branch_id"] = _branch_id(config, state)
        manifest["parent_backup_id"] = str(state.get("last_uploaded_backup_id") or "")
        result.update({"action": "adopted_remote_head", "head_backup_id": remote_head, "branch_id": manifest["branch_id"]})
        return result

    if remote_head != local_parent:
        old_branch = _branch_id(config, state)
        if state.get("diverged_from_remote_head") != remote_head:
            state["branch_id"] = _new_branch_id(config, remote_head)
            state["diverged_at"] = utc_now()
            state["diverged_previous_branch_id"] = old_branch
        state["diverged_from_remote_head"] = remote_head
        state["last_server_device_state"] = remote_state
        _write_state(state)
        manifest["branch_id"] = _branch_id(config, state)
        manifest["parent_backup_id"] = local_parent
        result.update(
            {
                "action": "created_branch",
                "branch_id": manifest["branch_id"],
                "previous_branch_id": old_branch,
                "remote_head_backup_id": remote_head,
                "local_parent_backup_id": local_parent,
            }
        )
    return result


def _update_pending_state(config: AppConfig, state: dict[str, Any], content_digest: str) -> dict[str, Any]:
    quiet_seconds = max(0, int(config.full_backup_quiet_seconds or 0))
    if state.get("pending_content_digest") != content_digest:
        state["pending_content_digest"] = content_digest
        state["pending_since"] = utc_now()
        _write_state(state)
    elapsed = _elapsed_seconds(state.get("pending_since"))
    return {
        "quiet_seconds": quiet_seconds,
        "pending_since": state.get("pending_since"),
        "quiet_elapsed_seconds": elapsed,
        "ready": elapsed >= quiet_seconds,
    }


def scan_full_backup_changes(
    config: AppConfig,
    create_package: bool = True,
    force: bool = False,
    notify_dirty: bool = False,
    check_remote: bool = False,
) -> dict[str, Any]:
    manifest = build_full_backup_manifest(config)
    state = _remember_branch(config, _read_state())
    remote_reconcile: dict[str, Any] | None = None
    if check_remote:
        remote_reconcile = reconcile_remote_head(config, manifest)
        state = _remember_branch(config, _read_state())
    last_digest = state.get("last_content_digest")
    uploaded_digest = state.get("last_uploaded_content_digest")
    changed = force or manifest["content_digest"] != last_digest
    unsafely_unuploaded = manifest["content_digest"] != uploaded_digest
    result: dict[str, Any] = {
        "success": True,
        "changed": changed,
        "needs_upload": unsafely_unuploaded,
        "content_digest": manifest["content_digest"],
        "last_content_digest": last_digest,
        "last_uploaded_content_digest": uploaded_digest,
        "branch_id": manifest["branch_id"],
        "parent_backup_id": manifest["parent_backup_id"],
        "included_count": manifest["included_count"],
        "included_bytes": manifest["included_bytes"],
    }
    if remote_reconcile is not None:
        result["remote_reconcile"] = remote_reconcile
    if notify_dirty and unsafely_unuploaded:
        if state.get("last_notified_content_digest") != manifest["content_digest"]:
            dirty = notify_codex_changed(
                config,
                reason="periodic_full_backup_scan",
                include_digest=True,
                content_digest=manifest["content_digest"],
            )
            result["dirty_notification"] = dirty
            if dirty.get("success"):
                state = _remember_branch(config, _read_state())
                state["last_notified_content_digest"] = manifest["content_digest"]
                _write_state(state)
        else:
            result["dirty_notification"] = {"skipped": True, "reason": "content digest already notified"}
    if changed and create_package:
        quiet = _update_pending_state(config, state, manifest["content_digest"])
        result["quiet"] = quiet
        if not force and not quiet["ready"]:
            result["package_deferred"] = True
            result["package_deferred_reason"] = "waiting for full backup quiet period"
            return result
        package = create_full_backup_package(config, force=True)
        result["package"] = package
    return result


def upload_full_backup(
    config: AppConfig,
    archive: str | Path,
    manifest: dict[str, Any] | None = None,
    allow_plaintext_upload: bool = False,
    update_local_state: bool = True,
) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    manifest = manifest or json.loads(_manifest_path_for_archive(archive_path).read_text(encoding="utf-8"))
    if not manifest.get("encrypted") and not (allow_plaintext_upload or config.full_backup_allow_plaintext_upload):
        return {
            "success": False,
            "error": "Refusing to upload plaintext full conversation backup. Pass --allow-plaintext-upload or enable full_backup_allow_plaintext_upload only after accepting the privacy risk.",
            "archive": str(archive_path),
            "backup_id": manifest.get("id"),
        }

    archive_sha256 = _hash_path(archive_path)
    metadata = {
        "id": manifest.get("id"),
        "type": "codex_full_backup",
        "format_version": manifest.get("format_version", 1),
        "device_id": manifest.get("device_id"),
        "branch_id": manifest.get("branch_id"),
        "parent_backup_id": manifest.get("parent_backup_id"),
        "created_at": manifest.get("created_at"),
        "content_digest": manifest.get("content_digest"),
        "included_count": manifest.get("included_count"),
        "included_bytes": manifest.get("included_bytes"),
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_path.stat().st_size,
        "encrypted": bool(manifest.get("encrypted")),
        "encryption": manifest.get("encryption", "none"),
    }
    headers = _headers(config)
    headers.update(
        {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(archive_path.stat().st_size),
            "X-Codex-Backup-Metadata": _metadata_header(metadata),
        }
    )
    request = None
    with archive_path.open("rb") as fh:
        request = urllib.request.Request(
            _server_url(config, "/api/full-backups"),
            data=fh,
            headers=headers,
            method="POST",
        )
        try:
            with DIRECT_OPENER.open(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"raw": text}
                if response.status == 201 and update_local_state:
                    state = _remember_branch(config, _read_state())
                    state.update(
                        {
                            "branch_id": manifest.get("branch_id") or _branch_id(config, state),
                            "last_uploaded_backup_id": manifest.get("id"),
                            "last_uploaded_content_digest": manifest.get("content_digest"),
                            "last_uploaded_at": utc_now(),
                            "last_notified_content_digest": manifest.get("content_digest"),
                            "last_server_device_state": payload.get("device_state"),
                        }
                    )
                    device_state = payload.get("device_state") if isinstance(payload, dict) else None
                    if isinstance(device_state, dict) and device_state.get("sync_state") != "diverged":
                        for key in ("diverged_from_remote_head", "diverged_at", "diverged_previous_branch_id"):
                            state.pop(key, None)
                    _write_state(state)
                return {"success": True, "status": response.status, **payload}
        except Exception as exc:  # noqa: BLE001 - return CLI-friendly error
            return {"success": False, "error": str(exc), "backup_id": manifest.get("id"), "archive": str(archive_path)}


def full_backup_now(config: AppConfig, force: bool = False, upload: bool = False, allow_plaintext_upload: bool = False) -> dict[str, Any]:
    manifest = build_full_backup_manifest(config)
    remote_reconcile = reconcile_remote_head(config, manifest) if upload else None
    package = create_full_backup_package(config, force=force, manifest=manifest)
    if not package.get("created"):
        result = {"success": True, "uploaded": False, **package}
        if remote_reconcile is not None:
            result["remote_reconcile"] = remote_reconcile
        return result
    if not upload:
        return {"success": True, "uploaded": False, **package}
    manifest_path = Path(str(package["manifest"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    uploaded = upload_full_backup(config, str(package["archive"]), manifest=manifest, allow_plaintext_upload=allow_plaintext_upload)
    result = {"success": bool(uploaded.get("success")), "uploaded": bool(uploaded.get("success")), "package": package, "upload": uploaded}
    if remote_reconcile is not None:
        result["remote_reconcile"] = remote_reconcile
    return result


def list_full_backups(config: AppConfig) -> dict[str, Any]:
    try:
        request = urllib.request.Request(_server_url(config, "/api/full-backups"), headers=_headers(config), method="GET")
        with DIRECT_OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def download_full_backup(config: AppConfig, backup_id: str, output: str | Path | None = None) -> dict[str, Any]:
    if not backup_id:
        return {"success": False, "error": "backup_id is required"}
    output_path = Path(output) if output else downloads_dir() / f"{safe_filename(backup_id)}.zip"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        _server_url(config, f"/api/full-backups/{quote(backup_id, safe='')}/download"),
        headers=_headers(config),
        method="GET",
    )
    try:
        digest = hashlib.sha256()
        total = 0
        with DIRECT_OPENER.open(request, timeout=120) as response, output_path.open("wb") as fh:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
        return {"success": True, "backup_id": backup_id, "path": str(output_path), "bytes": total, "sha256": digest.hexdigest()}
    except Exception as exc:
        return {"success": False, "error": str(exc), "backup_id": backup_id}


def _safe_restore_target(root: Path, relative: str) -> Path | None:
    target = root / relative
    try:
        if target.parent.resolve() == root.resolve() or root.resolve() in target.parent.resolve().parents:
            return target
    except OSError:
        return None
    return None


def restore_full_backup(config: AppConfig, archive: str | Path, confirm_backup_id: str, restore_config: bool = False) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    with zipfile.ZipFile(archive_path) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        backup_id = str(manifest.get("id", ""))
        if confirm_backup_id != backup_id:
            return {"success": False, "error": "Refusing restore without exact confirm_backup_id match", "backup_id": backup_id}
        try:
            protection = create_disaster_backup(config, reason=f"before_full_restore_{backup_id[:8]}", force=True)
        except Exception as exc:
            return {"success": False, "error": f"Failed to create preflight disaster backup: {exc}", "backup_id": backup_id}
        root = codex_home()
        root.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        skipped: list[dict[str, Any]] = []
        for name in zf.namelist():
            if not name.startswith("codex/") or name.endswith("/"):
                continue
            rel = name[len("codex/"):]
            if not restore_config and Path(rel).name in OPTIONAL_CONFIG_FILES:
                skipped.append({"path": rel, "reason": "config restore disabled"})
                continue
            target = _safe_restore_target(root, rel)
            if target is None:
                skipped.append({"path": rel, "reason": "unsafe restore path"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
            restored.append(rel)
    return {
        "success": True,
        "backup_id": backup_id,
        "restored_count": len(restored),
        "restored": restored,
        "skipped": skipped,
        "preflight_backup": protection,
    }
