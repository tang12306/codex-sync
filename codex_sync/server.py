from __future__ import annotations

import hashlib
import json
import difflib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .collector import build_snapshot
from .paths import app_dir, ensure_app_dirs
from .redact import redact_text
from .util import safe_filename, utc_now, write_json


DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
EXPECTED_SERVER_API_VERSION = 4
REQUIRED_SERVER_FEATURES = {
    "snapshots",
    "device_state",
    "devices",
    "changes",
    "full_backups",
    "project_backups",
    "retention",
    "wsl_full_backups",
}


def outbox_dir() -> Path:
    ensure_app_dirs()
    return app_dir() / "outbox"


def outbox_count() -> int:
    return len(list(outbox_dir().glob("*.json")))


def enqueue(payload: dict[str, Any]) -> Path:
    name = f"{utc_now().replace(':', '').replace('+', 'Z')}-{safe_filename(payload.get('id', 'payload'))}.json"
    path = outbox_dir() / name
    write_json(path, payload)
    return path


def _endpoint(config: AppConfig) -> str:
    return config.server_url.rstrip("/") + "/api/snapshots"


def snapshot_state_path() -> Path:
    ensure_app_dirs()
    return app_dir() / "snapshot-state.json"


def _read_snapshot_state() -> dict[str, Any]:
    path = snapshot_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_snapshot_state(state: dict[str, Any]) -> None:
    write_json(snapshot_state_path(), state)


def snapshot_signature(payload: dict[str, Any]) -> str:
    stable = dict(payload)
    stable.pop("id", None)
    stable.pop("created_at", None)
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def post_payload(config: AppConfig, payload: dict[str, Any], timeout: int = 15, queue_on_failure: bool = True) -> dict[str, Any]:
    if not config.server_url:
        result: dict[str, Any] = {"sent": False, "queued": False, "reason": "server_url is empty"}
        if queue_on_failure:
            path = enqueue(payload)
            result.update({"queued": True, "path": str(path)})
        return result

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(_endpoint(config), data=body, headers=headers, method="POST")
    try:
        with DIRECT_OPENER.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return {"sent": True, "status": response.status, "response": text[:1000]}
    except (urllib.error.URLError, TimeoutError) as exc:
        result = {"sent": False, "queued": False, "reason": str(exc)}
        if queue_on_failure:
            path = enqueue(payload)
            result.update({"queued": True, "path": str(path)})
        return result


def sync_once(config: AppConfig, cwd: str | None = None, skip_unchanged: bool = False) -> dict[str, Any]:
    payload = build_snapshot(config, cwd=cwd)
    signature = snapshot_signature(payload)
    if skip_unchanged:
        state = _read_snapshot_state()
        if state.get("last_snapshot_signature") == signature:
            return {
                "snapshot_id": payload["id"],
                "sent": False,
                "queued": False,
                "skipped": True,
                "reason": "snapshot unchanged",
                "snapshot_signature": signature,
            }
    result = post_payload(config, payload)
    if result.get("sent") or result.get("queued"):
        state = _read_snapshot_state()
        state.update(
            {
                "last_snapshot_signature": signature,
                "last_snapshot_id": payload["id"],
                "last_snapshot_at": payload["created_at"],
            }
        )
        _write_snapshot_state(state)
    return {"snapshot_id": payload["id"], "snapshot_signature": signature, **result}


def flush_outbox(config: AppConfig) -> dict[str, Any]:
    if not config.server_url:
        return {"sent": 0, "remaining": outbox_count(), "error": "server_url is empty"}
    sent = 0
    errors: list[str] = []
    for path in sorted(outbox_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = post_payload(config, payload, queue_on_failure=False)
            if result.get("sent"):
                path.unlink()
                sent += 1
            else:
                errors.append(str(result.get("reason")))
                break
        except Exception as exc:  # noqa: BLE001 - queue repair path
            errors.append(f"{path.name}: {exc}")
            break
    return {"sent": sent, "remaining": outbox_count(), "errors": errors}


def get_from_server(config: AppConfig, path: str, timeout: int = 15) -> Any:
    if not config.server_url:
        raise ValueError("server_url is empty")
    url = config.server_url.rstrip("/") + path
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with DIRECT_OPENER.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text)


def post_json_to_server(config: AppConfig, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    if not config.server_url:
        raise ValueError("server_url is empty")
    url = config.server_url.rstrip("/") + path
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with DIRECT_OPENER.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text)


def list_remote_snapshots(config: AppConfig) -> dict[str, Any]:
    try:
        return get_from_server(config, "/api/snapshots")
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_server_retention(config: AppConfig) -> dict[str, Any]:
    try:
        return get_from_server(config, "/api/retention")
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_server_version(config: AppConfig) -> dict[str, Any]:
    try:
        return get_from_server(config, "/api/version")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "success": False,
                "needs_update": True,
                "error": "Remote sync server does not expose /api/version. Update the server deployment.",
                "status": exc.code,
            }
        return {"success": False, "error": str(exc), "status": exc.code}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def check_server_compatibility(config: AppConfig) -> dict[str, Any]:
    version = get_server_version(config)
    expected = {
        "api_version": EXPECTED_SERVER_API_VERSION,
        "required_features": sorted(REQUIRED_SERVER_FEATURES),
    }
    if not version.get("success"):
        return {
            "success": False,
            "compatible": False,
            "needs_update": bool(version.get("needs_update")),
            "expected": expected,
            "server": version,
            "error": version.get("error") or "Failed to check server version.",
        }
    try:
        api_version = int(version.get("api_version") or 0)
    except (TypeError, ValueError):
        api_version = 0
    features = version.get("features") if isinstance(version.get("features"), dict) else {}
    missing = sorted(feature for feature in REQUIRED_SERVER_FEATURES if not features.get(feature))
    too_old = api_version < EXPECTED_SERVER_API_VERSION
    compatible = not too_old and not missing
    return {
        "success": True,
        "compatible": compatible,
        "needs_update": not compatible,
        "expected": expected,
        "server": version,
        "missing_features": missing,
        "server_api_version": api_version,
        "message": "Server is compatible." if compatible else "Remote sync server should be updated.",
    }


def prune_server_retention(config: AppConfig, dry_run: bool = False) -> dict[str, Any]:
    try:
        return post_json_to_server(config, "/api/retention/prune", {"dry_run": dry_run}, timeout=120)
    except Exception as exc:
        return {"success": False, "error": str(exc), "dry_run": dry_run}


def get_remote_snapshot_detail(config: AppConfig, snapshot_id: str) -> dict[str, Any]:
    snapshot, error = _download_restore_snapshot(config, snapshot_id)
    if error:
        return error
    assert snapshot is not None
    return {"success": True, "snapshot": snapshot}


def generate_remote_resume_context(config: AppConfig, snapshot_id: str) -> dict[str, Any]:
    snapshot, error = _download_restore_snapshot(config, snapshot_id)
    if error:
        return error
    assert snapshot is not None

    resume_dir = app_dir() / "resume"
    resume_dir.mkdir(parents=True, exist_ok=True)
    path = resume_dir / f"remote-{safe_filename(snapshot_id)}.md"
    git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
    codex = snapshot.get("codex", {}) if isinstance(snapshot.get("codex"), dict) else {}
    config_files = codex.get("config_files", {}) if isinstance(codex.get("config_files"), dict) else {}
    recent_events = snapshot.get("recent_events", []) if isinstance(snapshot.get("recent_events"), list) else []

    lines = [
        "# Codex Remote Resume Context",
        "",
        f"- Snapshot ID: {snapshot.get('id', snapshot_id)}",
        f"- Device: {snapshot.get('device_id', '-')}",
        f"- Created at: {snapshot.get('created_at', '-')}",
        f"- Current directory: {snapshot.get('cwd', '-')}",
        "",
        "## Git State",
    ]
    if git.get("is_repo"):
        lines.extend(
            [
                f"- Repo: {git.get('root', '-')}",
                f"- Branch: {git.get('branch', '-')}",
                f"- Commit: {git.get('commit', '-')}",
                f"- Dirty: {git.get('dirty', '-')}",
                "",
                "```text",
                git.get("status", "") or "clean",
                "```",
            ]
        )
    else:
        lines.append("- Not inside a Git repository or git state unavailable.")

    lines.extend(["", "## Codex Config Files"])
    if config_files:
        for name, meta in config_files.items():
            if isinstance(meta, dict):
                lines.append(f"- {name}: {meta.get('bytes', '?')} bytes, sha256={meta.get('sha256', '-')}")
    else:
        lines.append("- No config file metadata in snapshot.")

    lines.extend(["", "## Recent Codex Events"])
    if recent_events:
        for event in recent_events[-12:]:
            if isinstance(event, dict):
                lines.append(f"- {event.get('created_at', '-')} {event.get('event', event.get('type', '-'))} cwd={event.get('cwd', '-')}")
    else:
        lines.append("- No recent events in snapshot.")

    lines.extend(
        [
            "",
            "## Suggested Prompt",
            "",
            "Continue from this remote snapshot. First inspect the current repo and Git status on this machine, then reconcile it with the snapshot's cwd, branch, commit, and recent Codex events. Ask for missing context only if the task cannot be recovered from the snapshot.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"success": True, "snapshot_id": snapshot_id, "path": str(path)}


RESTORE_ALLOWED_FILES = ("config.toml", "AGENTS.md", "AGENTS.override.md")


def _download_restore_snapshot(config: AppConfig, snapshot_id: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if not snapshot_id:
        return None, {"success": False, "error": "snapshot_id is required"}
    try:
        snapshot = get_from_server(config, f"/api/snapshots/{quote(snapshot_id, safe='')}")
    except Exception as exc:
        return None, {"success": False, "error": f"Failed to download snapshot from server: {exc}"}
    if not isinstance(snapshot, dict) or snapshot.get("id") != snapshot_id:
        return None, {"success": False, "error": "Downloaded snapshot id does not match requested snapshot_id"}
    return snapshot, None


def _extract_restore_configs(snapshot: dict[str, Any]) -> dict[str, str]:
    codex_state = snapshot.get("codex", {})
    configs = codex_state.get("configs", {})
    if not isinstance(configs, dict):
        return {}
    return {name: content for name, content in configs.items() if isinstance(name, str) and isinstance(content, str)}


def preview_restore_snapshot(config: AppConfig, snapshot_id: str, restore_hooks: bool = False) -> dict[str, Any]:
    from .paths import codex_home

    snapshot, error = _download_restore_snapshot(config, snapshot_id)
    if error:
        return error
    assert snapshot is not None

    configs = _extract_restore_configs(snapshot)
    if not configs:
        return {"success": False, "error": "This snapshot does not contain raw configuration files inside 'codex.configs'"}

    home = codex_home()
    allowed = set(RESTORE_ALLOWED_FILES)
    if restore_hooks:
        allowed.add("hooks.json")

    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name, remote_content in configs.items():
        if name not in allowed:
            skipped.append(name)
            continue
        local_path = home / name
        local_content = local_path.read_text(encoding="utf-8", errors="replace") if local_path.exists() else ""
        diff = "\n".join(
            difflib.unified_diff(
                local_content.splitlines(),
                remote_content.splitlines(),
                fromfile=f"local/{name}",
                tofile=f"remote/{name}",
                lineterm="",
            )
        )
        files.append(
            {
                "name": name,
                "local_exists": local_path.exists(),
                "remote_bytes": len(remote_content.encode("utf-8")),
                "changed": local_content != remote_content,
                "diff": redact_text(diff),
            }
        )

    return {"success": True, "snapshot_id": snapshot_id, "files": files, "skipped": skipped}


def restore_snapshot_locally(
    config: AppConfig,
    snapshot_id: str,
    confirm_snapshot_id: str | None = None,
    restore_hooks: bool = False,
) -> dict[str, Any]:
    from .disaster_backup import create_disaster_backup
    from .paths import codex_home

    if not snapshot_id:
        return {"success": False, "error": "snapshot_id is required"}
    if confirm_snapshot_id != snapshot_id:
        return {
            "success": False,
            "error": "Refusing restore without exact confirm_snapshot_id match",
            "snapshot_id": snapshot_id,
        }

    snapshot, error = _download_restore_snapshot(config, snapshot_id)
    if error:
        return error
    assert snapshot is not None

    configs = _extract_restore_configs(snapshot)
    if not configs:
        return {"success": False, "error": "This snapshot does not contain any configuration files inside 'codex.configs'"}

    try:
        protection = create_disaster_backup(config, reason=f"before_restore_{snapshot_id[:8]}", force=True)
    except Exception as exc:
        return {"success": False, "error": f"Failed to create preflight disaster backup: {exc}"}

    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    skipped: list[str] = []
    allowed = set(RESTORE_ALLOWED_FILES)
    if restore_hooks:
        allowed.add("hooks.json")

    for name, content in configs.items():
        if name not in allowed:
            skipped.append(name)
            continue
        if not isinstance(content, str):
            skipped.append(name)
            continue
        try:
            filepath = home / name
            if filepath.parent.resolve() != home.resolve():
                skipped.append(name)
                continue
            filepath.write_text(content, encoding="utf-8")
            restored.append(name)
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to write configuration file '{name}' to local: {exc}",
                "restored": restored,
                "preflight_backup": protection
            }

    return {
        "success": True,
        "snapshot_id": snapshot_id,
        "restored": restored,
        "skipped": skipped,
        "preflight_backup": protection
    }
