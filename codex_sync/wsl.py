from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .config import AppConfig
from .full_backup import (
    DENY_NAMES,
    DENY_PARTS,
    DENY_SUFFIXES,
    DIRECTORIES,
    EXACT_FILES,
    FILE_PREFIXES,
    FILE_SUFFIXES,
    OPTIONAL_CONFIG_FILES,
    OPTIONAL_DIRECTORIES,
    open_full_backup_zip,
    read_full_backup_manifest,
    upload_full_backup,
    _manifest_path_for_archive,
    _write_encrypted_archive,
)
from .paths import app_dir, ensure_app_dirs
from .redact import redact_text
from .util import run_cmd, safe_filename, utc_now, write_json


def list_distros() -> list[str]:
    code, out, _ = run_cmd(["wsl.exe", "-l", "-q"], timeout=20)
    if code != 0:
        return []
    names = []
    for line in out.replace("\x00", "").splitlines():
        clean = line.strip()
        if clean:
            names.append(clean)
    return names


def _list_distros_result() -> tuple[list[str], str | None]:
    code, out, err = run_cmd(["wsl.exe", "-l", "-q"], timeout=20)
    if code != 0:
        return [], err or "wsl.exe returned a non-zero exit code"
    names = []
    for line in out.replace("\x00", "").splitlines():
        clean = line.strip()
        if clean:
            names.append(clean)
    return names, None


def distro_status(distro: str) -> dict[str, Any]:
    script = "printf '%s\\n' \"$HOME\"; test -d ~/.codex && echo HAS_CODEX || echo NO_CODEX; test -f ~/.codex/config.toml && echo HAS_CONFIG || true; test -f ~/.codex/AGENTS.md && echo HAS_AGENTS || true"
    code, out, err = run_cmd(["wsl.exe", "-d", distro, "sh", "-lc", script], timeout=30)
    lines = out.splitlines()
    return {
        "distro": distro,
        "ok": code == 0,
        "home": lines[0] if lines else "",
        "has_codex": "HAS_CODEX" in lines,
        "has_config": "HAS_CONFIG" in lines,
        "has_agents": "HAS_AGENTS" in lines,
        "warning": err if code == 0 and err else None,
        "error": err if code != 0 else None,
    }


def wsl_status() -> dict[str, Any]:
    distros, error = _list_distros_result()
    return {"success": error is None, "error": error, "distros": [distro_status(name) for name in distros]}


def list_codex_homes() -> dict[str, Any]:
    from .codex_channels import list_channels

    windows = list_channels()
    status = wsl_status()
    homes = [
        {
            "id": "windows",
            "kind": "windows",
            "label": "Windows",
            "ok": bool(windows.get("success")),
            "has_codex": bool(windows.get("success")),
            "current_provider": windows.get("current_provider"),
            "error": windows.get("error"),
        }
    ]
    for item in status.get("distros", []) or []:
        if not isinstance(item, dict):
            continue
        homes.append(
            {
                "id": f"wsl:{item.get('distro')}",
                "kind": "wsl",
                "distro": item.get("distro"),
                "label": f"WSL · {item.get('distro')}",
                "ok": bool(item.get("ok")),
                "has_codex": bool(item.get("has_codex")),
                "home": item.get("home"),
                "current_provider": _wsl_current_provider(str(item.get("distro"))) if item.get("ok") and item.get("has_codex") else None,
                "warning": item.get("warning"),
                "error": item.get("error"),
            }
        )
    return {"success": status.get("success", True), "homes": homes, "wsl_error": status.get("error")}


def pull_wsl_config(distro: str) -> dict[str, Any]:
    ensure_app_dirs()
    if not distro:
        return {"success": False, "error": "distro is required"}
    dest = app_dir() / "wsl" / safe_filename(distro)
    dest.mkdir(parents=True, exist_ok=True)
    files = {
        "config.toml": "~/.codex/config.toml",
        "AGENTS.md": "~/.codex/AGENTS.md",
        "AGENTS.override.md": "~/.codex/AGENTS.override.md",
        "hooks.json": "~/.codex/hooks.json",
    }
    pulled: list[str] = []
    skipped: list[str] = []
    for name, remote in files.items():
        code, out, _ = run_cmd(["wsl.exe", "-d", distro, "sh", "-lc", f"test -f {remote} && cat {remote}"], timeout=30)
        if code == 0 and out:
            (dest / name).write_text(redact_text(out), encoding="utf-8")
            pulled.append(name)
        else:
            skipped.append(name)
    (dest / "pulled_at.txt").write_text(utc_now(), encoding="utf-8")
    return {"success": True, "distro": distro, "dest": str(dest), "pulled": pulled, "skipped": skipped}


def wsl_backup_dir(distro: str) -> Path:
    ensure_app_dirs()
    path = app_dir() / "wsl" / safe_filename(distro) / "full-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def wsl_backup_state_path(distro: str) -> Path:
    return wsl_backup_dir(distro) / "state.json"


def _read_wsl_backup_state(distro: str) -> dict[str, Any]:
    path = wsl_backup_state_path(distro)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_wsl_backup_state(distro: str, data: dict[str, Any]) -> None:
    write_json(wsl_backup_state_path(distro), data)


def wsl_full_backup_status(distro: str) -> dict[str, Any]:
    state = _read_wsl_backup_state(distro)
    last_digest = str(state.get("last_content_digest") or "")
    uploaded_digest = str(state.get("last_uploaded_content_digest") or "")
    return {
        "success": True,
        "distro": distro,
        "device_id": _wsl_device_id(distro),
        "branch_id": _wsl_branch_id(distro, state),
        "needs_upload": bool(last_digest and last_digest != uploaded_digest),
        "last_content_digest": last_digest,
        "last_uploaded_content_digest": uploaded_digest,
        "last_backup_id": state.get("last_backup_id"),
        "last_uploaded_backup_id": state.get("last_uploaded_backup_id"),
        "last_created_at": state.get("last_created_at"),
        "last_uploaded_at": state.get("last_uploaded_at"),
        "last_archive": state.get("last_archive"),
    }


def _wsl_device_id(distro: str) -> str:
    return f"wsl:{distro}"


def _wsl_branch_id(distro: str, state: dict[str, Any] | None = None) -> str:
    state = state or _read_wsl_backup_state(distro)
    value = str(state.get("branch_id") or "").strip()
    if value:
        return value
    return f"{_wsl_device_id(distro)}:main"


def _wsl_sh(distro: str, script: str, input_bytes: bytes | None = None, timeout: int = 120) -> tuple[int, bytes, str]:
    try:
        proc = subprocess.run(
            ["wsl.exe", "-d", distro, "sh", "-lc", script],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        err = _decode_wsl_stderr(proc.stderr).strip()
        return proc.returncode, proc.stdout, err
    except FileNotFoundError as exc:
        return 127, b"", str(exc)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        err = _decode_wsl_stderr(exc.stderr or b"").strip() or f"Command timed out after {timeout}s"
        return 124, out, err


def _decode_wsl_stderr(raw: bytes) -> str:
    if not raw:
        return ""
    if b"\x00" in raw[:80]:
        return raw.decode("utf-16le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _wsl_safe_rel(rel: str) -> str | None:
    clean = rel.replace("\\", "/").strip()
    if clean.startswith("./"):
        clean = clean[2:]
    if not clean:
        return None
    path = Path(clean)
    if path.is_absolute() or ".." in path.parts:
        return None
    return clean


def _wsl_file_allowed(rel: str, include_config: bool, include_memories: bool) -> bool:
    path = Path(rel)
    parts = path.parts
    if any(part in DENY_PARTS for part in parts):
        return False
    if path.name in DENY_NAMES or path.suffix.lower() in DENY_SUFFIXES:
        return False
    if len(parts) == 1:
        name = path.name
        if name in EXACT_FILES:
            return True
        if include_config and name in OPTIONAL_CONFIG_FILES:
            return True
        return any(name.startswith(prefix) for prefix in FILE_PREFIXES) and any(name.endswith(suffix) for suffix in FILE_SUFFIXES)
    allowed_dirs = set(DIRECTORIES)
    if include_memories:
        allowed_dirs.update(OPTIONAL_DIRECTORIES)
    return parts[0] in allowed_dirs


def _wsl_candidate_files(distro: str, include_config: bool = True, include_memories: bool = True) -> tuple[list[str], str | None]:
    prune = " ".join(f"-path './{part}' -o -path './{part}/*'" for part in sorted(DENY_PARTS))
    script = (
        "cd ~/.codex 2>/dev/null || exit 10; "
        f"find . \\( {prune} \\) -prune -o -type f -print0"
    )
    code, out, err = _wsl_sh(distro, script, timeout=60)
    if code != 0:
        return [], err or "WSL Codex home not found"
    files = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = _wsl_safe_rel(raw.decode("utf-8", errors="replace"))
        if rel and _wsl_file_allowed(rel, include_config=include_config, include_memories=include_memories):
            files.append(rel)
    return sorted(set(files)), None


def _read_wsl_file(distro: str, rel: str) -> tuple[bytes | None, str | None]:
    code, out, err = _wsl_sh(distro, f"cd ~/.codex && cat -- {shlex.quote(rel)}", timeout=120)
    if code != 0:
        return None, err or f"failed to read {rel}"
    return out, None


def _wsl_codex_home(distro: str) -> str:
    code, out, _ = _wsl_sh(distro, "cd ~/.codex 2>/dev/null && pwd", timeout=30)
    if code != 0:
        return "~/.codex"
    return out.decode("utf-8", errors="replace").strip() or "~/.codex"


def _wsl_current_provider(distro: str) -> str | None:
    data, err = _read_wsl_file(distro, "config.toml")
    if err or data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
        match = re.match(r'\s*model_provider\s*=\s*"([^"]+)"', line)
        if match:
            return match.group(1)
    return None


def _wsl_state_db_rel(distro: str) -> str | None:
    files, err = _wsl_candidate_files(distro, include_config=False, include_memories=False)
    if err:
        return None
    state_files = [name for name in files if re.fullmatch(r"state_\d+\.sqlite", Path(name).name)]
    if not state_files:
        return None
    return sorted(state_files, key=lambda name: int(re.search(r"state_(\d+)\.sqlite", Path(name).name).group(1)), reverse=True)[0]


def list_wsl_channels(distro: str) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    rel = _wsl_state_db_rel(distro)
    if not rel:
        return {"success": False, "error": f"未找到 WSL Codex 状态数据库 state_*.sqlite：{distro}"}
    data, err = _read_wsl_file(distro, rel)
    if err or data is None:
        return {"success": False, "error": err or f"读取 WSL 状态库失败：{rel}"}
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(data)
    tmp.close()
    channels: list[dict[str, Any]] = []
    con = sqlite3.connect(tmp.name)
    try:
        for provider, count in con.execute("SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider ORDER BY model_provider"):
            channels.append({"provider": provider or "(unknown)", "threads": count})
    finally:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)
    return {
        "success": True,
        "home": f"wsl:{distro}",
        "distro": distro,
        "db": rel,
        "current_provider": _wsl_current_provider(distro),
        "channels": channels,
        "merged": _wsl_merge_summary(distro),
    }


def list_wsl_conversations(
    distro: str,
    search: str = "",
    cwd: str = "",
    provider: str = "",
    include_archived: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    rel = _wsl_state_db_rel(distro)
    if not rel:
        return {"success": False, "error": f"未找到 WSL Codex 状态数据库 state_*.sqlite：{distro}", "distro": distro}
    data, err = _read_wsl_file(distro, rel)
    if err or data is None:
        return {"success": False, "error": err or f"读取 WSL 状态库失败：{rel}", "distro": distro}

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(data)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    try:
        where: list[str] = []
        params: list[Any] = []
        if not include_archived:
            where.append("COALESCE(archived,0)=0")
        if cwd:
            where.append("cwd=?")
            params.append(cwd)
        if provider:
            where.append("model_provider=?")
            params.append(provider)
        if search:
            where.append("(title LIKE ? OR preview LIKE ? OR first_user_message LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = con.execute(
            "SELECT id,title,preview,first_user_message,cwd,model_provider,created_at,updated_at,"
            f"rollout_path,COALESCE(archived,0) AS archived FROM threads {wsql} ORDER BY updated_at DESC LIMIT ?",
            [*params, max(1, min(int(limit or 500), 2000))],
        ).fetchall()
        home_id = f"wsl:{distro}"
        merge_state = _read_wsl_merge_state(distro)
        conversations: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            merge_info = merge_state.get(str(row["id"])) or {}
            item.update(
                {
                    "home_id": home_id,
                    "home_kind": "wsl",
                    "home_label": f"WSL {distro}",
                    "distro": distro,
                    "merged": bool(merge_info),
                    "original_provider": merge_info.get("original_provider"),
                }
            )
            conversations.append(item)
        cwds = [x[0] for x in con.execute("SELECT DISTINCT cwd FROM threads WHERE cwd IS NOT NULL AND cwd<>'' ORDER BY cwd")]
        providers = [x[0] for x in con.execute("SELECT DISTINCT model_provider FROM threads WHERE model_provider IS NOT NULL AND model_provider<>'' ORDER BY model_provider")]
    finally:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)

    return {
        "success": True,
        "home": f"wsl:{distro}",
        "distro": distro,
        "db": rel,
        "conversations": conversations,
        "count": len(conversations),
        "cwds": cwds,
        "providers": providers,
        "current_provider": _wsl_current_provider(distro),
    }


def _wsl_rollout_rel(distro: str, rollout_path: str | None) -> str | None:
    if not rollout_path:
        return None
    raw = str(rollout_path).replace("\\", "/")
    home = _wsl_codex_home(distro).replace("\\", "/").rstrip("/")
    prefix = home + "/"
    if raw.startswith(prefix):
        return _wsl_safe_rel(raw[len(prefix):])
    marker = "/.codex/"
    if marker in raw:
        return _wsl_safe_rel(raw.split(marker, 1)[1])
    if not raw.startswith("/"):
        return _wsl_safe_rel(raw)
    return None


def read_wsl_conversation(
    distro: str,
    thread_id: str,
    include_tools: bool = True,
    include_reasoning: bool = False,
    include_developer: bool = False,
    max_chars: int = 200000,
) -> dict[str, Any]:
    from .conversations import _any_text, _content_text, _reasoning_text

    if not distro:
        return {"success": False, "error": "distro is required"}
    if not thread_id:
        return {"success": False, "error": "thread_id is required", "distro": distro}
    rel = _wsl_state_db_rel(distro)
    if not rel:
        return {"success": False, "error": f"未找到 WSL Codex 状态数据库 state_*.sqlite：{distro}", "distro": distro}
    data, err = _read_wsl_file(distro, rel)
    if err or data is None:
        return {"success": False, "error": err or f"读取 WSL 状态库失败：{rel}", "distro": distro}

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(data)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id,title,model_provider,cwd,created_at,updated_at,rollout_path FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
    finally:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)
    if row is None:
        return {"success": False, "error": "未找到该 WSL 对话", "distro": distro}

    thread = dict(row)
    thread.update({"home_id": f"wsl:{distro}", "home_kind": "wsl", "home_label": f"WSL {distro}", "distro": distro})
    rollout_rel = _wsl_rollout_rel(distro, row["rollout_path"])
    if not rollout_rel:
        return {"success": False, "error": f"无法定位 WSL 对话正文：{row['rollout_path']}", "thread": thread, "distro": distro}
    body, read_err = _read_wsl_file(distro, rollout_rel)
    if read_err or body is None:
        return {"success": False, "error": read_err or f"读取 WSL 对话正文失败：{rollout_rel}", "thread": thread, "distro": distro}

    messages: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ptype = payload.get("type")
        if kind == "response_item" and ptype == "message":
            role = payload.get("role")
            if role == "developer":
                if not include_developer:
                    continue
            elif role not in ("user", "assistant"):
                continue
            text = _content_text(payload.get("content"))
            messages.append({"role": role, "text": text})
            total += len(text)
        elif include_reasoning and kind == "response_item" and ptype == "reasoning":
            text = _reasoning_text(payload)
            if text:
                messages.append({"role": "reasoning", "text": text})
                total += len(text)
        elif include_tools and kind == "response_item" and ptype == "function_call":
            name = payload.get("name") or "tool"
            args = _any_text(payload.get("arguments"))[:2000]
            messages.append({"role": "tool_call", "text": f"{name} {args}".strip()})
        elif include_tools and kind == "response_item" and ptype == "function_call_output":
            messages.append({"role": "tool_output", "text": _any_text(payload.get("output"))[:2000]})
        if total > max_chars:
            truncated = True
            break
    return {"success": True, "thread": thread, "messages": messages, "message_count": len(messages), "truncated": truncated}


def export_wsl_conversation(
    distro: str,
    thread_id: str,
    fmt: str = "markdown",
    include_tools: bool = False,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    from .conversations import _ROLE_LABEL, exports_dir

    data = read_wsl_conversation(distro, thread_id, include_tools=include_tools, include_reasoning=include_reasoning)
    if not data.get("success"):
        return data
    thread = data["thread"]
    messages = data["messages"]
    title = thread.get("title") or thread_id
    stem = f"wsl-{safe_filename(distro)}-{safe_filename(title)[:60]}-{str(thread_id)[:8]}"
    if fmt == "json":
        path = exports_dir() / f"{stem}.json"
        path.write_text(json.dumps({"thread": thread, "messages": messages}, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        lines = [
            f"# {title}",
            "",
            f"- 对话 ID：{thread_id}",
            f"- 环境：WSL {distro}",
            f"- 渠道：{thread.get('model_provider')}",
            f"- 项目：{thread.get('cwd')}",
            "",
        ]
        for msg in messages:
            lines.append(f"## {_ROLE_LABEL.get(msg['role'], msg['role'])}")
            lines.append("")
            lines.append(msg["text"])
            lines.append("")
        path = exports_dir() / f"{stem}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
    return {"success": True, "path": str(path), "fmt": fmt, "message_count": len(messages), "truncated": data.get("truncated", False), "distro": distro}


def wsl_threads_index(distro: str) -> dict[str, int]:
    rel = _wsl_state_db_rel(distro)
    if not rel:
        return {}
    data, err = _read_wsl_file(distro, rel)
    if err or data is None:
        return {}
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(data)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    try:
        return {row[0]: (row[1] or 0) for row in con.execute("SELECT id, updated_at FROM threads")}
    finally:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)


def _wsl_codex_running(distro: str) -> bool:
    code, out, _ = _wsl_sh(distro, "pgrep -fl 'codex' 2>/dev/null | grep -v pgrep | grep -v grep", timeout=15)
    return code == 0 and bool(out.strip())


def _stop_wsl_codex(distro: str) -> dict[str, Any]:
    code, _, err = _wsl_sh(distro, "pkill -f 'codex' 2>/dev/null || true", timeout=20)
    still = _wsl_codex_running(distro)
    return {"stopped": not still, "still_running": still, "kill_rc": code, "error": err or None}


def _ensure_wsl_codex_closed(distro: str, close_running: bool) -> dict[str, Any] | None:
    if not _wsl_codex_running(distro):
        return None
    if not close_running:
        return {
            "success": False,
            "needs_close": True,
            "target_home": f"wsl:{distro}",
            "error": f"检测到 {distro} 里 Codex 正在运行。导入会写入该 WSL 的 ~/.codex，需要先关闭它。",
        }
    stop = _stop_wsl_codex(distro)
    if stop.get("still_running"):
        return {"success": False, "needs_close": True, "error": f"尝试关闭 {distro} 里的 Codex 失败，请手动关闭后重试。", "stop": stop}
    return None


def wsl_merge_state_path(distro: str) -> Path:
    ensure_app_dirs()
    path = app_dir() / "wsl" / safe_filename(distro)
    path.mkdir(parents=True, exist_ok=True)
    return path / "channel-merge-state.json"


def _read_wsl_merge_state(distro: str) -> dict[str, Any]:
    path = wsl_merge_state_path(distro)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_wsl_merge_state(distro: str, data: dict[str, Any]) -> None:
    write_json(wsl_merge_state_path(distro), data)


def _wsl_db_backup_dir(distro: str) -> Path:
    path = app_dir() / "wsl" / safe_filename(distro) / "codex-db-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_wsl_state_db(distro: str, state_rel: str, data: bytes) -> dict[str, Any]:
    stamp = utc_now().replace(":", "").replace("+", "Z")
    dest = _wsl_db_backup_dir(distro) / stamp
    dest.mkdir(parents=True, exist_ok=True)
    name = safe_filename(Path(state_rel).name or "state.sqlite")
    target = dest / name
    target.write_bytes(data)
    return {"dir": str(dest), "files": [name]}


def _wsl_write_file(distro: str, rel: str, data: bytes, timeout: int = 120) -> tuple[bool, str | None]:
    safe = _wsl_safe_rel(rel)
    if not safe:
        return False, "unsafe path"
    dirname = shlex.quote(str(Path(safe).parent).replace("\\", "/"))
    target_name = shlex.quote(safe)
    mkdir = "" if dirname == "'.'" else f"mkdir -p {dirname} && "
    code, _, err = _wsl_sh(distro, f"mkdir -p ~/.codex && cd ~/.codex && {mkdir}cat > {target_name}", input_bytes=data, timeout=timeout)
    return code == 0, err or None


def _write_wsl_state_db(distro: str, state_rel: str, data: bytes) -> tuple[bool, str | None]:
    safe = _wsl_safe_rel(state_rel)
    if not safe:
        return False, "unsafe state db path"
    quoted = shlex.quote(safe)
    code, _, err = _wsl_sh(
        distro,
        f"mkdir -p ~/.codex && cat > ~/.codex/{quoted} && rm -f ~/.codex/{quoted}-wal ~/.codex/{quoted}-shm",
        input_bytes=data,
        timeout=120,
    )
    return code == 0, err or None


def _wsl_rollout_set_provider(distro: str, rollout_path: str | None, provider: str) -> dict[str, Any]:
    rel = _wsl_rollout_rel(distro, rollout_path)
    if not rel:
        return {"ok": False, "reason": "no_path", "path": rollout_path}
    raw, err = _read_wsl_file(distro, rel)
    if err or raw is None:
        return {"ok": False, "reason": err or "read_failed", "path": rel}
    lines = raw.splitlines(keepends=True)
    if not lines:
        return {"ok": False, "reason": "empty", "path": rel}
    try:
        first = json.loads(lines[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "reason": "bad_json", "path": rel}
    payload = first.get("payload")
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        return {"ok": False, "reason": "no_session_meta", "path": rel}
    old = payload.get("model_provider")
    if old == provider:
        return {"ok": True, "changed": False, "old": old, "path": rel}
    payload["model_provider"] = provider
    newline = b"\r\n" if lines[0].endswith(b"\r\n") else b"\n"
    rewritten = (json.dumps(first, ensure_ascii=False).encode("utf-8") + newline) + b"".join(lines[1:])
    ok, write_err = _wsl_write_file(distro, rel, rewritten, timeout=120)
    if not ok:
        return {"ok": False, "reason": write_err or "write_failed", "path": rel}
    return {"ok": True, "changed": True, "old": old, "new": provider, "path": rel}


def _load_wsl_state_tmp(distro: str) -> tuple[str | None, Path | None, bytes | None, dict[str, Any] | None]:
    state_rel = _wsl_state_db_rel(distro)
    if not state_rel:
        return None, None, None, {"success": False, "error": f"未找到 {distro} 的 WSL Codex 状态数据库"}
    data, err = _read_wsl_file(distro, state_rel)
    if err or data is None:
        return None, None, None, {"success": False, "error": err or f"读取 {state_rel} 失败"}
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.write(data)
    tmp.close()
    return state_rel, Path(tmp.name), data, None


def _wsl_merge_summary(distro: str) -> dict[str, Any]:
    merge_state = _read_wsl_merge_state(distro)
    by_target: dict[str, int] = {}
    origin_breakdown: dict[str, dict[str, int]] = {}
    for info in merge_state.values():
        target = str(info.get("current_provider") or "")
        origin = str(info.get("original_provider") or "")
        if not target:
            continue
        by_target[target] = by_target.get(target, 0) + 1
        if origin:
            origin_breakdown.setdefault(target, {})
            origin_breakdown[target][origin] = origin_breakdown[target].get(origin, 0) + 1
    return {"total": len(merge_state), "by_target": by_target, "origin_breakdown": origin_breakdown}


def _merge_wsl_rows(config: AppConfig, distro: str, state_rel: str, tmp_db: Path, original_db: bytes, rows: list[dict[str, Any]], target: str) -> dict[str, Any]:
    if not rows:
        return {"success": False, "error": "没有可并入的对话（可能它们已在目标渠道）", "target_home": f"wsl:{distro}"}
    preflight = create_wsl_full_backup(distro, include_config=True, include_memories=True, config=config)
    db_backup = _backup_wsl_state_db(distro, state_rel, original_db)
    merge_state = _read_wsl_merge_state(distro)
    moved: list[dict[str, Any]] = []
    rollout_results: list[dict[str, Any]] = []
    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    try:
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(ids))
        con.execute(f"UPDATE threads SET model_provider=? WHERE id IN ({placeholders})", [target, *ids])
        con.commit()
    finally:
        con.close()
    for row in rows:
        tid = str(row["id"])
        prior = merge_state.get(tid, {})
        original = prior.get("original_provider", row.get("model_provider"))
        rollout_results.append({"id": tid, **_wsl_rollout_set_provider(distro, row.get("rollout_path"), target)})
        merge_state[tid] = {
            "home": f"wsl:{distro}",
            "original_provider": original,
            "current_provider": target,
            "title": row.get("title"),
            "cwd": row.get("cwd"),
            "rollout_path": row.get("rollout_path"),
            "merged_at": utc_now(),
        }
        moved.append({"id": tid, "from": row.get("model_provider"), "to": target, "title": row.get("title")})
    ok, write_err = _write_wsl_state_db(distro, state_rel, tmp_db.read_bytes())
    if not ok:
        return {"success": False, "error": write_err or f"写回 {state_rel} 失败", "preflight_backup": preflight, "db_backup": db_backup}
    _write_wsl_merge_state(distro, merge_state)
    return {
        "success": True,
        "target_home": f"wsl:{distro}",
        "distro": distro,
        "moved": len(moved),
        "target": target,
        "items": moved,
        "rollout": {
            "changed": sum(1 for item in rollout_results if item.get("changed")),
            "failed": [item for item in rollout_results if not item.get("ok")],
        },
        "preflight_backup": preflight,
        "db_backup": db_backup,
    }


def merge_wsl_channels(
    config: AppConfig,
    distro: str,
    sources: list[str] | None = None,
    target: str | None = None,
    all_others: bool = False,
    close_running: bool = False,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    target = target or _wsl_current_provider(distro)
    if not target:
        return {"success": False, "error": "无法确定目标渠道，请显式指定", "target_home": f"wsl:{distro}"}
    guard = _ensure_wsl_codex_closed(distro, close_running)
    if guard is not None:
        return guard
    state_rel, tmp_db, original_db, error = _load_wsl_state_tmp(distro)
    if error:
        return error
    assert state_rel is not None and tmp_db is not None and original_db is not None
    try:
        con = sqlite3.connect(tmp_db)
        con.row_factory = sqlite3.Row
        try:
            existing = [row["p"] for row in con.execute("SELECT DISTINCT model_provider AS p FROM threads") if row["p"]]
            if all_others:
                sources = [provider for provider in existing if provider != target]
            sources = sorted({source for source in (sources or []) if source and source != target})
            if not sources:
                return {"success": False, "error": "没有要并入的源渠道", "target_home": f"wsl:{distro}"}
            placeholders = ",".join("?" * len(sources))
            rows = [
                dict(row)
                for row in con.execute(
                    f"SELECT id, model_provider, title, cwd, rollout_path FROM threads WHERE model_provider IN ({placeholders})",
                    sources,
                )
            ]
        finally:
            con.close()
        result = _merge_wsl_rows(config, distro, state_rel, tmp_db, original_db, rows, target)
        if result.get("success"):
            result["sources"] = sources
        return result
    finally:
        tmp_db.unlink(missing_ok=True)


def merge_wsl_threads(
    config: AppConfig,
    distro: str,
    thread_ids: list[str] | None,
    target: str | None = None,
    close_running: bool = False,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    target = target or _wsl_current_provider(distro)
    if not target:
        return {"success": False, "error": "无法确定目标渠道，请显式指定", "target_home": f"wsl:{distro}"}
    ids = sorted({tid for tid in (thread_ids or []) if tid})
    if not ids:
        return {"success": False, "error": "未指定要并入的对话", "target_home": f"wsl:{distro}"}
    guard = _ensure_wsl_codex_closed(distro, close_running)
    if guard is not None:
        return guard
    state_rel, tmp_db, original_db, error = _load_wsl_state_tmp(distro)
    if error:
        return error
    assert state_rel is not None and tmp_db is not None and original_db is not None
    try:
        con = sqlite3.connect(tmp_db)
        con.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" * len(ids))
            rows = [
                dict(row)
                for row in con.execute(
                    f"SELECT id, model_provider, title, cwd, rollout_path FROM threads WHERE id IN ({placeholders})",
                    ids,
                )
                if row["model_provider"] != target
            ]
        finally:
            con.close()
        return _merge_wsl_rows(config, distro, state_rel, tmp_db, original_db, rows, target)
    finally:
        tmp_db.unlink(missing_ok=True)


def _restore_wsl_rows(config: AppConfig, distro: str, state_rel: str, tmp_db: Path, original_db: bytes, to_restore: dict[str, Any]) -> dict[str, Any]:
    preflight = create_wsl_full_backup(distro, include_config=True, include_memories=True, config=config)
    db_backup = _backup_wsl_state_db(distro, state_rel, original_db)
    merge_state = _read_wsl_merge_state(distro)
    restored: list[dict[str, Any]] = []
    rollout_results: list[dict[str, Any]] = []
    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    try:
        for tid, info in to_restore.items():
            original = str(info.get("original_provider") or "")
            if not original:
                continue
            row = con.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
            if row is None:
                continue
            con.execute("UPDATE threads SET model_provider=? WHERE id=?", (original, tid))
            rollout_path = info.get("rollout_path") or row["rollout_path"]
            rollout_results.append({"id": tid, **_wsl_rollout_set_provider(distro, rollout_path, original)})
            restored.append({"id": tid, "to": original})
        con.commit()
    finally:
        con.close()
    ok, write_err = _write_wsl_state_db(distro, state_rel, tmp_db.read_bytes())
    if not ok:
        return {"success": False, "error": write_err or f"写回 {state_rel} 失败", "preflight_backup": preflight, "db_backup": db_backup}
    for tid in to_restore:
        merge_state.pop(tid, None)
    _write_wsl_merge_state(distro, merge_state)
    return {
        "success": True,
        "target_home": f"wsl:{distro}",
        "distro": distro,
        "restored": len(restored),
        "items": restored,
        "rollout": {
            "changed": sum(1 for item in rollout_results if item.get("changed")),
            "failed": [item for item in rollout_results if not item.get("ok")],
        },
        "preflight_backup": preflight,
        "db_backup": db_backup,
    }


def restore_wsl_channels(
    config: AppConfig,
    distro: str,
    sources: list[str] | None = None,
    all_merged: bool = False,
    close_running: bool = False,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    guard = _ensure_wsl_codex_closed(distro, close_running)
    if guard is not None:
        return guard
    merge_state = _read_wsl_merge_state(distro)
    if not merge_state:
        return {"success": True, "restored": 0, "message": "没有需要还原的并入记录", "target_home": f"wsl:{distro}"}
    if all_merged:
        to_restore = dict(merge_state)
    else:
        wanted = {source for source in (sources or []) if source}
        to_restore = {tid: info for tid, info in merge_state.items() if info.get("original_provider") in wanted}
    if not to_restore:
        return {"success": True, "restored": 0, "message": "没有匹配的并入记录可还原", "target_home": f"wsl:{distro}"}
    state_rel, tmp_db, original_db, error = _load_wsl_state_tmp(distro)
    if error:
        return error
    assert state_rel is not None and tmp_db is not None and original_db is not None
    try:
        return _restore_wsl_rows(config, distro, state_rel, tmp_db, original_db, to_restore)
    finally:
        tmp_db.unlink(missing_ok=True)


def restore_wsl_threads(
    config: AppConfig,
    distro: str,
    thread_ids: list[str] | None,
    close_running: bool = False,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    guard = _ensure_wsl_codex_closed(distro, close_running)
    if guard is not None:
        return guard
    wanted = {tid for tid in (thread_ids or []) if tid}
    merge_state = _read_wsl_merge_state(distro)
    to_restore = {tid: info for tid, info in merge_state.items() if tid in wanted}
    if not to_restore:
        return {"success": True, "restored": 0, "message": "选中的对话没有可还原的并入记录", "target_home": f"wsl:{distro}"}
    state_rel, tmp_db, original_db, error = _load_wsl_state_tmp(distro)
    if error:
        return error
    assert state_rel is not None and tmp_db is not None and original_db is not None
    try:
        return _restore_wsl_rows(config, distro, state_rel, tmp_db, original_db, to_restore)
    finally:
        tmp_db.unlink(missing_ok=True)


def _digest_items(items: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def create_wsl_full_backup(
    distro: str,
    include_config: bool = True,
    include_memories: bool = True,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    files, err = _wsl_candidate_files(distro, include_config=include_config, include_memories=include_memories)
    if err:
        return {"success": False, "error": err, "distro": distro}
    if not files:
        return {"success": False, "error": "No WSL Codex files found to back up", "distro": distro}

    state = _read_wsl_backup_state(distro)
    config = config or AppConfig()
    encrypted = bool(getattr(config, "full_backup_encryption_enabled", True))
    device_id = _wsl_device_id(distro)
    backup_id = f"wsl-{safe_filename(distro)}-{uuid.uuid4()}"
    created_at = utc_now()
    suffix = ".zip.enc" if encrypted else ".zip"
    archive = wsl_backup_dir(distro) / f"{created_at.replace(':', '').replace('+', 'Z')}-{backup_id}{suffix}"
    plain_archive = archive
    if encrypted:
        tmp = tempfile.NamedTemporaryFile(prefix=f"{safe_filename(backup_id)}-", suffix=".zip", dir=wsl_backup_dir(distro), delete=False)
        tmp.close()
        plain_archive = Path(tmp.name)
    included: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = 0
    manifest: dict[str, Any]
    with zipfile.ZipFile(plain_archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            data, read_err = _read_wsl_file(distro, rel)
            if read_err or data is None:
                skipped.append({"path": rel, "reason": read_err or "read failed"})
                continue
            sha = hashlib.sha256(data).hexdigest()
            included.append({"path": rel, "bytes": len(data), "sha256": sha})
            total += len(data)
            zf.writestr(f"codex/{rel}", data)
        manifest = {
            "id": backup_id,
            "type": "codex_full_backup",
            "format_version": 1,
            "created_at": created_at,
            "device_id": device_id,
            "branch_id": _wsl_branch_id(distro, state),
            "parent_backup_id": str(state.get("last_uploaded_backup_id") or ""),
            "distro": distro,
            "codex_home": "~/.codex",
            "encrypted": encrypted,
            "encryption": "AES-256-GCM" if encrypted else "none",
            "content_digest": _digest_items(included),
            "included_count": len(included),
            "included_bytes": total,
            "files": included,
            "skipped": skipped,
            "source": "wsl",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    if encrypted:
        try:
            encryption_header = _write_encrypted_archive(plain_archive, archive, config, manifest)
            manifest["encryption_header"] = {key: value for key, value in encryption_header.items() if key != "secret_source"}
            manifest["encryption_key_source"] = encryption_header.get("secret_source")
        finally:
            plain_archive.unlink(missing_ok=True)
    manifest["archive_name"] = archive.name
    manifest["archive"] = str(archive)
    manifest["archive_bytes"] = archive.stat().st_size
    manifest["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    write_json(_manifest_path_for_archive(archive), manifest)
    state.update(
        {
            "branch_id": manifest["branch_id"],
            "last_backup_id": backup_id,
            "last_content_digest": manifest["content_digest"],
            "last_created_at": created_at,
            "last_archive": str(archive),
        }
    )
    _write_wsl_backup_state(distro, state)
    return {
        "success": True,
        "distro": distro,
        "backup_id": backup_id,
        "archive": str(archive),
        "content_digest": manifest["content_digest"],
        "branch_id": manifest["branch_id"],
        "parent_backup_id": manifest["parent_backup_id"],
        "included_count": len(included),
        "included_bytes": total,
        "skipped": skipped,
        "encrypted": encrypted,
        "encryption": manifest.get("encryption", "none"),
    }


def _read_wsl_archive_manifest(config: AppConfig, archive: Path) -> dict[str, Any]:
    return read_full_backup_manifest(config, archive)


def _prepare_wsl_upload_manifest(config: AppConfig, distro: str, archive: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(manifest or _read_wsl_archive_manifest(config, archive))
    state = _read_wsl_backup_state(distro)
    device_id = str(data.get("device_id") or _wsl_device_id(distro))
    data["id"] = str(data.get("id") or archive.stem)
    data["type"] = "codex_full_backup"
    data["format_version"] = int(data.get("format_version") or 1)
    data["device_id"] = device_id
    data["branch_id"] = str(data.get("branch_id") or state.get("branch_id") or f"{device_id}:main")
    data["parent_backup_id"] = str(data.get("parent_backup_id") or state.get("last_uploaded_backup_id") or "")
    data["distro"] = str(data.get("distro") or distro)
    data["source"] = str(data.get("source") or "wsl")
    data["encrypted"] = bool(data.get("encrypted"))
    data["encryption"] = str(data.get("encryption") or "none")
    return data


def upload_wsl_full_backup(
    config: AppConfig,
    distro: str,
    archive: str | Path,
    manifest: dict[str, Any] | None = None,
    allow_plaintext_upload: bool = False,
) -> dict[str, Any]:
    if not distro:
        return {"success": False, "error": "distro is required"}
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}", "distro": distro}
    upload_manifest = _prepare_wsl_upload_manifest(config, distro, archive_path, manifest)
    uploaded = upload_full_backup(
        config,
        archive_path,
        manifest=upload_manifest,
        allow_plaintext_upload=allow_plaintext_upload,
        update_local_state=False,
    )
    if uploaded.get("success"):
        state = _read_wsl_backup_state(distro)
        state.update(
            {
                "branch_id": upload_manifest.get("branch_id") or _wsl_branch_id(distro, state),
                "last_uploaded_backup_id": upload_manifest.get("id"),
                "last_uploaded_content_digest": upload_manifest.get("content_digest"),
                "last_uploaded_at": utc_now(),
                "last_archive": str(archive_path),
                "last_server_device_state": uploaded.get("device_state"),
            }
        )
        _write_wsl_backup_state(distro, state)
    return {"distro": distro, "archive": str(archive_path), **uploaded}


def wsl_full_backup_now(
    config: AppConfig,
    distro: str,
    include_config: bool = True,
    include_memories: bool = True,
    upload: bool = False,
    allow_plaintext_upload: bool = False,
) -> dict[str, Any]:
    package = create_wsl_full_backup(distro, include_config=include_config, include_memories=include_memories, config=config)
    if not package.get("success"):
        return package
    if not upload:
        return {"uploaded": False, **package}
    uploaded = upload_wsl_full_backup(
        config,
        distro,
        package["archive"],
        allow_plaintext_upload=allow_plaintext_upload,
    )
    return {
        "success": bool(uploaded.get("success")),
        "uploaded": bool(uploaded.get("success")),
        "distro": distro,
        "package": package,
        "upload": uploaded,
    }


def latest_wsl_backup(distro: str) -> Path | None:
    root = wsl_backup_dir(distro)
    archives = sorted([*root.glob("*.zip"), *root.glob("*.zip.enc")], key=lambda path: path.stat().st_mtime, reverse=True)
    return archives[0] if archives else None


def _safe_rel(rel: str) -> str | None:
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        return None
    return rel.replace("\\", "/")


def restore_wsl_full_backup(
    distro: str,
    archive: str | Path,
    confirm_backup_id: str,
    restore_config: bool = False,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    config = config or AppConfig()
    preflight = create_wsl_full_backup(distro, include_config=True, include_memories=True, config=config)
    restored: list[str] = []
    skipped: list[dict[str, Any]] = []
    with open_full_backup_zip(config, archive_path) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        backup_id = str(manifest.get("id") or "")
        if confirm_backup_id != backup_id:
            return {"success": False, "error": "Refusing restore without exact confirm_backup_id match", "backup_id": backup_id}
        for name in zf.namelist():
            if not name.startswith("codex/") or name.endswith("/"):
                continue
            rel = _safe_rel(name[len("codex/"):])
            if not rel:
                skipped.append({"path": name, "reason": "unsafe path"})
                continue
            if not restore_config and Path(rel).name in {"AGENTS.md", "AGENTS.override.md", "config.toml", "hooks.json"}:
                skipped.append({"path": rel, "reason": "config restore disabled"})
                continue
            data = zf.read(name)
            dirname = shlex.quote(str(Path(rel).parent).replace("\\", "/"))
            target = shlex.quote(rel)
            mkdir = "" if dirname == "'.'" else f"mkdir -p {dirname} && "
            code, _, err = _wsl_sh(distro, f"mkdir -p ~/.codex && cd ~/.codex && {mkdir}cat > {target}", input_bytes=data, timeout=120)
            if code == 0:
                restored.append(rel)
            else:
                skipped.append({"path": rel, "reason": err or "write failed"})
    return {
        "success": True,
        "distro": distro,
        "backup_id": confirm_backup_id,
        "restored_count": len(restored),
        "restored": restored,
        "skipped": skipped,
        "preflight_backup": preflight,
    }


def restore_latest_wsl_full_backup(distro: str, restore_config: bool = False, config: AppConfig | None = None) -> dict[str, Any]:
    archive = latest_wsl_backup(distro)
    if archive is None:
        return {"success": False, "error": "No WSL full backup found", "distro": distro}
    config = config or AppConfig()
    with open_full_backup_zip(config, archive) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    backup_id = str(manifest.get("id") or "")
    return restore_wsl_full_backup(distro, archive, confirm_backup_id=backup_id, restore_config=restore_config, config=config)


def import_conversations_to_wsl(
    distro: str,
    archive: str | Path,
    thread_ids: list[str] | None,
    target_provider: str | None = None,
    close_running: bool = False,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    from .backup_import import _rewrite_rollout_meta, _zip_rollout_entry, _zip_state_entry

    if not distro:
        return {"success": False, "error": "distro is required"}
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    ids = [tid for tid in (thread_ids or []) if tid]
    if not ids:
        return {"success": False, "error": "未指定要导入的对话"}
    target = target_provider or _wsl_current_provider(distro)
    if not target:
        return {"success": False, "error": "无法确定目标渠道，请显式指定"}

    guard = _ensure_wsl_codex_closed(distro, close_running)
    if guard is not None:
        return guard

    state_rel = _wsl_state_db_rel(distro)
    if not state_rel:
        return {"success": False, "error": f"未找到 {distro} 的 WSL Codex 状态数据库"}
    target_state, err = _read_wsl_file(distro, state_rel)
    if err or target_state is None:
        return {"success": False, "error": err or f"读取 {state_rel} 失败"}

    config = config or AppConfig()
    preflight = create_wsl_full_backup(distro, include_config=True, include_memories=True, config=config)
    home = _wsl_codex_home(distro)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    target_tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    target_tmp.write(target_state)
    target_tmp.close()

    with open_full_backup_zip(config, archive_path) as zf:
        entry = _zip_state_entry(zf)
        if not entry:
            Path(target_tmp.name).unlink(missing_ok=True)
            return {"success": False, "error": "备份包内未找到 state_*.sqlite"}
        names = set(zf.namelist())
        source_tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        source_tmp.write(zf.read(entry))
        source_tmp.close()
        scon = sqlite3.connect(source_tmp.name)
        scon.row_factory = sqlite3.Row
        dcon = sqlite3.connect(target_tmp.name)
        try:
            local_cols = [row[1] for row in dcon.execute("PRAGMA table_info(threads)")]
            zip_cols = [row[1] for row in scon.execute("PRAGMA table_info(threads)")]
            usable = [col for col in zip_cols if col in local_cols]
            local_ua = {row[0]: (row[1] or 0) for row in dcon.execute("SELECT id, updated_at FROM threads")}
            for tid in ids:
                srow = scon.execute(f"SELECT {','.join(usable)} FROM threads WHERE id=?", (tid,)).fetchone()
                if srow is None:
                    skipped.append({"id": tid, "reason": "not_in_backup"})
                    continue
                src = {key: srow[key] for key in srow.keys()}
                if tid in local_ua:
                    if (src.get("updated_at") or 0) > local_ua[tid]:
                        new_id = str(uuid.uuid4())
                    else:
                        skipped.append({"id": tid, "reason": "already_present"})
                        continue
                else:
                    new_id = tid
                rentry = _zip_rollout_entry(names, src.get("rollout_path"))
                if not rentry:
                    skipped.append({"id": tid, "reason": "no_rollout_in_backup"})
                    continue
                rel = Path(rentry[len("codex/"):])
                fname = rel.name
                if new_id != tid:
                    fname = fname.replace(tid, new_id) if tid in fname else f"rollout-imported-{new_id}.jsonl"
                dest_rel = (rel.parent / fname).as_posix()
                rollout_data = _rewrite_rollout_meta(zf.read(rentry), new_id, target)
                dirname = shlex.quote(str(Path(dest_rel).parent).replace("\\", "/"))
                target_name = shlex.quote(dest_rel)
                mkdir = "" if dirname == "'.'" else f"mkdir -p {dirname} && "
                code, _, write_err = _wsl_sh(distro, f"mkdir -p ~/.codex && cd ~/.codex && {mkdir}cat > {target_name}", input_bytes=rollout_data, timeout=120)
                if code != 0:
                    skipped.append({"id": tid, "reason": write_err or "rollout_write_failed"})
                    continue
                src["id"] = new_id
                src["rollout_path"] = f"{home}/{dest_rel}"
                src["model_provider"] = target
                cols = list(src.keys())
                dcon.execute(
                    f"INSERT OR REPLACE INTO threads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [src[col] for col in cols],
                )
                imported.append({"source_id": tid, "new_id": new_id, "copy": new_id != tid, "to": target, "title": src.get("title")})
            dcon.commit()
        finally:
            scon.close()
            dcon.close()
            Path(source_tmp.name).unlink(missing_ok=True)

    new_state = Path(target_tmp.name).read_bytes()
    Path(target_tmp.name).unlink(missing_ok=True)
    code, _, write_err = _wsl_sh(
        distro,
        f"mkdir -p ~/.codex && cat > ~/.codex/{shlex.quote(state_rel)} && rm -f ~/.codex/{shlex.quote(state_rel)}-wal ~/.codex/{shlex.quote(state_rel)}-shm",
        input_bytes=new_state,
        timeout=120,
    )
    if code != 0:
        return {"success": False, "error": write_err or f"写回 {distro} 状态库失败", "preflight_backup": preflight}

    return {
        "success": True,
        "target_home": f"wsl:{distro}",
        "distro": distro,
        "imported": len(imported),
        "skipped": skipped,
        "items": imported,
        "target": target,
        "preflight_backup": preflight,
    }
