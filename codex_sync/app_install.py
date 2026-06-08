from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .util import run_cmd


APP_NAME = "Codex Sync"
EXE_NAME = "CodexSync.exe"
STARTUP_VALUE = "CodexSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _is_windows() -> bool:
    return os.name == "nt"


def _current_executable() -> Path:
    return Path(sys.executable).resolve()


def _local_app_data() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw).expanduser() if raw else Path.home() / "AppData" / "Local"


def install_dir() -> Path:
    return _local_app_data() / "CodexSync"


def installed_executable() -> Path:
    return install_dir() / EXE_NAME


def _is_frozen_exe() -> bool:
    exe = _current_executable()
    return bool(getattr(sys, "frozen", False)) and exe.suffix.lower() == ".exe"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return str(left).lower() == str(right).lower()


def _desktop_shortcut() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / f"{APP_NAME}.lnk"


def _start_menu_shortcut() -> Path:
    root = os.environ.get("APPDATA")
    base = Path(root).expanduser() if root else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _create_shortcut(shortcut: Path, target: Path) -> dict[str, Any]:
    if not _is_windows():
        return {"success": False, "error": "Shortcuts are only supported on Windows.", "path": str(shortcut)}
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    command = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        + _ps_quote(shortcut)
        + ");"
        + "$s.TargetPath="
        + _ps_quote(target)
        + ";"
        + "$s.WorkingDirectory="
        + _ps_quote(target.parent)
        + ";"
        + "$s.IconLocation="
        + _ps_quote(f"{target},0")
        + ";"
        + "$s.Description="
        + _ps_quote("Codex Sync desktop app")
        + ";"
        + "$s.Save()"
    )
    code, out, err = run_cmd(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], timeout=30)
    return {"success": code == 0, "code": code, "stdout": out, "stderr": err, "path": str(shortcut)}


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
    if enabled and not target.exists():
        return {
            "success": False,
            "supported": True,
            "needs_install": True,
            "error": "请先安装到本机固定目录，再开启开机自启。",
            "install_path": str(target),
        }
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                command = _startup_command(target)
                winreg.SetValueEx(key, STARTUP_VALUE, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, STARTUP_VALUE)
                except FileNotFoundError:
                    pass
        return {"success": True, "enabled": enabled, "startup": startup_status()}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "supported": True, "enabled": enabled, "error": str(exc)}


def app_install_status() -> dict[str, Any]:
    current = _current_executable()
    installed = installed_executable()
    installed_available = installed.exists()
    running_installed = installed_available and _same_path(current, installed)
    return {
        "supported": _is_windows(),
        "installable": _is_windows() and _is_frozen_exe(),
        "installed": running_installed,
        "installed_available": installed_available,
        "current_executable": str(current),
        "install_dir": str(install_dir()),
        "installed_executable": str(installed),
        "desktop_shortcut": str(_desktop_shortcut()),
        "desktop_shortcut_exists": _desktop_shortcut().exists(),
        "start_menu_shortcut": str(_start_menu_shortcut()),
        "start_menu_shortcut_exists": _start_menu_shortcut().exists(),
        "startup": startup_status(),
        "version": __version__,
    }


def install_app(create_shortcuts: bool = True, enable_startup: bool = False) -> dict[str, Any]:
    if not _is_windows():
        return {"success": False, "supported": False, "error": "Codex Sync installation is only supported on Windows."}
    if not _is_frozen_exe():
        return {
            "success": False,
            "supported": True,
            "installable": False,
            "error": "当前是源码/开发模式运行，不是打包后的 EXE，无法执行本机安装。",
            "current_executable": str(_current_executable()),
        }

    source = _current_executable()
    target = installed_executable()
    target.parent.mkdir(parents=True, exist_ok=True)
    copied = False
    if not _same_path(source, target):
        temp_target = target.with_suffix(".exe.new")
        shutil.copy2(source, temp_target)
        temp_target.replace(target)
        copied = True

    shortcuts: list[dict[str, Any]] = []
    if create_shortcuts:
        shortcuts.append(_create_shortcut(_desktop_shortcut(), target))
        shortcuts.append(_create_shortcut(_start_menu_shortcut(), target))

    startup = set_startup_enabled(True) if enable_startup else startup_status()
    warnings = [item for item in shortcuts if item.get("success") is False]
    if enable_startup and startup.get("success") is False:
        warnings.append({"error": startup.get("error"), "target": str(target)})

    return {
        "success": True,
        "installed": True,
        "copied": copied,
        "source": str(source),
        "target": str(target),
        "shortcuts": shortcuts,
        "startup": startup,
        "warnings": warnings,
        "status": app_install_status(),
    }
