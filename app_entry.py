"""Codex Sync 独立 exe 入口（PyInstaller 打包用）。

- 双击运行（无参数）→ 还在安装包/临时位置时，启动安装向导（tkinter，多步：欢迎→选路径→安装中→完成）；
  已安装位置启动时，直接打开桌面端。
- `--uninstall` → 卸载流程（停进程、删快捷方式/注册表、清理安装目录）。
- 带其它参数 → 等价于 `python -m codex_sync <args>`（CLI）。

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

from codex_sync.app_install import APP_NAME, app_install_status, install_app, uninstall_app
from codex_sync.cli import main

# Win32 MessageBox 标志
MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONERROR = 0x00000010
MB_ICONWARNING = 0x00000030
MB_ICONINFO = 0x00000040
IDYES = 6


def _message_box(title: str, text: str, flags: int) -> int:
    if os.name != "nt":
        return IDYES
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return IDYES


def _legacy_self_install(status: dict) -> bool:
    """tkinter 向导不可用时的降级安装流（一串 MessageBox + 直接 install_app）。"""
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
    if _message_box(f"{APP_NAME} 安装", prompt, MB_YESNO | MB_ICONINFO) != IDYES:
        return True
    result = install_app(create_shortcuts=True, enable_startup=False)
    target = str(result.get("target") or target)
    if not result.get("success") or not target or not os.path.exists(target):
        _message_box(
            f"{APP_NAME} 安装失败",
            str(result.get("error") or "安装未完成，请关闭正在运行的 Codex Sync 后重试。"),
            MB_OK | MB_ICONERROR,
        )
        return True
    _message_box(f"{APP_NAME} 安装完成", "安装完成。现在将启动 Codex Sync。", MB_OK | MB_ICONINFO)
    subprocess.Popen([target], cwd=os.path.dirname(target) or None, close_fds=True)
    return True


def _run_install_flow(status: dict) -> bool:
    """启动安装向导（带安装器单实例锁）。返回 True 表示安装流程已接管，主进程应退出。"""
    guard = None
    try:
        from codex_sync.web_desktop import SingleInstance

        guard = SingleInstance("Local\\CodexSyncInstaller")
        if not guard.acquire():
            _message_box(
                f"{APP_NAME} 安装",
                "检测到另一个 Codex Sync 安装程序正在运行。\n\n请先完成或关闭它，再重新运行本安装包。",
                MB_OK | MB_ICONWARNING,
            )
            return True
    except Exception:
        guard = None  # 单实例不可用时不阻塞安装

    try:
        from codex_sync import installer

        res = installer.run()
        if not res.get("available"):
            return _legacy_self_install(status)
        if res.get("completed") and res.get("launch"):
            target = res.get("target") or status.get("installed_executable")
            if target and os.path.exists(target):
                subprocess.Popen([target], cwd=os.path.dirname(target) or None, close_fds=True)
        return True
    except Exception as exc:  # noqa: BLE001 - degrade to legacy installer
        try:
            return _legacy_self_install(status)
        except Exception:
            _message_box(f"{APP_NAME} 安装失败", f"安装失败：{exc}", MB_OK | MB_ICONERROR)
            return True
    finally:
        if guard is not None:
            try:
                guard.release()
            except Exception:
                pass


def _maybe_install() -> bool:
    if os.environ.get("CODEX_SYNC_SKIP_SELF_INSTALL") == "1":
        return False
    try:
        status = app_install_status()
    except Exception:
        return False
    if not status.get("installable") or status.get("installed"):
        return False
    return _run_install_flow(status)


def _uninstall_flow() -> None:
    try:
        status = app_install_status()
    except Exception:
        status = {}
    confirm = _message_box(
        f"卸载 {APP_NAME}",
        f"确定要卸载 {APP_NAME} 吗？\n\n安装目录：\n{status.get('install_dir', '')}\n\n"
        "这会删除程序文件、快捷方式和开机自启项。\n"
        "（你的同步配置与备份数据 ~/.codex-sync 不会被删除）",
        MB_YESNO | MB_ICONWARNING,
    )
    if confirm != IDYES:
        return
    try:
        res = uninstall_app()
    except Exception as exc:  # noqa: BLE001
        _message_box(f"{APP_NAME} 卸载失败", f"卸载失败：{exc}", MB_OK | MB_ICONERROR)
        return
    if res.get("success"):
        _message_box(f"{APP_NAME} 卸载", "卸载完成。程序目录将在几秒内清理完毕。", MB_OK | MB_ICONINFO)
    else:
        _message_box(f"{APP_NAME} 卸载失败", str(res.get("error") or "卸载未完成。"), MB_OK | MB_ICONERROR)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0].lstrip("-/").lower() == "uninstall":
        _uninstall_flow()
        sys.exit(0)
    if not argv:
        if _maybe_install():
            sys.exit(0)
        main(["desktop"])
    else:
        main(argv)
