from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
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
from .full_backup import download_full_backup, encryption_status, export_encryption_key, full_backup_now, get_remote_device_state, import_encryption_key, list_full_backups, list_remote_devices, notify_codex_changed, restore_full_backup, restore_latest_full_backup, scan_full_backup_changes, summarize_sync_health
from .hooks import hook_status, install_hooks
from .paths import app_dir
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
SYNC_HEALTH_CACHE_SECONDS = 60
CONVERSATION_WORKSPACE_CACHE_SECONDS = 60
WARMUP_INITIAL_DELAY_SECONDS = 3
WARMUP_INTERVAL_SECONDS = max(15, SYNC_HEALTH_CACHE_SECONDS - 10)

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
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return None
    return int(hwnd)


def _restore_desktop_window() -> bool:
    hwnd = _desktop_hwnd()
    if not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _hide_desktop_window() -> bool:
    hwnd = _desktop_hwnd()
    if not hwnd:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow(hwnd, 0)  # SW_HIDE: remove from taskbar and keep process alive.
    return True


def _focus_existing_window() -> bool:
    return _restore_desktop_window()


def _show_close_dialog() -> tuple[str, bool]:
    """Ask how to handle the window close.

    Returns ``(choice, remember)`` where ``choice`` is ``minimize_to_tray`` |
    ``exit`` | ``cancel`` and ``remember`` reflects the "设为默认" checkbox.
    The title-bar X and the Esc key both map to ``cancel`` (do nothing).
    """
    if os.name != "nt":
        return "exit", False
    try:
        return _task_dialog_close_choice()
    except Exception:  # noqa: BLE001 - degrade to a plain message box, never crash on close
        return _message_box_close_choice()


def _task_dialog_close_choice() -> tuple[str, bool]:
    """Native Task Dialog with a real "记住我的选择" verification checkbox."""
    import ctypes
    from ctypes import wintypes

    class TASKDIALOG_BUTTON(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("nButtonID", ctypes.c_int),
            ("pszButtonText", wintypes.LPCWSTR),
        ]

    class TASKDIALOGCONFIG(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("hwndParent", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("dwFlags", wintypes.DWORD),
            ("dwCommonButtons", wintypes.DWORD),
            ("pszWindowTitle", wintypes.LPCWSTR),
            ("pszMainIcon", wintypes.LPCWSTR),
            ("pszMainInstruction", wintypes.LPCWSTR),
            ("pszContent", wintypes.LPCWSTR),
            ("cButtons", wintypes.UINT),
            ("pButtons", ctypes.POINTER(TASKDIALOG_BUTTON)),
            ("nDefaultButton", ctypes.c_int),
            ("cRadioButtons", wintypes.UINT),
            ("pRadioButtons", ctypes.c_void_p),
            ("nDefaultRadioButton", ctypes.c_int),
            ("pszVerificationText", wintypes.LPCWSTR),
            ("pszExpandedInformation", wintypes.LPCWSTR),
            ("pszExpandedControlText", wintypes.LPCWSTR),
            ("pszCollapsedControlText", wintypes.LPCWSTR),
            ("pszFooterIcon", wintypes.LPCWSTR),
            ("pszFooter", wintypes.LPCWSTR),
            ("pfCallback", ctypes.c_void_p),
            ("lpCallbackData", ctypes.c_void_p),
            ("cxWidth", wintypes.UINT),
        ]

    ID_MINIMIZE = 1001
    ID_EXIT = 1002
    TDF_ALLOW_DIALOG_CANCELLATION = 0x0008
    TDF_USE_COMMAND_LINKS = 0x0010
    TDF_POSITION_RELATIVE_TO_WINDOW = 0x1000

    buttons = (TASKDIALOG_BUTTON * 2)(
        TASKDIALOG_BUTTON(ID_MINIMIZE, "最小化到托盘\n关闭窗口但保留后台运行，双击通知区图标可重新打开"),
        TASKDIALOG_BUTTON(ID_EXIT, "直接退出\n完全结束 Codex Sync 并停止后台服务"),
    )

    parent = _desktop_hwnd() or 0
    config = TASKDIALOGCONFIG()
    config.cbSize = ctypes.sizeof(TASKDIALOGCONFIG)
    config.hwndParent = parent
    flags = TDF_ALLOW_DIALOG_CANCELLATION | TDF_USE_COMMAND_LINKS
    if parent:
        flags |= TDF_POSITION_RELATIVE_TO_WINDOW
    config.dwFlags = flags
    config.pszWindowTitle = "Codex Sync"
    config.pszMainInstruction = "关闭 Codex Sync"
    config.pszContent = "你想要怎么处理这个窗口？"
    config.cButtons = 2
    config.pButtons = buttons
    config.nDefaultButton = ID_MINIMIZE
    config.pszVerificationText = "记住我的选择，不再询问（可在设置中修改）"

    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
    comctl32.TaskDialogIndirect.argtypes = [
        ctypes.POINTER(TASKDIALOGCONFIG),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(wintypes.BOOL),
    ]
    comctl32.TaskDialogIndirect.restype = ctypes.HRESULT  # ctypes raises OSError on a failed HRESULT

    pressed = ctypes.c_int(0)
    radio = ctypes.c_int(0)
    verified = wintypes.BOOL(0)
    comctl32.TaskDialogIndirect(
        ctypes.byref(config), ctypes.byref(pressed), ctypes.byref(radio), ctypes.byref(verified)
    )
    remember = bool(verified.value)
    if pressed.value == ID_MINIMIZE:
        return "minimize_to_tray", remember
    if pressed.value == ID_EXIT:
        return "exit", remember
    return "cancel", remember  # IDCANCEL — title-bar X or Esc


def _message_box_close_choice() -> tuple[str, bool]:
    """Fallback dialog when the Task Dialog API is unavailable (no checkbox)."""
    import ctypes

    message = "关闭窗口时要怎么处理？\n\n是：最小化到通知区域，后台继续运行。\n否：直接退出，停止后台服务。\n取消：返回应用。"
    MB_YESNOCANCEL = 0x00000003
    MB_ICONQUESTION = 0x00000020
    MB_SETFOREGROUND = 0x00010000
    result = ctypes.windll.user32.MessageBoxW(
        None, message, "关闭 Codex Sync", MB_YESNOCANCEL | MB_ICONQUESTION | MB_SETFOREGROUND
    )
    if result == 6:  # IDYES
        return "minimize_to_tray", False
    if result == 7:  # IDNO
        return "exit", False
    return "cancel", False


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
                ("hCursor", wintypes.HANDLE),
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
        shell32 = self._shell32
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_void_p, wintypes.LPCWSTR]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.GetCursorPos.argtypes = [ctypes.c_void_p]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

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
            cx = self._user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
            cy = self._user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
            icon = self._user32.LoadImageW(None, icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if not icon:
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
        if data is None:
            return False
        # Use the classic notify-icon callback format (no NIM_SETVERSION) so tray
        # clicks arrive in the wParam=uID / lParam=mouse-event layout that _run's
        # wndproc decodes. Leave IsPromoted untouched — Windows keeps new icons in
        # the overflow flyout by default, which is what the user wants.
        return bool(self._shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(data)))  # NIM_ADD

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

    def notify(self, title: str, message: str) -> None:
        """Show a tray balloon so the user can locate the minimized app."""
        import ctypes

        if not self.available or self._shell32 is None or self._notify_data_type is None or self.hwnd is None:
            return
        try:
            data = self._notify_data_type()
            data.cbSize = ctypes.sizeof(self._notify_data_type)
            data.hWnd = self.hwnd
            data.uID = 1
            data.uFlags = 0x00000010  # NIF_INFO
            data.szInfo = message[:255]
            data.szInfoTitle = title[:63]
            data.dwInfoFlags = 0x00000001  # NIIF_INFO
            self._shell32.Shell_NotifyIconW(0x00000001, ctypes.byref(data))  # NIM_MODIFY
        except Exception as exc:  # noqa: BLE001 - a balloon tip is best-effort
            self.local.runtime.log("warn", "failed to show tray balloon", {"error": str(exc)})

    def _show_menu(self, hwnd: int) -> None:
        import ctypes
        from ctypes import wintypes

        if self._user32 is None:
            return
        ID_SHOW = 2001
        ID_EXIT = 2002
        ID_SYNC = 2003
        ID_FULL_BACKUP = 2004
        ID_PREFLIGHT = 2005
        ID_UPDATE = 2006
        MF_STRING = 0x0000
        MF_SEPARATOR = 0x0800
        TPM_RIGHTBUTTON = 0x0002
        TPM_RETURNCMD = 0x0100
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        command = 0
        try:
            self._user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "显示主窗口")
            self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, MF_STRING, ID_SYNC, "立即同步一次")
            self._user32.AppendMenuW(menu, MF_STRING, ID_FULL_BACKUP, "立即完整备份")
            self._user32.AppendMenuW(menu, MF_STRING, ID_PREFLIGHT, "本地灾难备份")
            self._user32.AppendMenuW(menu, MF_STRING, ID_UPDATE, "检查更新")
            self._user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "退出 Codex Sync")
            point = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            self._user32.SetForegroundWindow(hwnd)
            command = self._user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
        finally:
            self._user32.DestroyMenu(menu)
        if command == ID_SHOW:
            self.restore_window()
        elif command == ID_EXIT:
            self.exit_app()
        elif command == ID_SYNC:
            self._run_action_async("sync-now", "立即同步一次")
        elif command == ID_FULL_BACKUP:
            self._run_action_async("full-backup-now", "立即完整备份")
        elif command == ID_PREFLIGHT:
            self._run_action_async("preflight-backup", "本地灾难备份")
        elif command == ID_UPDATE:
            self._run_action_async("app-update-check", "检查更新")

    def _run_action_async(self, action: str, title: str) -> None:
        def worker() -> None:
            try:
                result = self.local.runtime.run_action(action, {})
                self.notify(title, self._action_summary(action, result))
            except Exception as exc:  # noqa: BLE001 - surface failures via a balloon
                self.local.runtime.log("error", "tray action failed", {"action": action, "error": str(exc)})
                self.notify(title, f"失败：{exc}")

        self.notify(title, "正在执行…")
        threading.Thread(target=worker, name=f"CodexSyncTray-{action}", daemon=True).start()

    @staticmethod
    def _action_summary(action: str, result: Any) -> str:
        data = result if isinstance(result, dict) else {}
        if action == "app-update-check":
            if data.get("error"):
                return f"检查失败：{data.get('error')}"
            if data.get("update_available"):
                return f"发现新版本 {data.get('latest_version') or ''}（当前 {data.get('current_version') or ''}）".strip()
            return f"已是最新版本（{data.get('current_version') or ''}）"
        if action == "sync-now":
            return "同步已完成"
        if action == "full-backup-now":
            return "完整备份已完成"
        if action == "preflight-backup":
            return "本地灾难备份已完成"
        return "已完成"


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


CODEX_HOMES_CACHE_SECONDS = 300
_codex_homes_cache: dict[str, Any] = {"data": None, "at": 0.0}
_codex_homes_lock = threading.Lock()


def _peek_codex_homes() -> dict[str, Any] | None:
    """只读 codex homes 探测缓存；未命中/过期返回 None（不触发扫描）。"""
    with _codex_homes_lock:
        data = _codex_homes_cache["data"]
        at = _codex_homes_cache["at"]
    if data is not None and time.monotonic() - at < CODEX_HOMES_CACHE_SECONDS:
        return _clone_dict(data)
    return None


def _refresh_codex_homes() -> dict[str, Any]:
    """主动探测全部本机环境（含 WSL，较慢）并写入共享缓存。供预热 / 显式 probe 调用。"""
    result = list_codex_homes()
    with _codex_homes_lock:
        _codex_homes_cache["data"] = _clone_dict(result)
        _codex_homes_cache["at"] = time.monotonic()
    return result


def invalidate_codex_homes_cache() -> None:
    with _codex_homes_lock:
        _codex_homes_cache["data"] = None
        _codex_homes_cache["at"] = 0.0


def _overview_codex_homes() -> dict[str, Any]:
    """Fast homepage home inventory.

    Avoid launching each WSL distro during the overview check. Active WSL probing is
    still available on the dedicated conversation/backup pages. If a full probe
    (incl. WSL) is already cached (e.g. warmed up), reuse it so the homepage also
    shows WSL without re-scanning.
    """
    shared = _peek_codex_homes()
    if shared and shared.get("homes"):
        return {**shared, "active_wsl_scan": True, "homes_source": "shared_cache"}
    windows = list_channels(check_running=False)
    homes: list[dict[str, Any]] = [
        {
            "id": "windows",
            "kind": "windows",
            "label": "Windows",
            "ok": bool(windows.get("success")),
            "has_codex": bool(windows.get("success")),
            "current_provider": windows.get("current_provider"),
            "error": windows.get("error"),
        }
    ]
    wsl_root = app_dir() / "wsl"
    if wsl_root.exists():
        for child in sorted(wsl_root.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            try:
                has_local_state = any(child.iterdir())
            except OSError:
                has_local_state = False
            if not has_local_state:
                continue
            distro = child.name
            homes.append(
                {
                    "id": f"wsl:{distro}",
                    "kind": "wsl",
                    "distro": distro,
                    "label": f"WSL · {distro}",
                    "ok": True,
                    "has_codex": True,
                    "overview_source": "local_state",
                }
            )
    return {"success": True, "homes": homes, "active_wsl_scan": False}


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


def _clone_dict(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _collect_sync_health_locals(config: AppConfig) -> dict[str, Any]:
    """并行收集 sync-health 的本地数据。

    windows_task_status(schtasks)、_overview_codex_homes(sqlite)、_project_state(git)、
    project_auto_backup_status 彼此独立且较慢，放进线程池并发执行；outbox/hooks/snapshot 很快，串行即可。
    每个慢调用独立降级（_safe），互不影响整体。
    """
    def _safe(fn: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - health panel should degrade gracefully
            return fallback

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_task = executor.submit(_safe, windows_task_status, {})
        f_homes = executor.submit(_safe, _overview_codex_homes, {"success": False, "homes": []})
        f_git = executor.submit(_safe, _project_state, {})
        f_project = executor.submit(_safe, lambda: project_auto_backup_status(Path.cwd(), config), {})
        task = f_task.result()
        homes_result = f_homes.result()
        git = f_git.result()
        project_auto = f_project.result()
    outbox = outbox_count()
    hooks = hook_status()
    return {
        "task": task,
        "auto_scan": _auto_scan_summary(config, task),
        "outbox": outbox,
        "hooks": hooks,
        "git": git,
        "project_auto": project_auto,
        "snapshot": _snapshot_resume_status(outbox),
        "homes_result": homes_result,
    }


def _decorate_sync_health(config: AppConfig, health: dict[str, Any], _locals: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    checked_at = utc_now()
    data = _locals if _locals is not None else _collect_sync_health_locals(config)
    task = data["task"]
    auto_scan = data["auto_scan"]
    outbox = data["outbox"]
    hooks = data["hooks"]
    git = data["git"]
    project_auto = data["project_auto"]
    snapshot = data["snapshot"]
    homes_result = data["homes_result"]

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
            status, tone, detail = "未参与", "", "该发行版没有 ~/.codex，已从备份告警中忽略。"
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
    health["compute_ms"] = int((time.perf_counter() - started) * 1000)
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


def _choose_open_file(*, title: str) -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"success": False, "error": f"无法打开文件选择器：{exc}"}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(title=title, filetypes=[("JSON / Key", "*.json *.key"), ("All files", "*.*")])
    finally:
        root.destroy()
    if not selected:
        return {"success": False, "cancelled": True, "error": "未选择文件"}
    return {"success": True, "path": selected}


def _choose_save_file(*, title: str, default_name: str) -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"success": False, "error": f"无法打开文件保存器：{exc}"}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.asksaveasfilename(
            title=title,
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
    finally:
        root.destroy()
    if not selected:
        return {"success": False, "cancelled": True, "error": "未选择保存位置"}
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


def _channels_for_home(home: dict[str, Any], *, check_running: bool = True) -> dict[str, Any]:
    home_id = str(home.get("id") or "windows")
    if home_id.startswith("wsl:"):
        result = list_wsl_channels(home_id.split(":", 1)[1])
        result.setdefault("merged", {"total": 0, "by_target": {}, "origin_breakdown": {}})
        result.setdefault("codex_running", False)
        result.setdefault("codex_running_known", False)
        result["write_supported"] = True
    else:
        result = list_channels(check_running=check_running)
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


def _usable_homes_from_result(homes_result: dict[str, Any]) -> list[dict[str, Any]]:
    homes = homes_result.get("homes", []) or []
    usable = [home for home in homes if isinstance(home, dict) and home.get("ok") and home.get("has_codex")]
    return usable or [{"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True}]


def _fast_conversation_homes() -> dict[str, Any]:
    windows = list_channels(check_running=False)
    return {
        "success": True,
        "homes": [
            {
                "id": "windows",
                "kind": "windows",
                "label": "Windows",
                "ok": bool(windows.get("success")),
                "has_codex": bool(windows.get("success")),
                "current_provider": windows.get("current_provider"),
                "error": windows.get("error"),
            }
        ],
        "active_wsl_scan": False,
        "wsl_scan_skipped": True,
    }


def _conversation_workspace_for_home(home: dict[str, Any]) -> dict[str, Any]:
    conversations = _list_conversations_for_home(home, search="", cwd="", provider="", include_archived=False)
    channels = _channels_for_home(home, check_running=False)
    return {
        "home": home,
        "conversations": conversations,
        "channels": channels,
    }


def _scan_conversation_workspace(*, probe_homes: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    if probe_homes:
        homes_result = _refresh_codex_homes()
    else:
        homes_result = _peek_codex_homes() or _fast_conversation_homes()
    usable_homes = _usable_homes_from_result(homes_result)
    per_home: dict[str, dict[str, Any]] = {}
    for home in usable_homes:
        home_id = str(home.get("id") or "windows")
        per_home[home_id] = _conversation_workspace_for_home(home)
    return {
        "success": bool(homes_result.get("success", True)) or bool(per_home),
        "homes_result": homes_result,
        "homes": homes_result.get("homes", []) or usable_homes,
        "usable_homes": usable_homes,
        "per_home": per_home,
        "wsl_error": homes_result.get("wsl_error"),
        "probe_homes": probe_homes,
        "scan_ms": int((time.perf_counter() - started) * 1000),
    }


def _conversation_workspace_home(snapshot: dict[str, Any], source_home: str) -> dict[str, Any] | None:
    per_home = snapshot.get("per_home") if isinstance(snapshot.get("per_home"), dict) else {}
    if source_home in per_home and isinstance(per_home[source_home], dict):
        return per_home[source_home]
    homes = snapshot.get("homes") if isinstance(snapshot.get("homes"), list) else []
    home = next((item for item in homes if isinstance(item, dict) and item.get("id") == source_home), None)
    if home is not None:
        error = home.get("error") or "该本机环境不可用或未检测到 Codex。"
        return {
            "home": home,
            "conversations": {"success": False, "source_home": source_home, "error": error, "conversations": [], "count": 0, "cwds": [], "providers": []},
            "channels": {"success": False, "source_home": source_home, "error": error, "channels": [], "write_supported": False},
        }
    return None


def _workspace_conversations_for_scope(snapshot: dict[str, Any], source_home: str) -> dict[str, Any]:
    per_home = snapshot.get("per_home") if isinstance(snapshot.get("per_home"), dict) else {}
    if source_home == "all":
        conversations: list[dict[str, Any]] = []
        cwds: set[str] = set()
        providers: set[str] = set()
        errors: list[dict[str, Any]] = []
        success_count = 0
        for entry in per_home.values():
            if not isinstance(entry, dict):
                continue
            home = entry.get("home") if isinstance(entry.get("home"), dict) else {}
            result = entry.get("conversations") if isinstance(entry.get("conversations"), dict) else {}
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
            "homes": snapshot.get("usable_homes", []) or [],
            "conversations": conversations[:2000],
            "count": len(conversations[:2000]),
            "cwds": sorted(cwds),
            "providers": sorted(providers),
            "errors": errors,
            "error": "; ".join(str(item.get("error")) for item in errors) if errors and not conversations else None,
        }
    entry = _conversation_workspace_home(snapshot, source_home)
    if entry is None:
        home = {"id": "windows", "kind": "windows", "label": "Windows"} if not source_home.startswith("wsl:") else {
            "id": source_home,
            "kind": "wsl",
            "distro": source_home.split(":", 1)[1],
            "label": f"WSL {source_home.split(':', 1)[1]}",
        }
        return _list_conversations_for_home(home, search="", cwd="", provider="", include_archived=False)
    result = _clone_dict(entry.get("conversations") if isinstance(entry.get("conversations"), dict) else {})
    result["source_home"] = source_home
    return result


def _workspace_channels_for_scope(snapshot: dict[str, Any], source_home: str) -> dict[str, Any]:
    per_home = snapshot.get("per_home") if isinstance(snapshot.get("per_home"), dict) else {}
    if source_home == "all":
        groups = [
            entry.get("channels")
            for entry in per_home.values()
            if isinstance(entry, dict) and isinstance(entry.get("channels"), dict)
        ]
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
    entry = _conversation_workspace_home(snapshot, source_home)
    if entry is None:
        home = {"id": "windows", "kind": "windows", "label": "Windows"} if not source_home.startswith("wsl:") else {
            "id": source_home,
            "kind": "wsl",
            "distro": source_home.split(":", 1)[1],
            "label": f"WSL {source_home.split(':', 1)[1]}",
        }
        result = _channels_for_home(home)
    else:
        result = _clone_dict(entry.get("channels") if isinstance(entry.get("channels"), dict) else {})
    result["source_home"] = source_home
    return result


def _conversation_workspace_result(snapshot: dict[str, Any], source_home: str) -> dict[str, Any]:
    source_home = source_home or "all"
    conversations = _workspace_conversations_for_scope(snapshot, source_home)
    channels = _workspace_channels_for_scope(snapshot, source_home)
    return {
        "success": bool(snapshot.get("success")) or bool(conversations.get("success")) or bool(channels.get("success")),
        "source_home": source_home,
        "homes": snapshot.get("homes", []) or [],
        "usable_homes": snapshot.get("usable_homes", []) or [],
        "conversations": conversations,
        "channels": channels,
        "homes_result": snapshot.get("homes_result", {"success": False, "homes": []}),
        "scan_ms": snapshot.get("scan_ms"),
        "wsl_error": snapshot.get("wsl_error"),
    }


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
        self.sync_health_cache: dict[str, Any] | None = None
        self.sync_health_cache_at = 0.0
        self.conversation_workspace_cache: dict[str, Any] | None = None
        self.conversation_workspace_cache_at = 0.0
        self.warmup_stop = threading.Event()
        self.warmup_thread: threading.Thread | None = None

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
            "full_backup_auto_upload",
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
                    "full_backup_auto_upload",
                    "project_auto_backup_on_codex_stop",
                ):
                    value = bool(value)
                if key == "desktop_close_behavior" and value not in {"ask", "minimize_to_tray", "exit"}:
                    value = "ask"
                setattr(cfg, key, value)
        if str(payload.get("full_backup_encryption_passphrase") or "").strip():
            # 用户设置了加密密码即视为「要加密」：自动开启客户端加密、关闭明文上传
            cfg.full_backup_encryption_enabled = True
            cfg.full_backup_allow_plaintext_upload = False
        path = save_config(cfg)
        self.invalidate_sync_health_cache()
        self.log("ok", "config saved", {"path": str(path)})
        return {"saved": str(path), "config": cfg.to_public_dict()}

    def invalidate_sync_health_cache(self) -> None:
        with self.lock:
            self.sync_health_cache = None
            self.sync_health_cache_at = 0.0

    def invalidate_conversation_workspace_cache(self) -> None:
        with self.lock:
            self.conversation_workspace_cache = None
            self.conversation_workspace_cache_at = 0.0

    def cached_sync_health(self, cfg: AppConfig, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            cached = self.sync_health_cache
            cached_at = self.sync_health_cache_at
        if not force and cached is not None and now - cached_at < SYNC_HEALTH_CACHE_SECONDS:
            result = _clone_dict(cached)
            result["cached"] = True
            result["cache_age_seconds"] = int(now - cached_at)
            return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            health_future = executor.submit(summarize_sync_health, cfg)
            locals_future = executor.submit(_collect_sync_health_locals, cfg)
            health = health_future.result()
            health_locals = locals_future.result()
        result = _decorate_sync_health(cfg, health, _locals=health_locals)
        result["cached"] = False
        result["cache_ttl_seconds"] = SYNC_HEALTH_CACHE_SECONDS
        with self.lock:
            self.sync_health_cache = _clone_dict(result)
            self.sync_health_cache_at = time.monotonic()
        return result

    def cached_conversation_workspace(self, *, source_home: str = "all", force: bool = False, probe_homes: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            cached = self.conversation_workspace_cache
            cached_at = self.conversation_workspace_cache_at
        if not force and cached is not None and now - cached_at < CONVERSATION_WORKSPACE_CACHE_SECONDS:
            result = _conversation_workspace_result(_clone_dict(cached), source_home)
            result["cached"] = True
            result["cache_age_seconds"] = int(now - cached_at)
            return result
        snapshot = _scan_conversation_workspace(probe_homes=probe_homes)
        with self.lock:
            self.conversation_workspace_cache = _clone_dict(snapshot)
            self.conversation_workspace_cache_at = time.monotonic()
        result = _conversation_workspace_result(snapshot, source_home)
        result["cached"] = False
        result["cache_ttl_seconds"] = CONVERSATION_WORKSPACE_CACHE_SECONDS
        return result

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
                result["full_backup_scan"] = scan_full_backup_changes(cfg, create_package=True, notify_dirty=True, check_remote=True, upload=True)
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
        elif name == "export-encryption-key":
            target = str(payload.get("output_path") or "").strip()
            chosen = {"success": True, "path": target} if target else _choose_save_file(title="导出完整备份加密密钥", default_name="codex-sync-full-backup-key.json")
            result = export_encryption_key(cfg, chosen["path"]) if chosen.get("success") else chosen
        elif name == "import-encryption-key":
            source = str(payload.get("source_path") or "").strip()
            chosen = {"success": True, "path": source} if source else _choose_open_file(title="选择要导入的完整备份密钥文件")
            result = import_encryption_key(cfg, chosen["path"]) if chosen.get("success") else chosen
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
            result = self.cached_sync_health(cfg, force=bool(payload.get("force", False)))
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
        elif name == "download-full-backup":
            result = download_full_backup(cfg, str(payload.get("backup_id") or ""))
        elif name == "restore-full-backup":
            result = restore_full_backup(
                cfg,
                str(payload.get("archive") or ""),
                confirm_backup_id=str(payload.get("confirm_backup_id") or ""),
                restore_config=bool(payload.get("restore_config", False)),
            )
        elif name == "restore-latest-full-backup":
            result = restore_latest_full_backup(
                cfg,
                branch_id=str(payload.get("branch_id") or "").strip() or None,
                device_id=str(payload.get("device_id") or "").strip() or None,
                restore_config=bool(payload.get("restore_config", False)),
            )
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
                result = list_channels(check_running=False)
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
        elif name == "conversation-workspace":
            result = self.cached_conversation_workspace(
                source_home=str(payload.get("source_home") or "all"),
                force=bool(payload.get("force", False)),
                probe_homes=bool(payload.get("probe_homes", False)),
            )
        elif name == "list-channels":
            workspace = self.cached_conversation_workspace(source_home=str(payload.get("source_home") or "windows"), force=bool(payload.get("force", False)))
            result = workspace.get("channels") if isinstance(workspace.get("channels"), dict) else _channels_for_scope(str(payload.get("source_home") or "windows"))
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
            source_home = str(payload.get("source_home") or "windows")
            search = str(payload.get("search", ""))
            cwd = str(payload.get("cwd", ""))
            provider = str(payload.get("provider", ""))
            include_archived = bool(payload.get("include_archived", False))
            if include_archived or search or cwd or provider:
                result = _list_conversations_for_scope(
                    source_home,
                    search=search,
                    cwd=cwd,
                    provider=provider,
                    include_archived=include_archived,
                )
            else:
                workspace = self.cached_conversation_workspace(source_home=source_home, force=bool(payload.get("force", False)))
                result = workspace.get("conversations") if isinstance(workspace.get("conversations"), dict) else _list_conversations_for_scope(
                    source_home,
                    search="",
                    cwd="",
                    provider="",
                    include_archived=False,
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
        if name in {
            "sync-now",
            "full-backup-now",
            "restore-snapshot",
            "restore-channels",
            "merge-channels",
            "restore-threads",
            "merge-threads",
            "project-auto-backup-queue",
            "project-auto-backup-process",
            "wsl-full-backup",
        }:
            self.invalidate_sync_health_cache()
        if name in {
            "restore-channels",
            "merge-channels",
            "restore-threads",
            "merge-threads",
            "import-conversations",
            "wsl-restore-full-backup",
            "wsl-restore-latest",
            "wsl-pull",
            "restore-full-backup",
            "restore-latest-full-backup",
        }:
            self.invalidate_conversation_workspace_cache()
            invalidate_codex_homes_cache()
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

    def start_warmup(self) -> None:
        """启动后台预热线程：周期性 force 刷新 sync-health 与对话工作区缓存，
        让前端打开页面时几乎总是命中热缓存（cached=True），避免撞上冷扫描。"""
        if self.warmup_thread and self.warmup_thread.is_alive():
            return
        self.warmup_stop.clear()
        self.warmup_thread = threading.Thread(target=self._warmup_loop, name="CodexSyncWarmup", daemon=True)
        self.warmup_thread.start()

    def stop_warmup(self) -> None:
        self.warmup_stop.set()
        if self.warmup_thread and self.warmup_thread.is_alive():
            self.warmup_thread.join(timeout=2)

    def _warmup_loop(self) -> None:
        # 启动后稍等：避开与首屏请求争抢，并让 start→stop 的快速场景（如测试）能及时取消首轮。
        if self.warmup_stop.wait(WARMUP_INITIAL_DELAY_SECONDS):
            return
        while not self.warmup_stop.is_set():
            try:
                cfg = load_config()
                # force=True 才能真正保持缓存热：不 force 会命中未过期缓存而变成 no-op。
                # 先 probe_homes 探测全部环境（含 WSL）填共享缓存，再刷新两视图，使首页与对话与渠道都命中含 WSL 的列表。
                self.cached_conversation_workspace(source_home="all", force=True, probe_homes=True)
                self.cached_sync_health(cfg, force=True)
            except Exception as exc:  # noqa: BLE001 - 预热失败不应中断循环
                self.log("warmup", f"warmup error: {exc}")
            if self.warmup_stop.wait(WARMUP_INTERVAL_SECONDS):
                break


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
        self.runtime.start_warmup()
        self.runtime.start_daemon()

    def stop(self) -> None:
        self.runtime.stop_daemon()
        self.runtime.stop_warmup()
        if self.thread and self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()

    def serve_forever(self) -> None:
        self.runtime.start_warmup()
        self.runtime.start_daemon()
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.runtime.stop_warmup()
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
    notified = {"done": False}

    def perform_exit() -> None:
        """Allow the native close to proceed; teardown + hard exit happen upstream."""
        if tray is not None:
            tray.exit_requested = True
        local.runtime.log("ok", "desktop window exit confirmed")
        return None

    def perform_minimize() -> bool | None:
        # Never strand the window: if the tray icon is not usable, exit instead.
        if not (tray and tray.available):
            local.runtime.log("warn", "tray icon unavailable; exiting instead of minimizing to avoid a lost window")
            return perform_exit()
        errors: list[str] = []
        try:
            window.hide()
        except Exception as exc:  # noqa: BLE001 - close prompt should not crash the app
            errors.append(str(exc))
        native_hidden = _hide_desktop_window()
        if errors or not native_hidden:
            local.runtime.log("warn", "window hide fallback status", {"native_hidden": native_hidden, "errors": errors})
        if not notified["done"]:
            notified["done"] = True
            tray.notify(
                "Codex Sync 仍在后台运行",
                "已最小化到通知区域。双击托盘图标可重新打开；若未看到，请点任务栏右下角的 ∧ 展开隐藏图标。",
            )
        return False  # prevent the window from actually closing

    def on_closing() -> bool | None:
        if tray and tray.exit_requested:
            local.runtime.log("ok", "desktop exit requested from tray")
            return None
        cfg = load_config()
        behavior = cfg.desktop_close_behavior
        if behavior in {"minimize_to_tray", "exit"}:
            choice = behavior
            local.runtime.log("run", "desktop close (saved default)", {"choice": choice})
        else:
            choice, remember = _show_close_dialog()
            local.runtime.log("run", "desktop close prompt", {"choice": choice, "remember": remember})
            if choice == "cancel":
                return False
            if remember and choice in {"minimize_to_tray", "exit"}:
                cfg.desktop_close_behavior = choice
                save_config(cfg)
        if choice == "minimize_to_tray":
            return perform_minimize()
        return perform_exit()

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
        os._exit(0)
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
    # The window has closed (or WebView never started). Force a hard exit so a
    # lingering WebView2 / daemon thread can never keep the process (and the
    # single-instance mutex) alive after an intended shutdown.
    os._exit(0)


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
