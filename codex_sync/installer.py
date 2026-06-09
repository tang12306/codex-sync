"""Codex Sync 安装向导（tkinter 多步向导：欢迎 → 选路径 → 安装中 → 完成）。

仅在打包后的 EXE 双击安装时使用。tkinter 不可用时，``run`` 返回
``{"available": False}``，调用方（app_entry）据此降级到 MessageBox 自安装流。
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from . import __version__
from .app_install import (
    APP_NAME,
    app_install_status,
    default_install_dir,
    install_app,
)

WINDOW_W = 540
WINDOW_H = 400


def _icon_path() -> Path | None:
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "assets" / "CodexSync.ico")
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "CodexSync.ico")
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _free_space(path: str | Path) -> int | None:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except OSError:
        return None


def _fmt_bytes(value: int | None) -> str:
    if not value or value <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


class InstallerWizard:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.status = app_install_status()
        self.install_path = self.status.get("install_dir") or self.status.get("default_install_dir") or str(default_install_dir())
        if not self.status.get("installed_available"):
            # 全新安装预填开箱默认目录，而非空注册表回退值
            self.install_path = self.status.get("default_install_dir") or self.install_path
        self.result: dict[str, Any] | None = None
        self.launch = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"安装 {APP_NAME}")
        self.root.resizable(False, False)
        self._apply_icon()
        self._center()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.content = ttk.Frame(self.root, padding=20)
        self.content.pack(fill="both", expand=True)

    # ---- chrome helpers ----------------------------------------------------
    def _apply_icon(self) -> None:
        icon = _icon_path()
        if icon is not None:
            try:
                self.root.iconbitmap(str(icon))
            except Exception:  # noqa: BLE001 - icon is cosmetic
                pass

    def _center(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - WINDOW_W) // 2)
        y = max(0, (screen_h - WINDOW_H) // 3)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")

    def _clear(self) -> "Any":
        for child in self.content.winfo_children():
            child.destroy()
        return self.content

    def _title(self, parent, text: str):
        return self.ttk.Label(parent, text=text, font=("Segoe UI", 15, "bold"))

    def _on_close(self) -> None:
        # 安装进行中不允许关闭，避免半成品
        if getattr(self, "_installing", False):
            return
        self.root.destroy()

    # ---- pages -------------------------------------------------------------
    def show_welcome(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = self._clear()
        self._title(frame, f"{APP_NAME}").pack(anchor="w")
        ttk.Label(frame, text=f"版本 v{__version__}", foreground="#666").pack(anchor="w", pady=(0, 14))

        prev = self.status.get("installed_version")
        if self.status.get("update_available") and prev:
            note = f"检测到已安装 v{prev}，将更新到 v{__version__}。"
        elif self.status.get("installed_available"):
            note = f"检测到已安装 v{prev or '?'}（与当前版本相同），可重新安装/修复。"
        else:
            note = "本向导会把 Codex Sync 安装到本机，并创建桌面与开始菜单快捷方式。"
        ttk.Label(frame, text=note, wraplength=WINDOW_W - 60, justify="left").pack(anchor="w", pady=(0, 8))
        ttk.Label(
            frame,
            text="Codex Sync 用于在多台电脑之间同步 Codex 工作现场：跨设备备份/恢复对话、项目代码与渠道。",
            wraplength=WINDOW_W - 60,
            justify="left",
            foreground="#666",
        ).pack(anchor="w")

        btns = ttk.Frame(frame)
        btns.pack(side="bottom", anchor="e", pady=(20, 0), fill="x")
        ttk.Button(btns, text="取消", command=self._on_close).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="下一步", command=self.show_path).pack(side="right")

    def show_path(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = self._clear()
        self._title(frame, "选择安装位置").pack(anchor="w", pady=(0, 12))

        ttk.Label(frame, text="将安装到：").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(4, 6))
        self.path_var = tk.StringVar(value=self.install_path)
        entry = ttk.Entry(row, textvariable=self.path_var)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._browse).pack(side="left", padx=(8, 0))

        self.space_lbl = ttk.Label(frame, text="", foreground="#666")
        self.space_lbl.pack(anchor="w")
        self._update_space()
        self.path_var.trace_add("write", lambda *_: self._update_space())

        ttk.Label(
            frame,
            text="提示：默认安装到当前用户目录，无需管理员权限。",
            foreground="#666",
            wraplength=WINDOW_W - 60,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        btns = ttk.Frame(frame)
        btns.pack(side="bottom", anchor="e", pady=(20, 0), fill="x")
        ttk.Button(btns, text="取消", command=self._on_close).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="安装", command=self._start_install).pack(side="right")
        ttk.Button(btns, text="上一步", command=self.show_welcome).pack(side="right", padx=(0, 8))

    def _browse(self) -> None:
        from tkinter import filedialog

        initial = self.path_var.get() or self.install_path
        parent = initial
        while parent and not os.path.isdir(parent):
            new_parent = os.path.dirname(parent)
            if new_parent == parent:
                break
            parent = new_parent
        chosen = filedialog.askdirectory(title="选择安装位置", initialdir=parent or None, mustexist=False)
        if chosen:
            # 用户通常选的是父目录，自动追加产品名子目录
            chosen = os.path.normpath(chosen)
            if os.path.basename(chosen).lower() != "codexsync":
                chosen = os.path.join(chosen, "CodexSync")
            self.path_var.set(chosen)

    def _update_space(self) -> None:
        free = _free_space(self.path_var.get() or self.install_path)
        self.space_lbl.config(text=f"目标磁盘可用空间：{_fmt_bytes(free)}")

    def show_progress(self) -> None:
        ttk = self.ttk
        frame = self._clear()
        self._title(frame, "正在安装…").pack(anchor="w", pady=(0, 16))
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=WINDOW_W - 60)
        self.progress.pack(anchor="w", pady=(0, 10))
        self.progress_lbl = ttk.Label(frame, text="准备安装…", foreground="#444")
        self.progress_lbl.pack(anchor="w")

    def show_result(self) -> None:
        ttk = self.ttk
        frame = self._clear()
        result = self.result or {}
        success = bool(result.get("success"))
        if success:
            self._title(frame, "安装完成").pack(anchor="w", pady=(0, 10))
            ttk.Label(
                frame,
                text=f"{APP_NAME} v{__version__} 已安装到：\n{result.get('target', self.path_var.get())}",
                wraplength=WINDOW_W - 60,
                justify="left",
            ).pack(anchor="w")
            warnings = result.get("warnings") or []
            if warnings:
                ttk.Label(frame, text="部分快捷方式创建失败，但程序已可使用。", foreground="#a60").pack(anchor="w", pady=(8, 0))
            btns = ttk.Frame(frame)
            btns.pack(side="bottom", anchor="e", pady=(20, 0), fill="x")
            ttk.Button(btns, text="完成", command=self._finish_no_launch).pack(side="right", padx=(8, 0))
            ttk.Button(btns, text=f"启动 {APP_NAME}", command=self._finish_launch).pack(side="right")
        else:
            self._title(frame, "安装未完成").pack(anchor="w", pady=(0, 10))
            ttk.Label(
                frame,
                text=str(result.get("error") or "安装失败，请重试。"),
                wraplength=WINDOW_W - 60,
                justify="left",
                foreground="#c00",
            ).pack(anchor="w")
            btns = ttk.Frame(frame)
            btns.pack(side="bottom", anchor="e", pady=(20, 0), fill="x")
            ttk.Button(btns, text="关闭", command=self._on_close).pack(side="right", padx=(8, 0))
            label = "换个位置重试" if result.get("needs_other_location") else "返回重试"
            ttk.Button(btns, text=label, command=self.show_path).pack(side="right")

    # ---- install flow ------------------------------------------------------
    def _start_install(self) -> None:
        self.install_path = (self.path_var.get() or "").strip() or self.install_path
        self._installing = True
        self.show_progress()
        target = self.install_path

        def worker() -> None:
            try:
                res = install_app(
                    create_shortcuts=True,
                    enable_startup=False,
                    target_dir=target,
                    progress=lambda frac, msg: self._queue.put(("progress", frac, msg)),
                )
                self._queue.put(("done", res))
            except Exception as exc:  # noqa: BLE001 - surface to UI
                self._queue.put(("error", str(exc)))

        threading.Thread(target=worker, name="CodexSyncInstall", daemon=True).start()
        self.root.after(80, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, frac, msg = item
                    self.progress["value"] = float(frac) * 100
                    self.progress_lbl.config(text=msg)
                elif kind == "done":
                    self.result = item[1]
                    self._installing = False
                    self.show_result()
                    return
                elif kind == "error":
                    self.result = {"success": False, "error": item[1]}
                    self._installing = False
                    self.show_result()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _finish_launch(self) -> None:
        self.launch = True
        self.root.destroy()

    def _finish_no_launch(self) -> None:
        self.launch = False
        self.root.destroy()

    def run(self) -> dict[str, Any]:
        self.show_welcome()
        self.root.mainloop()
        result = self.result or {}
        return {
            "available": True,
            "completed": bool(result.get("success")),
            "launch": self.launch,
            "target": result.get("target"),
        }


def run() -> dict[str, Any]:
    """启动安装向导。返回 dict；tkinter 不可用时返回 {"available": False}。"""
    try:
        import tkinter  # noqa: F401
        from tkinter import ttk  # noqa: F401
    except Exception:  # noqa: BLE001 - headless / tk missing → caller downgrades
        return {"available": False}
    try:
        return InstallerWizard().run()
    except Exception as exc:  # noqa: BLE001 - any GUI failure → caller downgrades
        return {"available": False, "error": str(exc)}
