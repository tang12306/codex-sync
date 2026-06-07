"""Codex Sync 独立 exe 入口（PyInstaller 打包用）。

- 双击运行（无命令行参数）→ 打开本地 Web 控制台并自动唤起浏览器。
- 带参数运行 → 等价于 `python -m codex_sync <args>`（CLI）。

打包成 --noconsole(windowed) 时，sys.stdout/stderr 可能为 None；这里先重定向到 devnull，
避免内部 print 触发 AttributeError。
"""
import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from codex_sync.cli import main

if __name__ == "__main__":
    main(["desktop"] if len(sys.argv) <= 1 else sys.argv[1:])
