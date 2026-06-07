from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import __version__
from .backup_import import _ensure_local_archive, import_conversations, list_backup_conversations, list_importable_backups, read_backup_conversation
from .codex_channels import list_channels, merge_channels, merge_threads, restore_channels, restore_threads
from .collector import capture_event, create_resume_prompt
from .config import load_config, save_config
from .daemon import run_daemon
from .conversations import export_conversation, list_conversations, read_conversation
from .deploy import DeployConfig, deploy_config_path, deploy_status, install_server, load_deploy_config, save_deploy_config, update_server
from .disaster_backup import create_disaster_backup
from .git_backup import backup_project_to_server, create_patch_snapshot, git_state, list_project_backups
from .full_backup import full_backup_now, get_remote_device_state, list_full_backups, list_remote_devices, notify_codex_changed, scan_full_backup_changes, summarize_sync_health
from .hooks import hook_status, install_hooks
from .server import (
    check_server_compatibility,
    flush_outbox,
    get_server_retention,
    outbox_count,
    prune_server_retention,
    sync_once,
    list_remote_snapshots,
    get_remote_snapshot_detail,
    generate_remote_resume_context,
    preview_restore_snapshot,
    restore_snapshot_locally,
)
from .util import utc_now
from .windows_task import install_windows_task, uninstall_windows_task, windows_task_status
from .wsl import (
    export_wsl_conversation,
    import_conversations_to_wsl,
    list_codex_homes,
    list_wsl_conversations,
    list_wsl_channels,
    pull_wsl_config,
    read_wsl_conversation,
    restore_latest_wsl_full_backup,
    restore_wsl_full_backup,
    wsl_full_backup_now,
    wsl_threads_index,
    wsl_status,
)


STATIC_DIR = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ASSET_VERSION = __version__
CACHE_CONTROL = "no-store, max-age=0, must-revalidate"

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".map": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _add_no_cache_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", CACHE_CONTROL)
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")


def _append_asset_version(spec: str) -> str:
    separator = "&" if "?" in spec else "?"
    return spec if "v=" in spec else f"{spec}{separator}v={ASSET_VERSION}"


def _rewrite_js_imports(source: str) -> str:
    source = re.sub(
        r'(from\s+["\'])(\.{1,2}/[^"\']+?\.js)(["\'])',
        lambda match: f"{match.group(1)}{_append_asset_version(match.group(2))}{match.group(3)}",
        source,
    )
    return re.sub(
        r"(import\(\s*`)(\.{1,2}/[^`]*?\.js)(`\s*\))",
        lambda match: f"{match.group(1)}{_append_asset_version(match.group(2))}{match.group(3)}",
        source,
    )


def _rewrite_index_assets(html: str) -> str:
    return re.sub(
        r'((?:href|src)=["\'])(/(?:styles|js)/[^"\']+\.(?:css|js))(["\'])',
        lambda match: f"{match.group(1)}{_append_asset_version(match.group(2))}{match.group(3)}",
        html,
    )


_DEPLOY_STR_FIELDS = (
    "ssh_target",
    "remote_dir",
    "service_name",
    "python",
    "bind_host",
    "data_dir",
    "token_file",
    "nginx_server_name",
    "client_max_body_size",
)


def _deploy_from_payload(payload: dict[str, Any]) -> tuple[DeployConfig, str | None]:
    cfg = load_deploy_config()
    for key in _DEPLOY_STR_FIELDS:
        if key in payload:
            setattr(cfg, key, str(payload.get(key) or ""))
    for key in ("ssh_port", "bind_port", "max_body_bytes"):
        if key in payload and payload.get(key) not in (None, ""):
            try:
                setattr(cfg, key, int(payload.get(key)))
            except (TypeError, ValueError):
                pass
    if "nginx_enabled" in payload:
        cfg.nginx_enabled = bool(payload.get("nginx_enabled"))
    password = str(payload.get("ssh_password") or "").strip() or None
    return cfg, password


def _project_path_from_payload(payload: dict[str, Any]) -> str | None:
    raw = str(payload.get("project_path") or payload.get("cwd") or "").strip()
    return raw or None


def _project_state(project_path: str | None = None) -> dict[str, Any]:
    state = git_state(project_path)
    if not state.get("is_repo"):
        path = Path(project_path).expanduser() if project_path else Path.cwd()
        try:
            path = path.resolve()
        except OSError:
            pass
        state.setdefault("root", str(path))
        state.setdefault("exists", path.exists())
        state.setdefault("is_dir", path.is_dir())
    state["selected_path"] = str(project_path or Path.cwd())
    return state


def _choose_project_directory(initial: str | None = None) -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"success": False, "error": f"无法打开目录选择器：{exc}"}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title="选择要备份的项目文件夹",
            initialdir=initial or str(Path.cwd()),
            mustexist=True,
        )
    finally:
        root.destroy()
    if not selected:
        return {"success": False, "cancelled": True, "error": "未选择项目文件夹"}
    return {"success": True, "path": selected}


def _home_label(home: dict[str, Any]) -> str:
    return str(home.get("label") or home.get("id") or "Windows")


def _decorate_home_conversations(result: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
    if not result.get("success"):
        return result
    home_id = str(home.get("id") or "windows")
    home_kind = str(home.get("kind") or "windows")
    label = _home_label(home)
    distro = home.get("distro")
    for item in result.get("conversations", []) or []:
        if isinstance(item, dict):
            item.setdefault("home_id", home_id)
            item.setdefault("home_kind", home_kind)
            item.setdefault("home_label", label)
            if distro:
                item.setdefault("distro", distro)
    result["home_id"] = home_id
    result["home_kind"] = home_kind
    result["home_label"] = label
    return result


def _conversation_sort_key(item: dict[str, Any]) -> int:
    try:
        return int(item.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0


def _list_conversations_for_home(home: dict[str, Any], search: str, cwd: str, provider: str, include_archived: bool) -> dict[str, Any]:
    home_id = str(home.get("id") or "windows")
    if home_id.startswith("wsl:"):
        result = list_wsl_conversations(
            home_id.split(":", 1)[1],
            search=search,
            cwd=cwd,
            provider=provider,
            include_archived=include_archived,
        )
    else:
        result = list_conversations(search=search, cwd=cwd, provider=provider, include_archived=include_archived)
    return _decorate_home_conversations(result, home)


def _usable_homes() -> list[dict[str, Any]]:
    homes = list_codex_homes().get("homes", []) or []
    usable = [home for home in homes if isinstance(home, dict) and home.get("ok") and home.get("has_codex")]
    return usable or [{"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True}]


def _list_conversations_for_scope(source_home: str, search: str, cwd: str, provider: str, include_archived: bool) -> dict[str, Any]:
    if source_home == "all":
        conversations: list[dict[str, Any]] = []
        cwds: set[str] = set()
        providers: set[str] = set()
        errors: list[dict[str, Any]] = []
        success_count = 0
        homes = _usable_homes()
        for home in homes:
            result = _list_conversations_for_home(home, search, cwd, provider, include_archived)
            if result.get("success"):
                success_count += 1
                conversations.extend([item for item in result.get("conversations", []) or [] if isinstance(item, dict)])
                cwds.update(str(item) for item in result.get("cwds", []) or [] if item)
                providers.update(str(item) for item in result.get("providers", []) or [] if item)
            else:
                errors.append({"home_id": home.get("id"), "label": _home_label(home), "error": result.get("error")})
        conversations.sort(key=_conversation_sort_key, reverse=True)
        return {
            "success": success_count > 0 or not errors,
            "source_home": "all",
            "home_scope": "all",
            "homes": homes,
            "conversations": conversations[:2000],
            "count": len(conversations[:2000]),
            "cwds": sorted(cwds),
            "providers": sorted(providers),
            "errors": errors,
            "error": "; ".join(str(item.get("error")) for item in errors) if errors and not conversations else None,
        }
    home = next((item for item in _usable_homes() if item.get("id") == source_home), None)
    if home is None:
        if source_home.startswith("wsl:"):
            home = {"id": source_home, "kind": "wsl", "distro": source_home.split(":", 1)[1], "label": f"WSL {source_home.split(':', 1)[1]}"}
        else:
            home = {"id": "windows", "kind": "windows", "label": "Windows"}
    result = _list_conversations_for_home(home, search, cwd, provider, include_archived)
    result["source_home"] = str(home.get("id") or source_home)
    return result


def _channels_for_home(home: dict[str, Any]) -> dict[str, Any]:
    home_id = str(home.get("id") or "windows")
    if home_id.startswith("wsl:"):
        result = list_wsl_channels(home_id.split(":", 1)[1])
        result.setdefault("merged", {"total": 0, "by_target": {}, "origin_breakdown": {}})
        result.setdefault("codex_running", False)
        result["write_supported"] = False
    else:
        result = list_channels()
        result["write_supported"] = True
    result["home_id"] = home_id
    result["home_kind"] = str(home.get("kind") or ("wsl" if home_id.startswith("wsl:") else "windows"))
    result["home_label"] = _home_label(home)
    return result


def _channels_for_scope(source_home: str) -> dict[str, Any]:
    if source_home == "all":
        groups = [_channels_for_home(home) for home in _usable_homes()]
        ok_groups = [group for group in groups if group.get("success")]
        flattened: list[dict[str, Any]] = []
        for group in ok_groups:
            for channel in group.get("channels", []) or []:
                if isinstance(channel, dict):
                    flattened.append({**channel, "home_id": group.get("home_id"), "home_label": group.get("home_label")})
        return {
            "success": bool(ok_groups),
            "source_home": "all",
            "home_groups": groups,
            "channels": flattened,
            "write_supported": False,
            "error": "; ".join(str(group.get("error")) for group in groups if group.get("error")) or None,
        }
    home = next((item for item in _usable_homes() if item.get("id") == source_home), None)
    if home is None:
        if source_home.startswith("wsl:"):
            home = {"id": source_home, "kind": "wsl", "distro": source_home.split(":", 1)[1], "label": f"WSL {source_home.split(':', 1)[1]}"}
        else:
            home = {"id": "windows", "kind": "windows", "label": "Windows"}
    result = _channels_for_home(home)
    result["source_home"] = source_home
    return result


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_static(url_path: str) -> tuple[Path, str] | None:
    decoded = unquote(url_path or "")
    if decoded in ("", "/"):
        decoded = "/index.html"
    candidate = STATIC_DIR / decoded.lstrip("/")
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if not is_within(candidate, STATIC_DIR) or not candidate.is_file():
        return None
    content_type = STATIC_CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    return candidate, content_type


class DesktopRuntime:
    def __init__(self) -> None:
        self.session_token = secrets.token_urlsafe(32)
        self.logs: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.daemon_stop = threading.Event()
        self.daemon_thread: threading.Thread | None = None

    def log(self, level: str, message: str, data: Any | None = None) -> None:
        entry = {"time": utc_now(), "level": level, "message": message, "data": data}
        with self.lock:
            self.logs.append(entry)
            self.logs = self.logs[-200:]

    def status(self) -> dict[str, Any]:
        cfg = load_config()
        return {
            "time": utc_now(),
            "cwd": str(Path.cwd()),
            "config": {
                "server_url": cfg.server_url,
                "api_token_configured": bool(cfg.api_token),
                "device_id": cfg.device_id,
                "sync_interval_seconds": cfg.sync_interval_seconds,
                "max_untracked_copy_bytes": cfg.max_untracked_copy_bytes,
                "disaster_backup_enabled": cfg.disaster_backup_enabled,
                "disaster_backup_min_interval_seconds": cfg.disaster_backup_min_interval_seconds,
                "full_backup_enabled": cfg.full_backup_enabled,
                "full_backup_include_config": cfg.full_backup_include_config,
                "full_backup_include_memories": cfg.full_backup_include_memories,
                "full_backup_allow_plaintext_upload": cfg.full_backup_allow_plaintext_upload,
                "full_backup_quiet_seconds": cfg.full_backup_quiet_seconds,
                "full_backup_retention_count": cfg.full_backup_retention_count,
                "full_backup_retention_max_bytes": cfg.full_backup_retention_max_bytes,
            },
            "outbox_count": outbox_count(),
            "hooks": hook_status(),
            "git": _project_state(),
            "daemon_running": bool(self.daemon_thread and self.daemon_thread.is_alive()),
            "logs": self.logs[-80:],
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = load_config()
        for key in (
            "server_url",
            "api_token",
            "device_id",
            "sync_interval_seconds",
            "max_untracked_copy_bytes",
            "disaster_backup_enabled",
            "disaster_backup_min_interval_seconds",
            "full_backup_enabled",
            "full_backup_include_config",
            "full_backup_include_memories",
            "full_backup_allow_plaintext_upload",
            "full_backup_quiet_seconds",
            "full_backup_retention_count",
            "full_backup_retention_max_bytes",
        ):
            if key in payload:
                value = payload[key]
                if key in (
                    "sync_interval_seconds",
                    "max_untracked_copy_bytes",
                    "disaster_backup_min_interval_seconds",
                    "full_backup_quiet_seconds",
                    "full_backup_retention_count",
                    "full_backup_retention_max_bytes",
                ):
                    value = int(value)
                if key in (
                    "disaster_backup_enabled",
                    "full_backup_enabled",
                    "full_backup_include_config",
                    "full_backup_include_memories",
                    "full_backup_allow_plaintext_upload",
                ):
                    value = bool(value)
                setattr(cfg, key, value)
        path = save_config(cfg)
        self.log("ok", "config saved", {"path": str(path)})
        return {"saved": str(path), "config": cfg.to_public_dict()}

    def run_action(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        self.log("run", f"action started: {name}")
        cfg = load_config()
        if name == "sync-now":
            result = {
                "sync": sync_once(cfg, skip_unchanged=bool(payload.get("skip_unchanged_snapshot", True))),
                "flush_outbox": flush_outbox(cfg),
            }
            if cfg.full_backup_enabled:
                result["full_backup_scan"] = scan_full_backup_changes(cfg, create_package=True, notify_dirty=True, check_remote=True)
                result["device_state"] = get_remote_device_state(cfg)
        elif name == "flush-outbox":
            result = flush_outbox(cfg)
        elif name == "install-hooks":
            result = install_hooks()
        elif name == "preflight-backup":
            result = create_disaster_backup(cfg, reason=payload.get("reason", "web_desktop"), force=bool(payload.get("force", True)))
        elif name == "test-capture":
            result = capture_event("WebDesktopTest", json.dumps({"message": "web desktop capture test"}), cfg)
        elif name == "git-state":
            result = _project_state(_project_path_from_payload(payload))
        elif name == "git-snapshot":
            result = create_patch_snapshot(_project_path_from_payload(payload), cfg)
        elif name == "project-backup":
            result = backup_project_to_server(_project_path_from_payload(payload), cfg)
        elif name == "list-project-backups":
            result = list_project_backups(cfg)
        elif name == "choose-project-dir":
            result = _choose_project_directory(_project_path_from_payload(payload))
        elif name == "full-backup-now":
            result = full_backup_now(
                cfg,
                force=bool(payload.get("force", False)),
                upload=bool(payload.get("upload", False)),
                allow_plaintext_upload=bool(payload.get("allow_plaintext_upload", False)),
            )
        elif name == "full-backup-status":
            result = {"local": scan_full_backup_changes(cfg, create_package=False), "remote": get_remote_device_state(cfg)}
        elif name == "sync-health":
            result = summarize_sync_health(cfg)
        elif name == "list-devices":
            result = list_remote_devices(cfg)
        elif name in {"server-compatibility", "server-version"}:
            result = check_server_compatibility(cfg)
        elif name == "server-retention-status":
            result = get_server_retention(cfg)
        elif name == "server-retention-prune":
            result = prune_server_retention(cfg, dry_run=bool(payload.get("dry_run", False)))
        elif name == "deploy-config":
            result = {"success": True, "path": str(deploy_config_path()), "config": load_deploy_config().to_dict()}
        elif name == "save-deploy-config":
            deploy_cfg, _ = _deploy_from_payload(payload)
            path = save_deploy_config(deploy_cfg)
            result = {"success": True, "saved": str(path), "config": deploy_cfg.to_dict()}
        elif name == "deploy-status":
            deploy_cfg, ssh_password = _deploy_from_payload(payload)
            result = deploy_status(deploy_cfg, ssh_password=ssh_password)
        elif name == "deploy-update":
            deploy_cfg, ssh_password = _deploy_from_payload(payload)
            if bool(payload.get("save_config", False)):
                save_deploy_config(deploy_cfg)
            result = update_server(
                deploy_cfg,
                dry_run=bool(payload.get("dry_run", False)),
                assume_yes=True,
                ssh_password=ssh_password,
            )
        elif name == "deploy-install":
            deploy_cfg, ssh_password = _deploy_from_payload(payload)
            if bool(payload.get("save_config", True)):
                save_deploy_config(deploy_cfg)
            result = install_server(
                deploy_cfg,
                dry_run=bool(payload.get("dry_run", False)),
                assume_yes=True,
                regenerate_token=bool(payload.get("regenerate_token", False)),
                backfill_local=bool(payload.get("backfill_local", True)),
                ssh_password=ssh_password,
            )
        elif name == "notify-change":
            result = notify_codex_changed(cfg, reason=str(payload.get("reason", "web_manual")), include_digest=bool(payload.get("include_digest", True)))
        elif name == "list-full-backups":
            result = list_full_backups(cfg)
        elif name == "resume":
            result = {"path": str(create_resume_prompt(cfg))}
        elif name == "wsl-status":
            result = wsl_status()
        elif name == "list-codex-homes":
            result = list_codex_homes()
        elif name == "list-target-channels":
            target_home = str(payload.get("target_home") or "windows")
            if target_home.startswith("wsl:"):
                result = list_wsl_channels(target_home.split(":", 1)[1])
            else:
                result = list_channels()
        elif name == "wsl-pull":
            distro = payload.get("distro")
            if not distro:
                status = wsl_status()
                distros = status.get("distros", [])
                if not distros:
                    result = {"error": "No WSL distro found"}
                else:
                    result = pull_wsl_config(distros[0]["distro"])
            else:
                result = pull_wsl_config(str(distro))
        elif name == "wsl-full-backup":
            distro = payload.get("distro")
            if not distro:
                result = {"success": False, "error": "Missing 'distro' parameter"}
            else:
                result = wsl_full_backup_now(
                    cfg,
                    str(distro),
                    include_config=bool(payload.get("include_config", True)),
                    include_memories=bool(payload.get("include_memories", True)),
                    upload=bool(payload.get("upload", False)),
                    allow_plaintext_upload=bool(payload.get("allow_plaintext_upload", False)),
                )
        elif name == "wsl-restore-full-backup":
            distro = payload.get("distro")
            archive = payload.get("archive")
            confirm = payload.get("confirm_backup_id")
            if not distro or not archive or not confirm:
                result = {"success": False, "error": "Missing 'distro'/'archive'/'confirm_backup_id' parameter"}
            else:
                result = restore_wsl_full_backup(
                    str(distro),
                    str(archive),
                    confirm_backup_id=str(confirm),
                    restore_config=bool(payload.get("restore_config", False)),
                )
        elif name == "wsl-restore-latest":
            distro = payload.get("distro")
            if not distro:
                result = {"success": False, "error": "Missing 'distro' parameter"}
            else:
                result = restore_latest_wsl_full_backup(str(distro), restore_config=bool(payload.get("restore_config", False)))
        elif name == "start-daemon":
            result = self.start_daemon()
        elif name == "stop-daemon":
            result = self.stop_daemon()
        elif name == "list-snapshots":
            result = list_remote_snapshots(cfg)
        elif name == "snapshot-detail":
            snapshot_id = payload.get("snapshot_id")
            if not snapshot_id:
                raise ValueError("Missing 'snapshot_id' parameter")
            result = get_remote_snapshot_detail(cfg, str(snapshot_id))
        elif name == "remote-resume":
            snapshot_id = payload.get("snapshot_id")
            if not snapshot_id:
                raise ValueError("Missing 'snapshot_id' parameter")
            result = generate_remote_resume_context(cfg, str(snapshot_id))
        elif name == "preview-restore":
            snapshot_id = payload.get("snapshot_id")
            if not snapshot_id:
                raise ValueError("Missing 'snapshot_id' parameter")
            result = preview_restore_snapshot(cfg, str(snapshot_id), restore_hooks=bool(payload.get("restore_hooks", False)))
        elif name == "restore-snapshot":
            snapshot_id = payload.get("snapshot_id")
            if not snapshot_id:
                raise ValueError("Missing 'snapshot_id' parameter")
            result = restore_snapshot_locally(
                cfg,
                str(snapshot_id),
                confirm_snapshot_id=str(payload.get("confirm_snapshot_id", "")),
                restore_hooks=bool(payload.get("restore_hooks", False)),
            )
        elif name == "install-task":
            result = install_windows_task(minutes=int(payload.get("minutes", cfg.sync_interval_seconds // 60 or 3)))
        elif name == "uninstall-task":
            result = uninstall_windows_task()
        elif name == "task-status":
            result = windows_task_status()
        elif name == "list-channels":
            result = _channels_for_scope(str(payload.get("source_home") or "windows"))
        elif name == "merge-channels":
            source_home = str(payload.get("source_home") or "windows")
            if source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "渠道并入当前只支持 Windows 单环境，请先选择 Windows。"}
            else:
                result = merge_channels(
                    cfg,
                    sources=payload.get("sources") or None,
                    target=payload.get("target") or None,
                    all_others=bool(payload.get("all", False)),
                    close_running=bool(payload.get("close_codex", False)),
                )
        elif name == "restore-channels":
            source_home = str(payload.get("source_home") or "windows")
            if source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "渠道还原当前只支持 Windows 单环境，请先选择 Windows。"}
            else:
                result = restore_channels(
                    cfg,
                    sources=payload.get("sources") or None,
                    all_merged=bool(payload.get("all", False)),
                    close_running=bool(payload.get("close_codex", False)),
                )
        elif name == "list-conversations":
            result = _list_conversations_for_scope(
                str(payload.get("source_home") or "windows"),
                search=str(payload.get("search", "")),
                cwd=str(payload.get("cwd", "")),
                provider=str(payload.get("provider", "")),
                include_archived=bool(payload.get("include_archived", False)),
            )
        elif name == "read-conversation":
            thread_id = payload.get("thread_id")
            if not thread_id:
                raise ValueError("Missing 'thread_id' parameter")
            source_home = str(payload.get("source_home") or payload.get("home_id") or "windows")
            if source_home.startswith("wsl:"):
                result = read_wsl_conversation(
                    source_home.split(":", 1)[1],
                    str(thread_id),
                    include_tools=bool(payload.get("include_tools", True)),
                    include_reasoning=bool(payload.get("include_reasoning", False)),
                    include_developer=bool(payload.get("include_developer", False)),
                )
            else:
                result = read_conversation(
                    str(thread_id),
                    include_tools=bool(payload.get("include_tools", True)),
                    include_reasoning=bool(payload.get("include_reasoning", False)),
                    include_developer=bool(payload.get("include_developer", False)),
                )
        elif name == "export-conversation":
            thread_id = payload.get("thread_id")
            if not thread_id:
                raise ValueError("Missing 'thread_id' parameter")
            source_home = str(payload.get("source_home") or payload.get("home_id") or "windows")
            if source_home.startswith("wsl:"):
                result = export_wsl_conversation(
                    source_home.split(":", 1)[1],
                    str(thread_id),
                    fmt=str(payload.get("fmt", "markdown")),
                    include_tools=bool(payload.get("include_tools", False)),
                    include_reasoning=bool(payload.get("include_reasoning", False)),
                )
            else:
                result = export_conversation(
                    str(thread_id),
                    fmt=str(payload.get("fmt", "markdown")),
                    include_tools=bool(payload.get("include_tools", False)),
                    include_reasoning=bool(payload.get("include_reasoning", False)),
                )
        elif name == "merge-threads":
            source_home = str(payload.get("source_home") or "windows")
            if source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "对话并入当前只支持 Windows 单环境，请先选择 Windows。"}
            else:
                result = merge_threads(
                    cfg,
                    thread_ids=payload.get("thread_ids") or [],
                    target=payload.get("target") or None,
                    close_running=bool(payload.get("close_codex", False)),
                )
        elif name == "restore-threads":
            source_home = str(payload.get("source_home") or "windows")
            if source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "对话还原当前只支持 Windows 单环境，请先选择 Windows。"}
            else:
                result = restore_threads(
                    cfg,
                    thread_ids=payload.get("thread_ids") or [],
                    close_running=bool(payload.get("close_codex", False)),
                )
        elif name == "list-importable-backups":
            result = list_importable_backups(cfg)
        elif name == "list-backup-conversations":
            backup_id = payload.get("backup_id")
            if not backup_id:
                raise ValueError("Missing 'backup_id' parameter")
            archive, err = _ensure_local_archive(cfg, str(backup_id))
            if err:
                result = {"success": False, "error": err}
            else:
                target_home = str(payload.get("target_home") or "windows")
                if target_home.startswith("wsl:"):
                    distro = target_home.split(":", 1)[1]
                    result = list_backup_conversations(
                        archive,
                        local_index=wsl_threads_index(distro),
                        current_provider=list_wsl_channels(distro).get("current_provider"),
                    )
                else:
                    result = list_backup_conversations(archive)
        elif name == "read-backup-conversation":
            backup_id = payload.get("backup_id")
            thread_id = payload.get("thread_id")
            if not backup_id or not thread_id:
                raise ValueError("Missing 'backup_id'/'thread_id' parameter")
            archive, err = _ensure_local_archive(cfg, str(backup_id))
            result = {"success": False, "error": err} if err else read_backup_conversation(archive, str(thread_id))
        elif name == "import-conversations":
            backup_id = payload.get("backup_id")
            if not backup_id:
                raise ValueError("Missing 'backup_id' parameter")
            archive, err = _ensure_local_archive(cfg, str(backup_id))
            if err:
                result = {"success": False, "error": err}
            else:
                target_home = str(payload.get("target_home") or "windows")
                if target_home.startswith("wsl:"):
                    result = import_conversations_to_wsl(
                        target_home.split(":", 1)[1],
                        archive,
                        thread_ids=payload.get("thread_ids") or [],
                        target_provider=payload.get("target") or None,
                        close_running=bool(payload.get("close_codex", False)),
                    )
                else:
                    result = import_conversations(
                        cfg,
                        archive,
                        thread_ids=payload.get("thread_ids") or [],
                        target_provider=payload.get("target") or None,
                        close_running=bool(payload.get("close_codex", False)),
                    )
        else:
            raise ValueError(f"Unknown action: {name}")
        self.log("ok", f"action completed: {name}", result)
        return result

    def start_daemon(self) -> dict[str, Any]:
        if self.daemon_thread and self.daemon_thread.is_alive():
            return {"daemon_running": True, "message": "Daemon already running"}
        cfg = load_config()
        protection = create_disaster_backup(cfg, reason="before_web_daemon")
        self.log("ok", "preflight backup completed before daemon", protection)
        self.daemon_stop.clear()
        self.daemon_thread = threading.Thread(target=run_daemon, args=(cfg, self.daemon_stop.is_set, lambda msg: self.log("daemon", msg)), daemon=True)
        self.daemon_thread.start()
        return {"daemon_running": True, "preflight_backup": protection}

    def stop_daemon(self) -> dict[str, Any]:
        self.daemon_stop.set()
        return {"daemon_running": False, "message": "Daemon stop requested"}


def _json_response(handler: BaseHTTPRequestHandler, value: Any, status: int = 200) -> None:
    body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _add_no_cache_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _file_response(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    if content_type.startswith("application/javascript"):
        body = _rewrite_js_imports(path.read_text(encoding="utf-8")).encode("utf-8")
    else:
        body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    _add_no_cache_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _index_response(handler: BaseHTTPRequestHandler, path: Path, runtime: DesktopRuntime) -> None:
    html = _rewrite_index_assets(path.read_text(encoding="utf-8"))
    html = re.sub(r"Codex Sync · v[0-9.]+", f"Codex Sync · v{ASSET_VERSION}", html)
    meta = f'<meta name="codex-sync-desktop-token" content="{runtime.session_token}" />'
    version_meta = f'<meta name="codex-sync-version" content="{ASSET_VERSION}" />'
    body = html.replace("</head>", f"    {meta}\n    {version_meta}\n  </head>", 1).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _add_no_cache_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(runtime: DesktopRuntime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexSyncDesktop/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            runtime.log("http", format % args)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                if not self.check_session_token():
                    return
                _json_response(self, runtime.status())
                return
            resolved = resolve_static(parsed.path)
            if resolved is not None:
                if resolved[0].name == "index.html":
                    _index_response(self, resolved[0], runtime)
                    return
                _file_response(self, resolved[0], resolved[1])
                return
            _json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                if not self.check_session_token():
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                parsed = urlparse(self.path)
                if parsed.path == "/api/config":
                    _json_response(self, runtime.save_config(payload))
                    return
                if parsed.path == "/api/action":
                    name = payload.get("name")
                    if not name:
                        _json_response(self, {"error": "missing action name"}, HTTPStatus.BAD_REQUEST)
                        return
                    _json_response(self, runtime.run_action(str(name), payload))
                    return
                _json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # noqa: BLE001 - browser test console should show details
                runtime.log("error", str(exc))
                _json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def check_session_token(self) -> bool:
            token = self.headers.get("X-Codex-Sync-Desktop-Token", "")
            if secrets.compare_digest(token, runtime.session_token):
                return True
            _json_response(self, {"error": "invalid desktop session token"}, HTTPStatus.FORBIDDEN)
            return False

    return Handler


def find_port(start: int = DEFAULT_PORT) -> int:
    for port in range(start, start + 50):
        try:
            server = ThreadingHTTPServer((HOST, port), BaseHTTPRequestHandler)
            server.server_close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free local port found for Codex Sync desktop.")


def main(open_browser: bool = True, port: int | None = None) -> None:
    runtime = DesktopRuntime()
    protection = create_disaster_backup(load_config(), reason="web_desktop_start")
    runtime.log("ok", "preflight backup completed before desktop start", protection)
    port = port or find_port()
    server = ThreadingHTTPServer((HOST, port), make_handler(runtime))
    url = f"http://{HOST}:{port}/"
    runtime.log("ok", "modern desktop console started", {"url": url})
    if open_browser:
        webbrowser.open(url)
    print(f"Codex Sync desktop: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        runtime.stop_daemon()
        server.shutdown()
