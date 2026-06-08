from __future__ import annotations

import json
import os
import re
import secrets
import sys
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
from .config import AppConfig, load_config, save_config
from .daemon import run_daemon
from .conversations import export_conversation, list_conversations, read_conversation
from .deploy import DeployConfig, deploy_config_path, deploy_status, install_server, load_deploy_config, save_deploy_config, update_server
from .disaster_backup import create_disaster_backup
from .app_install import app_install_status, install_app, set_startup_enabled
from .app_update import check_app_update, download_latest_update, open_update_page
from .git_backup import (
    backup_project_to_server,
    create_patch_snapshot,
    git_state,
    list_project_backups,
    preview_project_backup_from_server,
    restore_project_backup_from_server,
)
from .full_backup import encryption_status, full_backup_now, get_remote_device_state, list_full_backups, list_remote_devices, notify_codex_changed, scan_full_backup_changes, summarize_sync_health
from .hooks import hook_status, install_hooks
from .project_auto_backup import (
    enqueue_project_auto_backup,
    install_project_git_hook,
    process_project_auto_backup_queue,
    project_auto_backup_status,
    uninstall_project_git_hook,
)
from .server import (
    check_server_compatibility,
    flush_outbox,
    get_server_retention,
    outbox_count,
    prune_server_retention,
    snapshot_state_path,
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
    merge_wsl_channels,
    merge_wsl_threads,
    pull_wsl_config,
    read_wsl_conversation,
    restore_latest_wsl_full_backup,
    restore_wsl_channels,
    restore_wsl_full_backup,
    restore_wsl_threads,
    wsl_full_backup_now,
    wsl_full_backup_status,
    wsl_threads_index,
    wsl_status,
)


STATIC_DIR = Path(__file__).resolve().parent / "web"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ASSET_VERSION = __version__
CACHE_CONTROL = "no-store, max-age=0, must-revalidate"
ERROR_ALREADY_EXISTS = 183
WINDOW_TITLE = "Codex Sync"

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


class SingleInstance:
    def __init__(self, name: str = "Local\\CodexSyncDesktop") -> None:
        self.name = name
        self.handle: int | None = None
        self.already_running = False

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return True
        self.handle = int(handle)
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self) -> None:
        if os.name != "nt" or not self.handle:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)
        self.handle = None


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


def _desktop_hwnd() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return None
    return int(hwnd)


def _restore_desktop_window() -> bool:
    hwnd = _desktop_hwnd()
    if not hwnd:
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _hide_desktop_window() -> bool:
    hwnd = _desktop_hwnd()
    if not hwnd:
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow(hwnd, 0)  # SW_HIDE: remove from taskbar and keep process alive.
    return True


def _focus_existing_window() -> bool:
    return _restore_desktop_window()


def _show_close_dialog() -> tuple[str, bool]:
    """Return (choice, remember), where choice is minimize_to_tray | exit | cancel."""
    if os.name != "nt":
        return "exit", False
    import ctypes

    message = "关闭窗口时要怎么处理？\n\n是：隐藏到通知区域图标，后台继续运行。\n否：直接退出，停止本地服务。\n取消：返回应用。"
    MB_YESNOCANCEL = 0x00000003
    MB_ICONQUESTION = 0x00000020
    MB_DEFBUTTON1 = 0x00000000
    MB_SETFOREGROUND = 0x00010000
    result = ctypes.windll.user32.MessageBoxW(
        None,
        message,
        "关闭 Codex Sync",
        MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON1 | MB_SETFOREGROUND,
    )
    if result == 6:  # IDYES
        return "minimize_to_tray", False
    if result == 7:  # IDNO
        return "exit", False
    return "cancel", False


def _close_choice(config: AppConfig) -> str:
    if config.desktop_close_behavior in {"minimize_to_tray", "exit"}:
        return config.desktop_close_behavior
    choice, remember = _show_close_dialog()
    if remember and choice in {"minimize_to_tray", "exit"}:
        config.desktop_close_behavior = choice
        save_config(config)
    return choice


class WindowsTrayIcon:
    def __init__(self, window: Any, local: "LocalDesktopServer") -> None:
        self.window = window
        self.local = local
        self.hwnd: int | None = None
        self.exit_requested = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._wndproc: Any | None = None
        self._notify_data_type: Any | None = None
        self._shell32: Any | None = None
        self._user32: Any | None = None
        self._icon_handle: int | None = None
        self._icon_added = False

    @property
    def available(self) -> bool:
        return os.name == "nt" and bool(self.hwnd) and self._icon_added

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="CodexSyncTray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return bool(self.hwnd)

    def stop(self) -> None:
        if os.name != "nt":
            return
        hwnd = self.hwnd
        if hwnd and self._user32:
            try:
                self._user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:  # noqa: BLE001
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def restore_window(self) -> None:
        try:
            self.window.show()
            self.window.restore()
            _restore_desktop_window()
        except Exception as exc:  # noqa: BLE001
            self.local.runtime.log("error", "failed to restore tray window", {"error": str(exc)})

    def exit_app(self) -> None:
        self.exit_requested = True
        try:
            self.window.destroy()
        except Exception as exc:  # noqa: BLE001
            self.local.runtime.log("error", "failed to exit from tray", {"error": str(exc)})

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

        self._notify_data_type = NOTIFYICONDATAW
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = self._user32
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.restype = LRESULT
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.restype = wintypes.HMENU

        WM_CLOSE = 0x0010
        WM_DESTROY = 0x0002
        WM_TRAYICON = 0x0400 + 20
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        WM_CONTEXTMENU = 0x007B

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_TRAYICON:
                if lparam == WM_LBUTTONDBLCLK:
                    self.restore_window()
                    return 0
                if lparam in {WM_RBUTTONUP, WM_CONTEXTMENU}:
                    self._show_menu(hwnd)
                    return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._delete_icon()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"CodexSyncTrayWindow{os.getpid()}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        try:
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                self.local.runtime.log("error", "failed to create tray message window", {"error": ctypes.get_last_error()})
                self._ready.set()
                return
            self.hwnd = int(hwnd)
            self._icon_added = self._add_icon()
            if not self._icon_added:
                self.local.runtime.log("error", "failed to add tray notification icon", {"error": ctypes.get_last_error()})
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001
            self.local.runtime.log("error", "tray icon failed", {"error": str(exc)})
            self._ready.set()
        finally:
            self.hwnd = None

    def _load_icon(self) -> int:
        import ctypes

        if self._user32 is None:
            return 0
        icon_path = _desktop_icon_path()
        if icon_path:
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            icon = self._user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if icon:
                self._icon_handle = int(icon)
                return int(icon)
        IDI_APPLICATION = 32512
        icon = self._user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return int(icon or 0)

    def _icon_data(self) -> Any:
        import ctypes

        if self._notify_data_type is None or self.hwnd is None:
            return None
        data = self._notify_data_type()
        data.cbSize = ctypes.sizeof(self._notify_data_type)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = 0x0001 | 0x0002 | 0x0004  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = 0x0400 + 20
        data.hIcon = self._load_icon()
        data.szTip = "Codex Sync"
        return data

    def _add_icon(self) -> bool:
        import ctypes

        if self._shell32 is None:
            return False
        data = self._icon_data()
        if data is not None:
            return bool(self._shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(data)))  # NIM_ADD
        return False

    def _delete_icon(self) -> None:
        import ctypes

        if self._shell32 is None or self._notify_data_type is None or self.hwnd is None:
            return
        data = self._notify_data_type()
        data.cbSize = ctypes.sizeof(self._notify_data_type)
        data.hWnd = self.hwnd
        data.uID = 1
        self._shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(data))  # NIM_DELETE
        self._icon_added = False

    def _show_menu(self, hwnd: int) -> None:
        import ctypes
        from ctypes import wintypes

        if self._user32 is None:
            return
        ID_SHOW = 2001
        ID_EXIT = 2002
        MF_STRING = 0x0000
        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        try:
            self._user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示 Codex Sync")
            self._user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出 Codex Sync")
            point = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            self._user32.SetForegroundWindow(hwnd)
            command = self._user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
            if command == ID_SHOW:
                self.restore_window()
            elif command == ID_EXIT:
                self.exit_app()
        finally:
            self._user32.DestroyMenu(menu)


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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _status_item(
    item_id: str,
    group: str,
    name: str,
    status: str,
    tone: str = "",
    detail: str = "",
    **extra: Any,
) -> dict[str, Any]:
    item = {"id": item_id, "group": group, "name": name, "status": status, "tone": tone, "detail": detail}
    item.update({key: value for key, value in extra.items() if value not in (None, "")})
    return item


def _auto_scan_summary(config: AppConfig, task: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(task.get("installed"))
    return {
        "enabled": enabled,
        "interval_seconds": int(config.sync_interval_seconds or 0),
        "task_name": task.get("task_name"),
        "last_run": task.get("last_run"),
        "next_run": task.get("next_run"),
        "next_run_in_seconds": task.get("next_run_in_seconds"),
        "last_result": task.get("last_result"),
        "status": "已开启" if enabled else "未开启",
        "tone": "ok" if enabled else "warn",
        "error": task.get("error") if not enabled else None,
    }


def _snapshot_resume_status(outbox: int) -> dict[str, Any]:
    state = _read_json_file(snapshot_state_path())
    return {
        "outbox_count": outbox,
        "last_snapshot_id": state.get("last_snapshot_id"),
        "last_snapshot_at": state.get("last_snapshot_at"),
        "last_snapshot_signature": state.get("last_snapshot_signature"),
    }


def _sync_health_summary(items: list[dict[str, Any]], auto_scan: dict[str, Any]) -> dict[str, Any]:
    danger = sum(1 for item in items if item.get("tone") == "danger")
    warn = sum(1 for item in items if item.get("tone") == "warn")
    ok = sum(1 for item in items if item.get("tone") == "ok")
    next_seconds = auto_scan.get("next_run_in_seconds")
    if auto_scan.get("enabled") and isinstance(next_seconds, int):
        scan_text = f"自动扫描约 {max(1, (next_seconds + 59) // 60)} 分钟后"
    elif auto_scan.get("enabled"):
        scan_text = "自动扫描已开启"
    else:
        scan_text = "自动扫描未开启"
    if danger:
        text = f"{danger} 项异常 · {warn} 项待处理 · {scan_text}"
        tone = "danger"
    elif warn:
        text = f"{warn} 项待处理 · {ok} 项正常 · {scan_text}"
        tone = "warn"
    else:
        text = f"全部正常 · {ok} 项已检查 · {scan_text}"
        tone = "ok"
    return {"text": text, "tone": tone, "ok": ok, "warn": warn, "danger": danger, "scan_text": scan_text}


def _decorate_sync_health(config: AppConfig, health: dict[str, Any]) -> dict[str, Any]:
    checked_at = utc_now()
    task = windows_task_status()
    auto_scan = _auto_scan_summary(config, task)
    outbox = outbox_count()
    hooks = hook_status()
    git = _project_state()
    project_auto = project_auto_backup_status(Path.cwd(), config)
    snapshot = _snapshot_resume_status(outbox)
    homes_result: dict[str, Any]
    try:
        homes_result = list_codex_homes()
    except Exception as exc:  # noqa: BLE001 - health panel should degrade gracefully
        homes_result = {"success": False, "homes": [], "error": str(exc)}

    items: list[dict[str, Any]] = []
    local = _as_dict(health.get("local"))
    devices = health.get("devices") if isinstance(health.get("devices"), list) else []
    pending = health.get("pending_devices") if isinstance(health.get("pending_devices"), list) else []

    if not config.server_url:
        server_status, server_tone, server_detail = "未配置", "warn", "请在系统设置中配置同步服务器。"
    elif health.get("success"):
        server_status, server_tone, server_detail = "正常", "ok", f"已读取 {len(devices)} 台设备状态。"
    else:
        server_status, server_tone, server_detail = "检查失败", "danger", str(health.get("error") or "无法连接同步服务器。")
    items.append(
        _status_item(
            "server",
            "基础连接",
            "同步服务器",
            server_status,
            server_tone,
            server_detail,
            scope=config.server_url or "未配置",
            last_checked_at=checked_at,
            href="#/settings",
            action_label="配置服务器" if not config.server_url else "检查设置",
        )
    )

    if not config.full_backup_enabled:
        win_status, win_tone = "未开启", "warn"
    elif local.get("diverged"):
        win_status, win_tone = "远端有分歧", "warn"
    elif local.get("needs_upload"):
        win_status, win_tone = "有未上传变更", "warn"
    elif not local.get("last_content_digest"):
        win_status, win_tone = "尚未扫描", "warn"
    elif not local.get("last_uploaded_content_digest"):
        win_status, win_tone = "尚未上传", "warn"
    else:
        win_status, win_tone = "已上传最新变更", "ok"
    items.append(
        _status_item(
            "windows-conversations",
            "对话备份",
            "Windows 对话",
            win_status,
            win_tone,
            "Windows %USERPROFILE%\\.codex 的完整对话备份状态。",
            scope=config.device_id,
            last_checked_at=checked_at,
            last_change_at=local.get("last_dirty_at") or local.get("last_created_at"),
            last_uploaded_at=local.get("last_uploaded_at"),
            action="full-backup-now",
            action_label="扫描并上传",
        )
    )

    homes = homes_result.get("homes") if isinstance(homes_result.get("homes"), list) else []
    for home in homes:
        if not isinstance(home, dict) or home.get("kind") != "wsl":
            continue
        label = str(home.get("label") or home.get("id") or "WSL")
        distro = str(home.get("distro") or "")
        if not home.get("ok"):
            status, tone, detail = "不可访问", "danger", str(home.get("error") or homes_result.get("error") or "WSL 发行版不可访问。")
            extra: dict[str, Any] = {}
        elif not home.get("has_codex"):
            status, tone, detail = "未检测到 Codex", "warn", "该发行版中没有 ~/.codex 目录。"
            extra = {}
        else:
            wsl_state = wsl_full_backup_status(distro)
            if wsl_state.get("needs_upload"):
                status, tone = "有未上传变更", "warn"
            elif not wsl_state.get("last_content_digest"):
                status, tone = "尚未备份", "warn"
            elif not wsl_state.get("last_uploaded_content_digest"):
                status, tone = "尚未上传", "warn"
            else:
                status, tone = "已上传最新备份", "ok"
            detail = f"{label} 的 ~/.codex 完整对话备份状态。"
            extra = {
                "last_change_at": wsl_state.get("last_created_at"),
                "last_uploaded_at": wsl_state.get("last_uploaded_at"),
                "action": "wsl-full-backup",
                "action_label": "备份并上传",
                "payload": {"distro": distro, "include_config": True, "include_memories": True, "upload": True},
            }
        items.append(
            _status_item(
                f"wsl-{distro}",
                "对话备份",
                label,
                status,
                tone,
                detail,
                scope=distro,
                last_checked_at=checked_at,
                href="#/backups",
                **extra,
            )
        )
    if homes_result.get("success") is False and not any(item.get("group") == "对话备份" and str(item.get("id", "")).startswith("wsl-") for item in items):
        items.append(
            _status_item(
                "wsl-detect",
                "对话备份",
                "WSL 环境",
                "检查失败",
                "danger",
                str(homes_result.get("error") or homes_result.get("wsl_error") or "无法读取 WSL 环境。"),
                last_checked_at=checked_at,
                href="#/backups",
                action_label="查看备份",
            )
        )

    if health.get("success"):
        remote_status = f"{len(pending)} 台待处理" if pending else "正常"
        remote_tone = "warn" if pending else "ok"
        pending_names = ", ".join(str(item.get("device_id") or "-") for item in pending[:3] if isinstance(item, dict))
        remote_detail = f"远端待处理设备：{pending_names}" if pending_names else "其它设备没有 dirty/diverged 状态。"
    else:
        remote_status, remote_tone, remote_detail = "未检查", "warn", "服务器不可用时无法读取其它设备。"
    items.append(
        _status_item(
            "remote-devices",
            "远端状态",
            "其它设备",
            remote_status,
            remote_tone,
            remote_detail,
            last_checked_at=checked_at,
            href="#/backups",
            action_label="查看云端备份",
        )
    )

    queue_count = int(project_auto.get("queue_count") or 0)
    project_last = _as_dict(project_auto.get("last"))
    if queue_count:
        project_status, project_tone = f"{queue_count} 个待上传", "warn"
    elif not config.server_url:
        project_status, project_tone = "服务器未配置", "warn"
    elif not git.get("exists", True) or git.get("is_dir") is False:
        project_status, project_tone = "路径不可用", "danger"
    elif not git.get("is_repo"):
        if project_auto.get("codex_stop_enabled"):
            project_status, project_tone = "Codex Stop 自动入队", "ok"
        else:
            project_status, project_tone = "可手动或 Stop 入队", "warn"
    elif git.get("dirty"):
        project_status, project_tone = "有本地改动", "warn"
    elif project_auto.get("enabled_for_git_commit") or project_auto.get("codex_stop_enabled"):
        project_status, project_tone = "自动备份已开启", "ok"
    else:
        project_status, project_tone = "自动备份未开启", "warn"
    items.append(
        _status_item(
            "project-backup",
            "项目备份",
            "当前项目",
            project_status,
            project_tone,
            "当前工作目录的项目备份与自动备份状态。",
            scope=git.get("root") or git.get("selected_path") or str(Path.cwd()),
            last_checked_at=checked_at,
            last_uploaded_at=project_last.get("last_backup_at"),
            href="#/project",
            action_label="查看项目备份",
        )
    )

    if snapshot["outbox_count"]:
        snap_status, snap_tone = f"{snapshot['outbox_count']} 条待发送", "warn"
    elif not config.server_url:
        snap_status, snap_tone = "服务器未配置", "warn"
    elif not snapshot.get("last_snapshot_at"):
        snap_status, snap_tone = "尚未生成", "warn"
    else:
        snap_status, snap_tone = "最近无待发送", "ok"
    items.append(
        _status_item(
            "resume-snapshot",
            "接力快照",
            "轻量接力状态",
            snap_status,
            snap_tone,
            "保存 cwd、repo 状态、Codex 配置元数据和最近 hook 事件，用于 remote-resume。",
            last_checked_at=checked_at,
            last_uploaded_at=snapshot.get("last_snapshot_at"),
            action="sync-now",
            action_label="立即同步",
        )
    )

    hook_events = hooks.get("events") if isinstance(hooks.get("events"), list) else []
    items.append(
        _status_item(
            "hooks",
            "自动化",
            "Codex Hooks",
            f"{len(hook_events)} 个事件" if hook_events else "未安装",
            "ok" if hook_events else "warn",
            "用于在 Codex 启动、提交提示、压缩和停止时捕获轻量状态。",
            scope=", ".join(hook_events) if hook_events else hooks.get("path"),
            last_checked_at=checked_at,
            href="#/settings",
            action_label="管理 Hooks",
        )
    )

    items.append(
        _status_item(
            "auto-scan",
            "自动化",
            "自动扫描任务",
            auto_scan["status"],
            auto_scan["tone"],
            "Windows 定时任务会静默运行 sync-now，并处理项目自动备份队列。",
            last_checked_at=checked_at,
            last_change_at=auto_scan.get("last_run"),
            next_run_at=auto_scan.get("next_run"),
            next_run_in_seconds=auto_scan.get("next_run_in_seconds"),
            href="#/settings",
            action_label="设置自动扫描",
        )
    )

    health["checked_at"] = checked_at
    health["task_status"] = task
    health["auto_scan"] = auto_scan
    health["snapshot_resume"] = snapshot
    health["project_auto_backup"] = project_auto
    health["codex_homes"] = homes_result
    health["status_items"] = items
    health["summary"] = _sync_health_summary(items, auto_scan)
    return health


def _choose_project_directory(initial: str | None = None, *, purpose: str = "project") -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"success": False, "error": f"无法打开目录选择器：{exc}"}
    title = "选择恢复目标文件夹" if purpose == "restore" else "选择项目文件夹"
    cancel_message = "未选择恢复目标文件夹" if purpose == "restore" else "未选择项目文件夹"
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title=title,
            initialdir=initial or str(Path.cwd()),
            mustexist=True,
        )
    finally:
        root.destroy()
    if not selected:
        return {"success": False, "cancelled": True, "error": cancel_message}
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
        result["write_supported"] = True
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
                "full_backup_encryption_enabled": cfg.full_backup_encryption_enabled,
                "full_backup_encryption_passphrase_configured": bool(cfg.full_backup_encryption_passphrase),
                "full_backup_encryption": encryption_status(cfg),
                "full_backup_allow_plaintext_upload": cfg.full_backup_allow_plaintext_upload,
                "full_backup_quiet_seconds": cfg.full_backup_quiet_seconds,
                "full_backup_retention_count": cfg.full_backup_retention_count,
                "full_backup_retention_max_bytes": cfg.full_backup_retention_max_bytes,
                "project_auto_backup_on_codex_stop": cfg.project_auto_backup_on_codex_stop,
                "project_auto_backup_min_interval_seconds": cfg.project_auto_backup_min_interval_seconds,
                "desktop_close_behavior": cfg.desktop_close_behavior,
            },
            "app_install": app_install_status(),
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
            "full_backup_encryption_enabled",
            "full_backup_encryption_passphrase",
            "full_backup_allow_plaintext_upload",
            "full_backup_quiet_seconds",
            "full_backup_retention_count",
            "full_backup_retention_max_bytes",
            "project_auto_backup_on_codex_stop",
            "project_auto_backup_min_interval_seconds",
            "desktop_close_behavior",
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
                    "project_auto_backup_min_interval_seconds",
                ):
                    value = int(value)
                if key in (
                    "disaster_backup_enabled",
                    "full_backup_enabled",
                    "full_backup_include_config",
                    "full_backup_include_memories",
                    "full_backup_encryption_enabled",
                    "full_backup_allow_plaintext_upload",
                    "project_auto_backup_on_codex_stop",
                ):
                    value = bool(value)
                if key == "desktop_close_behavior" and value not in {"ask", "minimize_to_tray", "exit"}:
                    value = "ask"
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
            result["project_auto_backup"] = process_project_auto_backup_queue(cfg)
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
            result = list_project_backups(
                cfg,
                limit=int(payload.get("limit") or 200),
                offset=int(payload.get("offset") or 0),
                repo_name=str(payload.get("repo_name") or "").strip() or None,
                device_id=str(payload.get("device_id") or "").strip() or None,
            )
        elif name == "preview-project-restore":
            result = preview_project_backup_from_server(cfg, str(payload.get("backup_id") or ""), _project_path_from_payload(payload) or str(Path.cwd()))
        elif name == "restore-project-backup":
            result = restore_project_backup_from_server(
                cfg,
                str(payload.get("backup_id") or ""),
                _project_path_from_payload(payload) or str(Path.cwd()),
                confirm_backup_id=str(payload.get("confirm_backup_id") or ""),
                overwrite=bool(payload.get("overwrite", True)),
            )
        elif name == "project-auto-backup-status":
            result = project_auto_backup_status(_project_path_from_payload(payload), cfg)
        elif name == "project-auto-backup-install-git-hook":
            result = install_project_git_hook(_project_path_from_payload(payload), cfg)
        elif name == "project-auto-backup-uninstall-git-hook":
            result = uninstall_project_git_hook(_project_path_from_payload(payload))
        elif name == "project-auto-backup-queue":
            result = enqueue_project_auto_backup(_project_path_from_payload(payload), cfg, reason=str(payload.get("reason") or "manual"))
        elif name == "project-auto-backup-process":
            result = process_project_auto_backup_queue(cfg, limit=int(payload.get("limit") or 5))
        elif name == "choose-project-dir":
            purpose = str(payload.get("purpose") or "project")
            result = _choose_project_directory(_project_path_from_payload(payload), purpose=purpose)
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
            result = _decorate_sync_health(cfg, summarize_sync_health(cfg))
        elif name == "list-devices":
            result = list_remote_devices(cfg)
        elif name in {"server-compatibility", "server-version"}:
            result = check_server_compatibility(cfg)
        elif name == "app-update-check":
            result = check_app_update(force=bool(payload.get("force", False)))
        elif name == "app-update-download":
            result = download_latest_update(force=bool(payload.get("force", False)))
        elif name == "app-update-open":
            result = open_update_page(force=bool(payload.get("force", False)))
        elif name == "app-install-status":
            result = app_install_status()
        elif name == "app-install":
            result = install_app(
                create_shortcuts=bool(payload.get("create_shortcuts", True)),
                enable_startup=bool(payload.get("enable_startup", False)),
            )
        elif name == "app-startup-enable":
            result = set_startup_enabled(True)
        elif name == "app-startup-disable":
            result = set_startup_enabled(False)
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
                    config=cfg,
                )
        elif name == "wsl-restore-latest":
            distro = payload.get("distro")
            if not distro:
                result = {"success": False, "error": "Missing 'distro' parameter"}
            else:
                result = restore_latest_wsl_full_backup(str(distro), restore_config=bool(payload.get("restore_config", False)), config=cfg)
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
            if source_home.startswith("wsl:"):
                result = merge_wsl_channels(
                    cfg,
                    source_home.split(":", 1)[1],
                    sources=payload.get("sources") or None,
                    target=payload.get("target") or None,
                    all_others=bool(payload.get("all", False)),
                    close_running=bool(payload.get("close_codex", False)),
                )
            elif source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "Unsupported source_home"}
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
            if source_home.startswith("wsl:"):
                result = restore_wsl_channels(
                    cfg,
                    source_home.split(":", 1)[1],
                    sources=payload.get("sources") or None,
                    all_merged=bool(payload.get("all", False)),
                    close_running=bool(payload.get("close_codex", False)),
                )
            elif source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "Unsupported source_home"}
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
            if source_home.startswith("wsl:"):
                result = merge_wsl_threads(
                    cfg,
                    source_home.split(":", 1)[1],
                    thread_ids=payload.get("thread_ids") or [],
                    target=payload.get("target") or None,
                    close_running=bool(payload.get("close_codex", False)),
                )
            elif source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "Unsupported source_home"}
            else:
                result = merge_threads(
                    cfg,
                    thread_ids=payload.get("thread_ids") or [],
                    target=payload.get("target") or None,
                    close_running=bool(payload.get("close_codex", False)),
                )
        elif name == "restore-threads":
            source_home = str(payload.get("source_home") or "windows")
            if source_home.startswith("wsl:"):
                result = restore_wsl_threads(
                    cfg,
                    source_home.split(":", 1)[1],
                    thread_ids=payload.get("thread_ids") or [],
                    close_running=bool(payload.get("close_codex", False)),
                )
            elif source_home != "windows":
                result = {"success": False, "write_supported": False, "error": "Unsupported source_home"}
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
                        config=cfg,
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


class LocalDesktopServer:
    def __init__(self, runtime: DesktopRuntime, server: ThreadingHTTPServer, url: str) -> None:
        self.runtime = runtime
        self.server = server
        self.url = url
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.server.serve_forever, name="CodexSyncDesktopServer", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.runtime.stop_daemon()
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def serve_forever(self) -> None:
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.runtime.stop_daemon()
            self.server.server_close()


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


def create_local_server(port: int | None = None) -> LocalDesktopServer:
    runtime = DesktopRuntime()
    protection = create_disaster_backup(load_config(), reason="web_desktop_start")
    runtime.log("ok", "preflight backup completed before desktop start", protection)
    port = port or find_port()
    server = ThreadingHTTPServer((HOST, port), make_handler(runtime))
    url = f"http://{HOST}:{port}/"
    runtime.log("ok", "modern desktop console started", {"url": url})
    return LocalDesktopServer(runtime, server, url)


def _desktop_icon_path() -> str | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if bundle_root:
        candidates.append(Path(bundle_root) / "assets" / "CodexSync.ico")
    candidates.extend(
        [
            PROJECT_ROOT / "assets" / "CodexSync.ico",
            Path.cwd() / "assets" / "CodexSync.ico",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _wait_for_background_server(local: LocalDesktopServer) -> None:
    try:
        while local.thread and local.thread.is_alive():
            local.thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass


def _attach_close_prompt(window: Any, local: LocalDesktopServer, tray: WindowsTrayIcon | None = None) -> None:
    def on_closing() -> bool | None:
        if tray and tray.exit_requested:
            local.runtime.log("ok", "desktop exit requested from tray")
            return None
        cfg = load_config()
        choice = _close_choice(cfg)
        local.runtime.log("run", "desktop close requested", {"choice": choice})
        if choice == "minimize_to_tray":
            errors: list[str] = []
            try:
                window.hide()
            except Exception as exc:  # noqa: BLE001 - close prompt should not crash the app
                errors.append(str(exc))
            native_hidden = _hide_desktop_window()
            if errors or not native_hidden:
                local.runtime.log("warn", "window hide requested; native hide fallback status", {"native_hidden": native_hidden, "errors": errors})
            if not (tray and tray.available):
                local.runtime.log("warn", "window hidden without a confirmed tray icon; relaunching the app will restore it")
            return False
        if choice == "cancel":
            return False
        local.runtime.log("ok", "desktop window exit confirmed")
        return None

    window.events.closing += on_closing


def open_desktop_window(local: LocalDesktopServer) -> None:
    local.start()
    tray: WindowsTrayIcon | None = None
    try:
        import webview
    except Exception as exc:  # noqa: BLE001 - desktop should remain usable without WebView runtime
        local.runtime.log("error", "pywebview unavailable; falling back to browser", {"error": str(exc)})
        webbrowser.open(local.url)
        _wait_for_background_server(local)
        local.stop()
        return
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        window = webview.create_window(
            WINDOW_TITLE,
            local.url,
            width=1280,
            height=860,
            min_size=(1040, 700),
            resizable=True,
            text_select=True,
            confirm_close=False,
            background_color="#0f172a",
        )
        if window is not None:
            tray = WindowsTrayIcon(window, local)
            if tray.start():
                local.runtime.log("ok", "tray icon started")
            else:
                local.runtime.log("warn", "tray icon unavailable")
            _attach_close_prompt(window, local, tray)
        webview.start(gui="edgechromium", debug=False, private_mode=True, icon=_desktop_icon_path())
    except Exception as exc:  # noqa: BLE001 - fall back to browser if WebView2 is missing/broken
        local.runtime.log("error", "desktop window failed; falling back to browser", {"error": str(exc)})
        webbrowser.open(local.url)
        _wait_for_background_server(local)
    finally:
        if tray is not None:
            tray.stop()
        local.stop()


def main(open_browser: bool = False, port: int | None = None, window: bool = True) -> None:
    instance = SingleInstance() if window else None
    if instance and not instance.acquire():
        _focus_existing_window()
        instance.release()
        return
    local = create_local_server(port)
    try:
        if window:
            open_desktop_window(local)
            return
        if open_browser:
            webbrowser.open(local.url)
        print(f"Codex Sync desktop: {local.url}")
        print("Press Ctrl+C to stop.")
        local.serve_forever()
    finally:
        if instance:
            instance.release()
