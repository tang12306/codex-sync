"""本机安装状态与开机自启。

安装、升级、卸载都由 Inno Setup 安装程序完成（installer/CodexSync.iss）。这里只读取安装程序写入的
卸载信息，管理开机自启（HKCU Run 键，和安装程序的“开机时自动启动”选项写的是同一个值），
以及在卸载前撤掉指向本程序的 Codex hooks 和定时任务。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import __version__


APP_NAME = "Codex Sync"
EXE_NAME = "CodexSync.exe"
# 必须和 installer/CodexSync.iss 里的 AppIdGuid 一致（测试会校验）
APP_ID = "{680F4FE3-3511-457B-981C-BE2D98CEDDB4}"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
STARTUP_VALUE = "CodexSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _is_windows() -> bool:
    return os.name == "nt"


def _current_executable() -> Path:
    return Path(sys.executable).resolve()


def _is_frozen_exe() -> bool:
    exe = _current_executable()
    return bool(getattr(sys, "frozen", False)) and exe.suffix.lower() == ".exe"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return str(left).lower() == str(right).lower()


def _read_uninstall_values() -> dict[str, str]:
    """读取安装程序写的卸载信息（HKCU，因为安装程序默认按当前用户安装）。"""
    if not _is_windows():
        return {}
    try:
        import winreg

        values: dict[str, str] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            for name in ("Inno Setup: App Path", "InstallLocation", "DisplayVersion", "UninstallString"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                text = str(value).strip()
                if text:
                    values[name] = text
        return values
    except OSError:
        return {}


def _install_dir(values: dict[str, str]) -> Path | None:
    raw = values.get("Inno Setup: App Path") or values.get("InstallLocation")
    return Path(raw.rstrip("\\/")) if raw else None


def _uninstaller(values: dict[str, str]) -> Path | None:
    raw = values.get("UninstallString", "").strip().strip('"')
    if raw and Path(raw).exists():
        return Path(raw)
    return None


def install_dir() -> Path | None:
    return _install_dir(_read_uninstall_values())


def installed_executable() -> Path | None:
    root = install_dir()
    return root / EXE_NAME if root else None


def uninstaller_path() -> Path | None:
    """安装目录里的卸载程序；注册表信息丢失时，退回到当前 exe 旁边找。"""
    found = _uninstaller(_read_uninstall_values())
    if found:
        return found
    if _is_frozen_exe():
        candidates = sorted(_current_executable().parent.glob("unins*.exe"))
        if candidates:
            return candidates[0]
    return None


def _startup_command(target: Path) -> str:
    return f'"{target}"'


def startup_status() -> dict[str, Any]:
    if not _is_windows():
        return {"supported": False, "enabled": False, "error": "Startup is only supported on Windows."}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            try:
                command, value_type = winreg.QueryValueEx(key, STARTUP_VALUE)
            except FileNotFoundError:
                return {"supported": True, "enabled": False, "value_name": STARTUP_VALUE}
        target = str(command).strip().strip('"')
        return {
            "supported": True,
            "enabled": True,
            "value_name": STARTUP_VALUE,
            "command": command,
            "target": target,
            "target_exists": Path(target).exists(),
            "value_type": value_type,
        }
    except Exception as exc:  # noqa: BLE001 - status must not break the desktop UI
        return {"supported": True, "enabled": False, "value_name": STARTUP_VALUE, "error": str(exc)}


def set_startup_enabled(enabled: bool) -> dict[str, Any]:
    if not _is_windows():
        return {"success": False, "supported": False, "error": "Startup is only supported on Windows."}
    target = installed_executable()
    if enabled and (target is None or not target.exists()):
        return {
            "success": False,
            "supported": True,
            "needs_install": True,
            "error": "还没有用安装程序安装 Codex Sync。请先运行 CodexSyncSetup 安装，再开启开机自启。",
        }
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, _startup_command(target))
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_VALUE)
                except FileNotFoundError:
                    pass
        return {"success": True, "enabled": enabled, "startup": startup_status()}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "supported": True, "enabled": enabled, "error": str(exc)}


def app_install_status() -> dict[str, Any]:
    values = _read_uninstall_values()
    current = _current_executable()
    root = _install_dir(values)
    installed = root / EXE_NAME if root else None
    installed_available = bool(installed and installed.exists())
    uninstaller = _uninstaller(values)
    return {
        "supported": _is_windows(),
        "packaged": _is_frozen_exe(),
        "installed": installed_available and _same_path(current, installed),
        "installed_available": installed_available,
        "installed_version": values.get("DisplayVersion"),
        "install_dir": str(root) if root else None,
        "installed_executable": str(installed) if installed else None,
        "uninstaller": str(uninstaller) if uninstaller else None,
        "current_executable": str(current),
        "startup": startup_status(),
        "version": __version__,
    }


def cleanup_before_uninstall() -> dict[str, Any]:
    """卸载程序在删除文件前调用（CodexSync.exe uninstall-cleanup）。

    只撤掉命令里包含当前 exe 路径的 Codex hooks 和 Windows 定时任务，源码模式装的不动；
    否则卸载后它们会一直去调用一个已经不存在的程序。
    """
    from .hooks import uninstall_hooks
    from .windows_task import uninstall_windows_task, windows_task_status

    exe = str(_current_executable())
    result: dict[str, Any] = {"success": True, "executable": exe}
    try:
        result["hooks"] = uninstall_hooks(match=exe)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort, never block uninstall
        result["hooks"] = {"success": False, "error": str(exc)}
    try:
        task = windows_task_status()
        command = str(task.get("task_to_run") or "")
        if task.get("installed") and exe.lower() in command.lower():
            result["task"] = uninstall_windows_task()
        else:
            result["task"] = {"skipped": True}
    except Exception as exc:  # noqa: BLE001
        result["task"] = {"success": False, "error": str(exc)}
    return result
