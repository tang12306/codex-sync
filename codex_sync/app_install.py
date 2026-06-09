from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .util import run_cmd


APP_NAME = "Codex Sync"
EXE_NAME = "CodexSync.exe"
STARTUP_VALUE = "CodexSync"
PUBLISHER = "Codex Sync contributors"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SOFTWARE_KEY = r"Software\CodexSync"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexSync"
UNINSTALL_SHORTCUT_NAME = f"卸载 {APP_NAME}"

ProgressFn = Callable[[float, str], None]


def _is_windows() -> bool:
    return os.name == "nt"


def _current_executable() -> Path:
    return Path(sys.executable).resolve()


def _local_app_data() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw).expanduser() if raw else Path.home() / "AppData" / "Local"


def default_install_dir() -> Path:
    """开箱默认安装目录（向导预填值），不受已安装记录影响。"""
    return _local_app_data() / "CodexSync"


# ----------------------------------------------------------------------------
# 注册表：本应用安装记录 HKCU\Software\CodexSync
# ----------------------------------------------------------------------------
def _read_software_value(name: str) -> str | None:
    if not _is_windows():
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SOFTWARE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
        text = str(value).strip()
        return text or None
    except (FileNotFoundError, OSError):
        return None


def _write_software_values(values: dict[str, str]) -> None:
    if not _is_windows():
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SOFTWARE_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def _delete_software_key() -> None:
    if not _is_windows():
        return
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SOFTWARE_KEY)
    except (FileNotFoundError, OSError):
        pass


def install_dir() -> Path:
    """当前认定的安装目录：优先用注册表记录（支持自定义路径），否则默认目录。"""
    recorded = _read_software_value("InstallDir")
    if recorded:
        try:
            return Path(recorded)
        except (OSError, ValueError):
            pass
    return default_install_dir()


def installed_executable() -> Path:
    return install_dir() / EXE_NAME


def installed_version() -> str | None:
    return _read_software_value("Version")


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


def _start_menu_dir() -> Path:
    root = os.environ.get("APPDATA")
    base = Path(root).expanduser() if root else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _start_menu_shortcut() -> Path:
    return _start_menu_dir() / f"{APP_NAME}.lnk"


def _uninstall_shortcut() -> Path:
    return _start_menu_dir() / f"{UNINSTALL_SHORTCUT_NAME}.lnk"


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _create_shortcut(shortcut: Path, target: Path, arguments: str = "") -> dict[str, Any]:
    if not _is_windows():
        return {"success": False, "error": "Shortcuts are only supported on Windows.", "path": str(shortcut)}
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(shortcut) + ");",
        "$s.TargetPath=" + _ps_quote(target) + ";",
        "$s.WorkingDirectory=" + _ps_quote(target.parent) + ";",
        "$s.IconLocation=" + _ps_quote(f"{target},0") + ";",
        "$s.Description=" + _ps_quote("Codex Sync desktop app") + ";",
    ]
    if arguments:
        parts.append("$s.Arguments=" + _ps_quote(arguments) + ";")
    parts.append("$s.Save()")
    command = "".join(parts)
    code, out, err = run_cmd(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], timeout=30)
    return {"success": code == 0, "code": code, "stdout": out, "stderr": err, "path": str(shortcut)}


def _stop_processes_for_executable(target: Path) -> dict[str, Any]:
    if not _is_windows() or not target.exists():
        return {"success": True, "stopped": []}
    command = (
        "$target="
        + _ps_quote(target)
        + ";$self="
        + str(os.getpid())
        + ";$stopped=@();"
        + "Get-Process -ErrorAction SilentlyContinue | ForEach-Object {"
        + "$path=$null;try{$path=$_.Path}catch{};"
        + "if($path -and $_.Id -ne $self -and [string]::Equals($path,$target,[System.StringComparison]::OrdinalIgnoreCase)){"
        + "$stopped += $_.Id; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue"
        + "}};"
        + "$stopped -join ','"
    )
    code, out, err = run_cmd(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], timeout=30)
    stopped = [int(item) for item in out.split(",") if item.strip().isdigit()]
    if stopped:
        time.sleep(0.8)
    return {"success": code == 0, "stopped": stopped, "stderr": err}


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


def _copy_with_progress(src: Path, dst: Path, progress: ProgressFn | None, lo: float, hi: float) -> None:
    total = max(1, src.stat().st_size)
    copied = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fsrc, dst.open("wb") as fdst:
        while True:
            chunk = fsrc.read(1024 * 1024)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            if progress:
                progress(lo + (hi - lo) * min(1.0, copied / total), "正在拷贝程序文件…")
    shutil.copystat(src, dst, follow_symlinks=True)


def _register_uninstall(target: Path, install_root: Path) -> None:
    if not _is_windows():
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{target},0")
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{target}" --uninstall')
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_root))
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def _unregister_uninstall() -> None:
    if not _is_windows():
        return
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except (FileNotFoundError, OSError):
        pass


def _schedule_dir_removal(path: Path) -> bool:
    """spawn 一个游离的 cmd，等当前 exe 退出后再删除安装目录（规避“exe 删自身目录”占用）。"""
    if not _is_windows() or not path.exists():
        return False
    try:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        cmd = f'ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{path}"'
        subprocess.Popen(["cmd", "/c", cmd], creationflags=flags, close_fds=True)
        return True
    except Exception:  # noqa: BLE001 - best-effort cleanup
        return False


def app_install_status() -> dict[str, Any]:
    current = _current_executable()
    installed = installed_executable()
    installed_available = installed.exists()
    running_installed = installed_available and _same_path(current, installed)
    prev_version = installed_version()
    update_available = bool(installed_available and prev_version and prev_version != __version__)
    return {
        "supported": _is_windows(),
        "installable": _is_windows() and _is_frozen_exe(),
        "installed": running_installed,
        "installed_available": installed_available,
        "installed_version": prev_version,
        "update_available": update_available,
        "current_executable": str(current),
        "install_dir": str(install_dir()),
        "default_install_dir": str(default_install_dir()),
        "installed_executable": str(installed),
        "desktop_shortcut": str(_desktop_shortcut()),
        "desktop_shortcut_exists": _desktop_shortcut().exists(),
        "start_menu_shortcut": str(_start_menu_shortcut()),
        "start_menu_shortcut_exists": _start_menu_shortcut().exists(),
        "startup": startup_status(),
        "version": __version__,
    }


def install_app(
    create_shortcuts: bool = True,
    enable_startup: bool = False,
    target_dir: str | Path | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
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

    def _report(fraction: float, message: str) -> None:
        if progress:
            try:
                progress(max(0.0, min(1.0, fraction)), message)
            except Exception:  # noqa: BLE001 - progress UI must never break install
                pass

    _report(0.02, "准备安装…")
    source = _current_executable()
    install_root = Path(target_dir).expanduser() if target_dir else install_dir()
    target = install_root / EXE_NAME
    try:
        install_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "success": False,
            "error": f"无法创建安装目录：{exc}",
            "target": str(target),
            "needs_other_location": True,
        }

    stopped = {"success": True, "stopped": []}
    copied = False
    if not _same_path(source, target):
        _report(0.08, "正在关闭正在运行的旧版本…")
        stopped = _stop_processes_for_executable(target)
        temp_target = target.with_suffix(".exe.new")
        try:
            _copy_with_progress(source, temp_target, progress, 0.1, 0.7)
            last_error: Exception | None = None
            for _ in range(5):
                try:
                    temp_target.replace(target)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.4)
            if last_error is not None:
                temp_target.unlink(missing_ok=True)
                return {
                    "success": False,
                    "error": f"无法写入安装文件（可能被占用或权限不足）：{last_error}",
                    "target": str(target),
                    "needs_other_location": True,
                }
        except OSError as exc:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "success": False,
                "error": f"拷贝程序文件失败（可能权限不足，换个目录试试）：{exc}",
                "target": str(target),
                "needs_other_location": True,
            }
        copied = True

    _report(0.72, "记录安装信息…")
    _write_software_values({"InstallDir": str(install_root), "Version": __version__, "Executable": str(target)})

    shortcuts: list[dict[str, Any]] = []
    if create_shortcuts:
        _report(0.78, "创建快捷方式…")
        shortcuts.append(_create_shortcut(_desktop_shortcut(), target))
        shortcuts.append(_create_shortcut(_start_menu_shortcut(), target))
        shortcuts.append(_create_shortcut(_uninstall_shortcut(), target, arguments="--uninstall"))

    _report(0.9, "注册卸载信息…")
    try:
        _register_uninstall(target, install_root)
    except OSError:
        pass

    startup = set_startup_enabled(True) if enable_startup else startup_status()
    warnings = [item for item in shortcuts if item.get("success") is False]
    if enable_startup and startup.get("success") is False:
        warnings.append({"error": startup.get("error"), "target": str(target)})

    _report(1.0, "安装完成")
    return {
        "success": True,
        "installed": True,
        "copied": copied,
        "source": str(source),
        "target": str(target),
        "install_dir": str(install_root),
        "stopped_processes": stopped,
        "shortcuts": shortcuts,
        "startup": startup,
        "warnings": warnings,
        "status": app_install_status(),
    }


def uninstall_app(remove_user_data: bool = False) -> dict[str, Any]:
    if not _is_windows():
        return {"success": False, "supported": False, "error": "Uninstall is only supported on Windows."}
    install_root = install_dir()
    target = installed_executable()

    stopped = _stop_processes_for_executable(target)

    removed_shortcuts: list[str] = []
    for shortcut in (_desktop_shortcut(), _start_menu_shortcut(), _uninstall_shortcut()):
        try:
            if shortcut.exists():
                shortcut.unlink()
                removed_shortcuts.append(str(shortcut))
        except OSError:
            pass

    set_startup_enabled(False)
    _unregister_uninstall()
    _delete_software_key()

    scheduled = _schedule_dir_removal(install_root)
    return {
        "success": True,
        "uninstalled": True,
        "install_dir": str(install_root),
        "stopped_processes": stopped,
        "removed_shortcuts": removed_shortcuts,
        "directory_removal_scheduled": scheduled,
        "remove_user_data": remove_user_data,
    }
