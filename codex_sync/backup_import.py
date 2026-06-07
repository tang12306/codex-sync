"""阶段 2：从完整备份 zip「选择性导入对话」到本机（选哪些 + 接入哪个渠道）。

服务器侧对话只在 full_backups 的 zip（含 codex/state_*.sqlite 的 threads + codex/sessions/**/rollout-*.jsonl）。
导入 = 把完整 thread + rollout 落到本机，Codex 打开即可 resume 续聊（不手写 thread_spawn_edges，
派生/接续交给 Codex 原生）。冲突(同 id)：本机已有且不更新→跳过；服务器版更新→导入为新副本(新 id)，不覆盖本机。
写库/文件复用 codex_channels 的 Codex 关闭守卫 + 灾难/state 备份。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .codex_channels import _backup_state_db, _ensure_codex_closed, codex_state_db, current_codex_provider
from .config import AppConfig
from .conversations import _any_text, _content_text, _reasoning_text
from .disaster_backup import create_disaster_backup
from .full_backup import download_full_backup, downloads_dir, full_backup_dir, list_full_backups
from .paths import app_dir, codex_home
from .util import safe_filename


def _zip_state_entry(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        if name.startswith("codex/") and name.endswith(".sqlite") and "state_" in Path(name).name:
            return name
    return None


def _zip_rollout_entry(names: set[str], src_rollout_path: Any) -> str | None:
    """把源机 rollout 绝对路径映射成 zip 内条目 codex/sessions/...。"""
    if not src_rollout_path:
        return None
    norm = str(src_rollout_path).replace("\\", "/")
    idx = norm.find("sessions/")
    if idx < 0:
        return None
    entry = "codex/" + norm[idx:]
    return entry if entry in names else None


# ---------- 备份包发现 ----------

def _local_backup_roots() -> list[Path]:
    roots = [full_backup_dir(), downloads_dir()]
    wsl_root = app_dir() / "wsl"
    if wsl_root.exists():
        roots.append(wsl_root)
    return roots


def list_importable_backups(config: AppConfig) -> dict[str, Any]:
    remote = list_full_backups(config)
    local_zips = sorted({z for root in _local_backup_roots() for z in root.rglob("*.zip")})
    local_names = [z.name for z in local_zips]
    backups: list[dict[str, Any]] = []
    seen: set[str] = set()
    if remote.get("success"):
        for b in remote.get("full_backups", []) or []:
            bid = b.get("id")
            if bid:
                seen.add(str(bid))
            backups.append(
                {
                    "backup_id": bid,
                    "device_id": b.get("device_id"),
                    "created_at": b.get("created_at"),
                    "size_bytes": b.get("size_bytes"),
                    "downloaded": bool(bid and any(str(bid) in n for n in local_names)),
                    "source": "server",
                }
            )
    for archive in sorted(local_zips, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with zipfile.ZipFile(archive) as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        bid = str(manifest.get("id") or "")
        if not bid or bid in seen:
            continue
        seen.add(bid)
        backups.append(
            {
                "backup_id": bid,
                "device_id": manifest.get("device_id"),
                "created_at": manifest.get("created_at"),
                "size_bytes": archive.stat().st_size,
                "downloaded": True,
                "source": manifest.get("source") or "local",
                "archive": str(archive),
            }
        )
    return {
        "success": True,
        "backups": backups,
        "remote_error": None if remote.get("success") else remote.get("error"),
        "local_zip_count": len(local_zips),
    }


def _find_local_archive(backup_id: str) -> Path | None:
    cand = downloads_dir() / f"{safe_filename(backup_id)}.zip"
    if cand.exists():
        return cand
    for folder in _local_backup_roots():
        for z in folder.rglob("*.zip"):
            if str(backup_id) in z.name:
                return z
            try:
                with zipfile.ZipFile(z) as zf:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                if str(manifest.get("id") or "") == str(backup_id):
                    return z
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                continue
    return None


def _ensure_local_archive(config: AppConfig, backup_id: str) -> tuple[Path | None, str | None]:
    found = _find_local_archive(backup_id)
    if found:
        return found, None
    res = download_full_backup(config, backup_id)
    if not res.get("success"):
        return None, res.get("error", "下载备份包失败")
    return Path(res["path"]), None


# ---------- 浏览备份内对话 ----------

def _local_threads_index() -> dict[str, int]:
    try:
        db = codex_state_db()
    except FileNotFoundError:
        return {}
    con = sqlite3.connect(db)
    try:
        return {r[0]: (r[1] or 0) for r in con.execute("SELECT id, updated_at FROM threads")}
    finally:
        con.close()


def list_backup_conversations(archive: str | Path, local_index: dict[str, int] | None = None, current_provider: str | None = None) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.exists():
        return {"success": False, "error": f"备份包不存在：{archive}"}
    local = local_index if local_index is not None else _local_threads_index()
    with zipfile.ZipFile(archive) as zf:
        entry = _zip_state_entry(zf)
        if not entry:
            return {"success": False, "error": "备份包内未找到 state_*.sqlite"}
        names = set(zf.namelist())
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.write(zf.read(entry))
        tmp.close()
    convos: list[dict[str, Any]] = []
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute(
            "SELECT id,title,preview,first_user_message,cwd,model_provider,created_at,updated_at,rollout_path,"
            "COALESCE(archived,0) AS archived FROM threads ORDER BY updated_at DESC"
        ):
            item = {k: r[k] for k in r.keys() if k != "rollout_path"}
            present = r["id"] in local
            item["present"] = present
            item["newer"] = bool(present and (r["updated_at"] or 0) > local.get(r["id"], 0))
            item["has_body"] = _zip_rollout_entry(names, r["rollout_path"]) is not None
            convos.append(item)
    finally:
        con.close()
        Path(tmp.name).unlink(missing_ok=True)
    return {"success": True, "archive": str(archive), "conversations": convos, "current_provider": current_provider or current_codex_provider()}


def _parse_rollout_text(text: str, include_tools: bool, include_reasoning: bool, include_developer: bool, max_chars: int) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for line in text.splitlines():
        line = line.strip()
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
            text_part = _content_text(payload.get("content"))
            messages.append({"role": role, "text": text_part})
            total += len(text_part)
        elif include_reasoning and kind == "response_item" and ptype == "reasoning":
            text_part = _reasoning_text(payload)
            if text_part:
                messages.append({"role": "reasoning", "text": text_part})
                total += len(text_part)
        elif include_tools and kind == "response_item" and ptype == "function_call":
            messages.append({"role": "tool_call", "text": f"{payload.get('name') or 'tool'} {_any_text(payload.get('arguments'))[:2000]}".strip()})
        elif include_tools and kind == "response_item" and ptype == "function_call_output":
            messages.append({"role": "tool_output", "text": _any_text(payload.get("output"))[:2000]})
        if total > max_chars:
            truncated = True
            break
    return {"messages": messages, "truncated": truncated}


def read_backup_conversation(
    archive: str | Path,
    thread_id: str,
    include_tools: bool = False,
    include_reasoning: bool = False,
    include_developer: bool = False,
    max_chars: int = 200000,
) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.exists():
        return {"success": False, "error": f"备份包不存在：{archive}"}
    with zipfile.ZipFile(archive) as zf:
        entry = _zip_state_entry(zf)
        if not entry:
            return {"success": False, "error": "备份包内无 state db"}
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.write(zf.read(entry))
        tmp.close()
        con = sqlite3.connect(tmp.name)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT id,title,model_provider,cwd,rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
        finally:
            con.close()
            Path(tmp.name).unlink(missing_ok=True)
        if row is None:
            return {"success": False, "error": "备份内未找到该对话"}
        thread = {k: row[k] for k in row.keys() if k != "rollout_path"}
        rentry = _zip_rollout_entry(set(zf.namelist()), row["rollout_path"])
        if not rentry:
            return {"success": False, "error": "备份内无该对话正文", "thread": thread}
        raw = zf.read(rentry).decode("utf-8", errors="replace")
    parsed = _parse_rollout_text(raw, include_tools, include_reasoning, include_developer, max_chars)
    return {"success": True, "thread": thread, "messages": parsed["messages"], "truncated": parsed["truncated"], "message_count": len(parsed["messages"])}


# ---------- 导入 ----------

def _rewrite_rollout_meta(raw: bytes, new_id: str, provider: str) -> bytes:
    """改 rollout 首行 session_meta 的 id 与 model_provider（导入到本机/目标渠道）。"""
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return raw
    try:
        first = json.loads(lines[0])
        if first.get("type") == "session_meta" and isinstance(first.get("payload"), dict):
            first["payload"]["id"] = new_id
            first["payload"]["model_provider"] = provider
            lines[0] = json.dumps(first, ensure_ascii=False)
    except ValueError:
        pass
    return ("\n".join(lines) + "\n").encode("utf-8")


def import_conversations(
    config: AppConfig,
    archive: str | Path,
    thread_ids: list[str] | None,
    target_provider: str | None = None,
    close_running: bool = False,
) -> dict[str, Any]:
    archive = Path(archive)
    if not archive.exists():
        return {"success": False, "error": f"备份包不存在：{archive}"}
    target = target_provider or current_codex_provider()
    if not target:
        return {"success": False, "error": "无法确定目标渠道，请指定"}
    ids = [t for t in (thread_ids or []) if t]
    if not ids:
        return {"success": False, "error": "未指定要导入的对话"}
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    guard = _ensure_codex_closed(close_running)
    if guard is not None:
        return guard

    protection = create_disaster_backup(config, reason="before_conversation_import", force=True)
    db_backup = _backup_state_db(db)

    con = sqlite3.connect(db)
    local_cols = [d[1] for d in con.execute("PRAGMA table_info(threads)")]
    local_ua = {r[0]: (r[1] or 0) for r in con.execute("SELECT id, updated_at FROM threads")}
    con.close()

    home = codex_home()
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with zipfile.ZipFile(archive) as zf:
        entry = _zip_state_entry(zf)
        if not entry:
            return {"success": False, "error": "备份包内无 state db"}
        names = set(zf.namelist())
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp.write(zf.read(entry))
        tmp.close()
        scon = sqlite3.connect(tmp.name)
        scon.row_factory = sqlite3.Row
        dcon = sqlite3.connect(db)
        try:
            zip_cols = [d[1] for d in scon.execute("PRAGMA table_info(threads)")]
            usable = [c for c in zip_cols if c in local_cols]
            for tid in ids:
                srow = scon.execute(f"SELECT {','.join(usable)} FROM threads WHERE id=?", (tid,)).fetchone()
                if srow is None:
                    skipped.append({"id": tid, "reason": "not_in_backup"})
                    continue
                src = {k: srow[k] for k in srow.keys()}
                if tid in local_ua:
                    if (src.get("updated_at") or 0) > local_ua[tid]:
                        new_id = str(uuid.uuid4())  # 服务器版更新 → 新副本，不覆盖本机
                    else:
                        skipped.append({"id": tid, "reason": "already_present"})
                        continue
                else:
                    new_id = tid
                rentry = _zip_rollout_entry(names, src.get("rollout_path"))
                if not rentry:
                    skipped.append({"id": tid, "reason": "no_rollout_in_backup"})
                    continue
                rel = Path(rentry[len("codex/"):])  # sessions/YYYY/MM/DD/rollout-*.jsonl
                fname = rel.name
                if new_id != tid:
                    fname = fname.replace(tid, new_id) if tid in fname else f"rollout-imported-{new_id}.jsonl"
                dest = home / rel.parent / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(_rewrite_rollout_meta(zf.read(rentry), new_id, target))
                src["id"] = new_id
                src["rollout_path"] = str(dest)
                src["model_provider"] = target
                cols = list(src.keys())
                dcon.execute(
                    f"INSERT OR REPLACE INTO threads ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [src[c] for c in cols],
                )
                imported.append({"source_id": tid, "new_id": new_id, "copy": new_id != tid, "to": target, "title": src.get("title")})
            dcon.commit()
        finally:
            scon.close()
            dcon.close()
            Path(tmp.name).unlink(missing_ok=True)

    return {
        "success": True,
        "imported": len(imported),
        "skipped": skipped,
        "items": imported,
        "target": target,
        "preflight_backup": protection,
        "db_backup": db_backup,
    }
