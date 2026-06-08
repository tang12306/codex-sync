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


def _message_box(title: str, text: str, flags: int) -> int:
    if os.name != "nt":
        return 6
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 6


def _install_and_relaunch_when_portable() -> bool:
    if len(sys.argv) > 1 or os.environ.get("CODEX_SYNC_SKIP_SELF_INSTALL") == "1":
        return False
    try:
        status = app_install_status()
        if not status.get("installable") or status.get("installed"):
            return False
        target = str(status.get("installed_executable") or "")
        current = str(status.get("current_executable") or sys.executable)
        existing = bool(status.get("installed_available"))
        prompt = (
            "检测到你正在从安装包或临时位置启动 Codex Sync。\n\n"
            f"将安装到：\n{target}\n\n"
            "安装会创建桌面和开始菜单快捷方式，并在安装完成后启动应用。\n\n"
            "选择“是”开始安装；选择“否”退出安装。"
        )
        if existing:
            prompt = (
                "检测到 Codex Sync 已安装，是否覆盖更新本机安装？\n\n"
                f"当前启动文件：\n{current}\n\n"
                f"安装位置：\n{target}\n\n"
                "安装器会先关闭正在运行的旧版本，再启动新版本。"
            )
        choice = _message_box("Codex Sync 安装", prompt, 0x00000004 | 0x00000040)
        if choice != 6:
            return True
        result = install_app(create_shortcuts=True, enable_startup=False)
        target = str(result.get("target") or target)
        if not result.get("success") or not result.get("copied") or not target or not os.path.exists(target):
            _message_box("Codex Sync 安装失败", "安装未完成，请关闭正在运行的 Codex Sync 后重试。", 0x00000000 | 0x00000010)
            return True
        _message_box("Codex Sync 安装完成", "安装完成。现在将启动 Codex Sync。", 0x00000000 | 0x00000040)
        subprocess.Popen([target], cwd=os.path.dirname(target) or None, close_fds=True)
        return True
    except Exception as exc:
        _message_box("Codex Sync 安装失败", f"安装失败：{exc}", 0x00000000 | 0x00000010)
        return True

if __name__ == "__main__":
    if _install_and_relaunch_when_portable():
        sys.exit(0)
    main(["desktop"] if len(sys.argv) <= 1 else sys.argv[1:])
