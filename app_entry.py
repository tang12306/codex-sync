"""Codex Sync 打包 exe 的入口（PyInstaller 打包用）。

安装、升级、卸载由 Inno Setup 安装程序负责（installer/CodexSync.iss），这里不再自己安装自己。
- 无参数 → 打开桌面端；
- `--uninstall` → 兼容旧版开始菜单里的“卸载 Codex Sync”快捷方式，转交给安装目录里的卸载程序；
- 其它参数 → 等价于 `python -m codex_sync <args>`（CLI，hooks / 定时任务都通过这种方式调用）。

打包成 --noconsole(windowed) 时，sys.stdout/stderr 可能为 None；先重定向到 devnull，
避免内部 print 触发 AttributeError。
"""
import os
import subprocess
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from codex_sync.app_install import APP_NAME, uninstaller_path
from codex_sync.cli import main

MB_OK = 0x00000000
MB_ICONINFO = 0x00000040


def _message_box(title: str, text: str, flags: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


def _run_uninstaller() -> None:
    uninstaller = uninstaller_path()
    if uninstaller:
        subprocess.Popen([str(uninstaller)], cwd=str(uninstaller.parent), close_fds=True)
        return
    _message_box(
        f"卸载 {APP_NAME}",
        f"请在 Windows“设置 → 应用 → 已安装的应用”中找到 {APP_NAME} 并卸载。",
        MB_OK | MB_ICONINFO,
    )


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0].lstrip("-/").lower() == "uninstall":
        _run_uninstaller()
        sys.exit(0)
    main(argv or ["desktop"])
