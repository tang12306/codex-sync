from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Windows 上启动子进程时不创建控制台窗口：避免无窗口父进程（pythonw、计划任务、hook）
# 调用 git / schtasks / wsl 等控制台程序时弹出一闪而过的黑框。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def pythonw_executable() -> str:
    """返回无控制台窗口的解释器（pythonw.exe）；找不到时退回当前解释器。"""
    exe = Path(sys.executable).resolve()
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def decode_process_output(data: bytes | None) -> str:
    if not data:
        return ""
    # 关键：循环内用严格解码，解错才抛异常并继续试下一种编码。
    # 旧实现用 errors="replace"，第一种编码永不失败、直接返回，导致中文(GBK)被替换成 �。
    encodings = []
    if b"\x00" in data[:200]:
        encodings.append("utf-16le")
    encodings.extend(["utf-8-sig", "utf-8", "mbcs", "cp936"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # 全部失败时兜底，保证不抛：Windows 用系统编码，其他用 utf-8。
    return data.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(value: str) -> str:
    clean = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            clean.append(char)
        else:
            clean.append("_")
    return "".join(clean).strip("._") or "unknown"


def run_cmd(args: list[str], cwd: str | Path | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode, decode_process_output(proc.stdout).strip(), decode_process_output(proc.stderr).strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, decode_process_output(exc.stdout).strip(), decode_process_output(exc.stderr).strip() or f"Command timed out after {timeout}s"


def read_tail(path: Path, max_bytes: int = 64 * 1024) -> str:
    if not path.exists() or not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(-max_bytes, os.SEEK_END)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False))
        handle.write("\n")


def system_fingerprint() -> dict[str, str]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
    }
