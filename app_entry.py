"""Codex Sync 独立 exe 入口（PyInstaller 打包用）。

- 双击运行（无命令行参数）→ 若还在解压目录，先安装到本机固定目录并从安装位置重启；已安装时打开桌面端。
- 带参数运行 → 等价于 `python -m codex_sync <args>`（CLI）。

打包成 --noconsole(windowed) 时，sys.stdout/stderr 可能为 None；这里先重定向到 devnull，
避免内部 print 触发 AttributeError。
"""
import os
import subprocess
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from codex_sync.app_install import app_install_status, install_app
from codex_sync.cli import main


def _install_and_relaunch_when_portable() -> bool:
    if len(sys.argv) > 1 or os.environ.get("CODEX_SYNC_SKIP_SELF_INSTALL") == "1":
        return False
    try:
        status = app_install_status()
        if not status.get("installable") or status.get("installed"):
            return False
        result = install_app(create_shortcuts=True, enable_startup=False)
        target = str(result.get("target") or status.get("installed_executable") or "")
        if not result.get("success") or not result.get("copied") or not target or not os.path.exists(target):
            return False
        subprocess.Popen([target], cwd=os.path.dirname(target) or None, close_fds=True)
        return True
    except Exception:
        return False

if __name__ == "__main__":
    if _install_and_relaunch_when_portable():
        sys.exit(0)
    main(["desktop"] if len(sys.argv) <= 1 else sys.argv[1:])
