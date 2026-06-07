"""对话浏览器：浏览 / 查看正文 / 导出（纯标准库，复用 codex_channels）。

数据源：~/.codex/state_*.sqlite 的 threads 表（每行一个对话）+ 每个对话的 rollout .jsonl 正文。
只读为主：list/read/export 不改 Codex 数据。按对话的渠道合并/还原走 codex_channels.merge_threads/restore_threads。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .codex_channels import _read_merge_state, codex_state_db, current_codex_provider
from .paths import app_dir, ensure_app_dirs
from .util import safe_filename


# ---------- 列表 ----------

def list_conversations(
    search: str = "",
    cwd: str = "",
    provider: str = "",
    include_archived: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    merge_state = _read_merge_state()
    con = sqlite3.connect(db)
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
        conversations: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            info = merge_state.get(r["id"])
            item["merged"] = bool(info)
            item["original_provider"] = info.get("original_provider") if info else None
            conversations.append(item)
        cwds = [x[0] for x in con.execute("SELECT DISTINCT cwd FROM threads WHERE cwd IS NOT NULL AND cwd<>'' ORDER BY cwd")]
        providers = [x[0] for x in con.execute("SELECT DISTINCT model_provider FROM threads WHERE model_provider IS NOT NULL AND model_provider<>'' ORDER BY model_provider")]
    finally:
        con.close()

    return {
        "success": True,
        "conversations": conversations,
        "count": len(conversations),
        "cwds": cwds,
        "providers": providers,
        "current_provider": current_codex_provider(),
    }


# ---------- 正文解析 ----------

def _content_text(content: Any) -> str:
    """把 message.content（[{type:input_text/output_text, text}]）拼成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


def _reasoning_text(payload: dict[str, Any]) -> str:
    for key in ("summary", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            text = _content_text(value)
            if text:
                return text
        elif isinstance(value, str) and value:
            return value
    return ""


def _any_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def read_conversation(
    thread_id: str,
    include_tools: bool = True,
    include_reasoning: bool = False,
    include_developer: bool = False,
    max_chars: int = 200000,
) -> dict[str, Any]:
    """解析对话 rollout，按序产出消息：user/assistant 文本（developer 系统注入默认排除），可选 reasoning/tool。"""
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id,title,model_provider,cwd,created_at,updated_at,rollout_path FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {"success": False, "error": "未找到该对话"}
    thread = dict(row)
    rollout = Path(row["rollout_path"]) if row["rollout_path"] else None
    if not rollout or not rollout.exists():
        return {"success": False, "error": f"对话正文文件不存在：{row['rollout_path']}", "thread": thread}

    messages: list[dict[str, Any]] = []
    total = 0
    truncated = False
    with rollout.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
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
                        continue  # Codex 框架注入的 permissions/mode 说明，默认排除
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


# ---------- 导出 ----------

def exports_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


_ROLE_LABEL = {
    "user": "用户",
    "assistant": "助手",
    "developer": "系统注入",
    "reasoning": "思考",
    "tool_call": "工具调用",
    "tool_output": "工具输出",
}


def export_conversation(
    thread_id: str,
    fmt: str = "markdown",
    include_tools: bool = False,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    data = read_conversation(thread_id, include_tools=include_tools, include_reasoning=include_reasoning)
    if not data.get("success"):
        return data
    thread = data["thread"]
    messages = data["messages"]
    title = thread.get("title") or thread_id
    stem = f"{safe_filename(title)[:60]}-{str(thread_id)[:8]}"

    if fmt == "json":
        path = exports_dir() / f"{stem}.json"
        path.write_text(json.dumps({"thread": thread, "messages": messages}, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        lines = [
            f"# {title}",
            "",
            f"- 对话 ID：{thread_id}",
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

    return {"success": True, "path": str(path), "fmt": fmt, "message_count": len(messages), "truncated": data.get("truncated", False)}
