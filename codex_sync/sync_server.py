from __future__ import annotations

import json
import os
import base64
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


HOST = os.environ.get("CODEX_SYNC_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_SYNC_SERVER_PORT", "8888"))
DATA_DIR = Path(os.environ.get("CODEX_SYNC_DATA_DIR", Path.home() / "codex_snapshots")).resolve()
DB_PATH = Path(os.environ.get("CODEX_SYNC_DB_PATH", DATA_DIR / "snapshots.sqlite3")).resolve()
TOKEN_FILE = Path(os.environ.get("CODEX_SYNC_SERVER_TOKEN_FILE", Path.home() / ".codex-sync-server-token"))
MAX_BODY_BYTES = int(os.environ.get("CODEX_SYNC_MAX_BODY_BYTES", str(20 * 1024 * 1024)))
FULL_BACKUP_MAX_BODY_BYTES = int(os.environ.get("CODEX_SYNC_FULL_BACKUP_MAX_BODY_BYTES", str(512 * 1024 * 1024)))
PROJECT_BACKUP_MAX_BODY_BYTES = int(os.environ.get("CODEX_SYNC_PROJECT_BACKUP_MAX_BODY_BYTES", str(128 * 1024 * 1024)))
ALLOWED_ORIGIN = os.environ.get("CODEX_SYNC_ALLOWED_ORIGIN", "")
RETENTION_FULL_BACKUP_KEEP = int(os.environ.get("CODEX_SYNC_RETENTION_FULL_BACKUP_KEEP", "10"))
RETENTION_FULL_BACKUP_DAYS = int(os.environ.get("CODEX_SYNC_RETENTION_FULL_BACKUP_DAYS", "30"))
RETENTION_PROJECT_BACKUP_KEEP = int(os.environ.get("CODEX_SYNC_RETENTION_PROJECT_BACKUP_KEEP", "20"))
RETENTION_PROJECT_BACKUP_DAYS = int(os.environ.get("CODEX_SYNC_RETENTION_PROJECT_BACKUP_DAYS", "90"))
RETENTION_SNAPSHOT_KEEP = int(os.environ.get("CODEX_SYNC_RETENTION_SNAPSHOT_KEEP", "100"))
RETENTION_SNAPSHOT_DAYS = int(os.environ.get("CODEX_SYNC_RETENTION_SNAPSHOT_DAYS", "14"))
RETENTION_MAX_BYTES = int(os.environ.get("CODEX_SYNC_RETENTION_MAX_BYTES", str(20 * 1024 * 1024 * 1024)))
SERVER_VERSION = "0.1.3"
SERVER_API_VERSION = 4
SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
SERVER_FEATURES = {
    "snapshots": True,
    "device_state": True,
    "devices": True,
    "changes": True,
    "full_backups": True,
    "project_backups": True,
    "retention": True,
    "wsl_full_backups": True,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_not_after(first: Any, second: Any) -> bool:
    first_dt = parse_utc(first)
    second_dt = parse_utc(second)
    if first_dt is None or second_dt is None:
        return False
    return first_dt <= second_dt


def safe_filename(value: Any, fallback: str = "unknown", max_len: int = 120) -> str:
    raw = str(value or "").strip()
    clean = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in raw)
    while ".." in clean:
        clean = clean.replace("..", "_")
    clean = clean.strip("._")
    if not clean or clean in {".", ".."}:
        clean = fallback
    return clean[:max_len]


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_or_create_token() -> tuple[str, str]:
    env_token = os.environ.get("CODEX_SYNC_SERVER_TOKEN")
    if env_token:
        return env_token, "environment variable CODEX_SYNC_SERVER_TOKEN"

    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token, str(TOKEN_FILE)

    token = secrets.token_urlsafe(32)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    return token, str(TOKEN_FILE)


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not is_within(DB_PATH, DATA_DIR):
        raise RuntimeError("CODEX_SYNC_DB_PATH must stay inside CODEX_SYNC_DATA_DIR")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                created_at TEXT,
                received_at TEXT NOT NULL,
                cwd TEXT,
                type TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_received_at ON snapshots(received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_device_received ON snapshots(device_id, received_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS full_backups (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                created_at TEXT,
                received_at TEXT NOT NULL,
                content_digest TEXT,
                artifact_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                encrypted INTEGER NOT NULL,
                format_version INTEGER NOT NULL,
                artifact_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_full_backups_received_at ON full_backups(received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_full_backups_device_received ON full_backups(device_id, received_at DESC)")
        _ensure_column(conn, "full_backups", "branch_id", "TEXT")
        _ensure_column(conn, "full_backups", "parent_backup_id", "TEXT")
        _ensure_column(conn, "full_backups", "sync_state", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_full_backups_branch_received ON full_backups(branch_id, received_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_backups (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                repo_root TEXT,
                branch TEXT,
                commit_sha TEXT,
                created_at TEXT,
                received_at TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                artifact_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_backups_received_at ON project_backups(received_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project_backups_repo_received ON project_backups(repo_name, received_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_states (
                device_id TEXT PRIMARY KEY,
                dirty INTEGER NOT NULL,
                sync_state TEXT NOT NULL,
                branch_id TEXT,
                head_backup_id TEXT,
                head_content_digest TEXT,
                last_change_at TEXT,
                last_backup_at TEXT,
                last_event_id TEXT,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def normalize_snapshot_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    snapshot_id = str(payload.get("id") or secrets.token_hex(16))
    device_id = str(payload.get("device_id") or "unknown_device")
    created_at = str(payload.get("created_at") or "")
    cwd = str(payload.get("cwd") or "")
    snapshot_type = str(payload.get("type") or "")

    payload = dict(payload)
    payload["id"] = snapshot_id
    payload["device_id"] = device_id
    if created_at:
        payload["created_at"] = created_at

    return payload, {
        "id": snapshot_id,
        "device_id": device_id,
        "created_at": created_at,
        "cwd": cwd,
        "type": snapshot_type,
    }


def save_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    payload, meta = normalize_snapshot_payload(payload)
    received_at = utc_now()
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO snapshots (id, device_id, created_at, received_at, cwd, type, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id=excluded.device_id,
                created_at=excluded.created_at,
                received_at=excluded.received_at,
                cwd=excluded.cwd,
                type=excluded.type,
                payload_json=excluded.payload_json
            """,
            (
                meta["id"],
                meta["device_id"],
                meta["created_at"],
                received_at,
                meta["cwd"],
                meta["type"],
                payload_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    retention = prune_retention(dry_run=False)
    return {
        "snapshot_id": meta["id"],
        "device_id": meta["device_id"],
        "received_at": received_at,
        "retention": {"deleted_count": retention.get("deleted_count", 0), "deleted_bytes": retention.get("deleted_bytes", 0)},
    }


def list_snapshots(limit: int = 50, offset: int = 0, device_id: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    params: list[Any] = []
    where = ""
    if device_id:
        where = "WHERE device_id = ?"
        params.append(device_id)
    params.extend([limit, offset])
    conn = connect_db()
    try:
        rows = conn.execute(
            f"""
            SELECT id, device_id, created_at, received_at, cwd, type
            FROM snapshots
            {where}
            ORDER BY received_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    conn = connect_db()
    try:
        row = conn.execute("SELECT payload_json FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    data = json.loads(row["payload_json"])
    return data if isinstance(data, dict) else None


def _device_state_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        data["metadata"] = {}
    data["dirty"] = bool(data.get("dirty"))
    return data


def get_device_state(device_id: str) -> dict[str, Any] | None:
    conn = connect_db()
    try:
        row = conn.execute("SELECT * FROM device_states WHERE device_id = ?", (device_id,)).fetchone()
    finally:
        conn.close()
    return _device_state_row_to_dict(row) if row else None


def list_device_states(limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT * FROM device_states ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return [_device_state_row_to_dict(row) for row in rows]


def save_change_notification(payload: dict[str, Any]) -> dict[str, Any]:
    device_id = str(payload.get("device_id") or "")
    if not device_id:
        raise ValueError("device_id is required")
    updated_at = utc_now()
    changed_at = str(payload.get("changed_at") or updated_at)
    branch_id = str(payload.get("branch_id") or f"{device_id}:main")
    event_id = str(payload.get("event_id") or secrets.token_hex(16))
    prior = get_device_state(device_id) or {}
    content_digest = str(payload.get("content_digest") or "")
    head_content_digest = str(prior.get("head_content_digest") or "")
    stale_change = bool(prior.get("last_backup_at") and is_not_after(changed_at, prior.get("last_backup_at")))
    covered_by_head = bool(
        prior.get("head_backup_id")
        and (
            (content_digest and content_digest == head_content_digest)
            or (stale_change and not content_digest)
        )
    )
    metadata = {
        "event": payload.get("event"),
        "reason": payload.get("reason"),
        "content_digest": content_digest or None,
        "parent_backup_id": payload.get("parent_backup_id"),
        "cwd": payload.get("cwd"),
        "covered_by_head": covered_by_head,
        "stale_change": stale_change,
    }
    if covered_by_head:
        dirty = 0
        sync_state = "diverged" if prior.get("sync_state") == "diverged" else "clean"
    else:
        dirty = 1
        sync_state = "dirty" if prior.get("sync_state") != "diverged" else "diverged"
    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO device_states (
                device_id, dirty, sync_state, branch_id, head_backup_id, head_content_digest,
                last_change_at, last_backup_at, last_event_id, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                dirty=excluded.dirty,
                sync_state=excluded.sync_state,
                branch_id=excluded.branch_id,
                last_change_at=excluded.last_change_at,
                last_event_id=excluded.last_event_id,
                updated_at=excluded.updated_at,
                metadata_json=excluded.metadata_json
            """,
            (
                device_id,
                dirty,
                sync_state,
                branch_id,
                prior.get("head_backup_id"),
                prior.get("head_content_digest"),
                changed_at,
                prior.get("last_backup_at"),
                event_id,
                updated_at,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "device_id": device_id, "event_id": event_id, "device_state": get_device_state(device_id)}


def mark_backup_complete(
    device_id: str,
    backup_id: str,
    branch_id: str,
    content_digest: str,
    parent_backup_id: str,
    received_at: str,
) -> dict[str, Any] | None:
    prior = get_device_state(device_id) or {}
    prior_head = str(prior.get("head_backup_id") or "")
    diverged = bool(prior_head and backup_id != prior_head and parent_backup_id != prior_head)
    sync_state = "diverged" if diverged else "clean"
    metadata = {
        "parent_backup_id": parent_backup_id,
        "previous_head_backup_id": prior_head,
        "diverged": diverged,
    }
    updated_at = utc_now()
    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO device_states (
                device_id, dirty, sync_state, branch_id, head_backup_id, head_content_digest,
                last_change_at, last_backup_at, last_event_id, updated_at, metadata_json
            )
            VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                dirty=0,
                sync_state=excluded.sync_state,
                branch_id=excluded.branch_id,
                head_backup_id=excluded.head_backup_id,
                head_content_digest=excluded.head_content_digest,
                last_backup_at=excluded.last_backup_at,
                updated_at=excluded.updated_at,
                metadata_json=excluded.metadata_json
            """,
            (
                device_id,
                sync_state,
                branch_id,
                backup_id,
                content_digest,
                prior.get("last_change_at"),
                received_at,
                prior.get("last_event_id"),
                updated_at,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_device_state(device_id)


def full_backups_dir() -> Path:
    path = DATA_DIR / "full_backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_backups_dir() -> Path:
    path = DATA_DIR / "project_backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def retention_policy() -> dict[str, Any]:
    return {
        "full_backups": {"keep_per_device_branch": RETENTION_FULL_BACKUP_KEEP, "max_age_days": RETENTION_FULL_BACKUP_DAYS},
        "project_backups": {"keep_per_repo_device": RETENTION_PROJECT_BACKUP_KEEP, "max_age_days": RETENTION_PROJECT_BACKUP_DAYS},
        "snapshots": {"keep_per_device": RETENTION_SNAPSHOT_KEEP, "max_age_days": RETENTION_SNAPSHOT_DAYS},
        "server_total_max_bytes": RETENTION_MAX_BYTES,
    }


def version_info() -> dict[str, Any]:
    return {
        "success": True,
        "server_version": SERVER_VERSION,
        "api_version": SERVER_API_VERSION,
        "features": SERVER_FEATURES,
        "started_at": SERVER_STARTED_AT,
        "retention_policy": retention_policy(),
    }


def _row_time(row: dict[str, Any]) -> datetime:
    parsed = parse_utc(row.get("received_at") or row.get("created_at"))
    return parsed or datetime.fromtimestamp(0, timezone.utc)


def _cutoff(days: int) -> datetime | None:
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _query_rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = connect_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _full_backup_rows() -> list[dict[str, Any]]:
    return _query_rows(
        """
        SELECT id, device_id, created_at, received_at, content_digest, artifact_sha256,
               size_bytes, encrypted, format_version, artifact_name, metadata_json,
               branch_id, parent_backup_id, sync_state
        FROM full_backups
        ORDER BY received_at DESC
        """
    )


def _project_backup_rows() -> list[dict[str, Any]]:
    return _query_rows(
        """
        SELECT id, device_id, repo_name, repo_root, branch, commit_sha, created_at,
               received_at, artifact_sha256, size_bytes, artifact_name, metadata_json
        FROM project_backups
        ORDER BY received_at DESC
        """
    )


def _snapshot_rows() -> list[dict[str, Any]]:
    return _query_rows(
        """
        SELECT id, device_id, created_at, received_at, cwd, type
        FROM snapshots
        ORDER BY received_at DESC
        """
    )


def _current_head_ids() -> set[str]:
    rows = _query_rows("SELECT head_backup_id FROM device_states WHERE head_backup_id IS NOT NULL AND head_backup_id != ''")
    return {str(row["head_backup_id"]) for row in rows if row.get("head_backup_id")}


def _backup_artifact_path(kind: str, artifact_name: str) -> Path:
    root = full_backups_dir() if kind == "full_backup" else project_backups_dir()
    return root / str(artifact_name)


def _add_plan(plan: dict[tuple[str, str], dict[str, Any]], item: dict[str, Any], reason: str) -> None:
    key = (str(item["kind"]), str(item["id"]))
    existing = plan.get(key)
    if existing:
        reasons = existing.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        return
    copied = dict(item)
    copied["reasons"] = [reason]
    plan[key] = copied


def _group_rows(rows: list[dict[str, Any]], key_fn) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)
    for group_rows in groups.values():
        group_rows.sort(key=_row_time, reverse=True)
    return groups


def _backup_item(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    size = int(row.get("size_bytes") or 0)
    item = {
        "kind": kind,
        "id": str(row.get("id") or ""),
        "device_id": row.get("device_id"),
        "received_at": row.get("received_at"),
        "created_at": row.get("created_at"),
        "size_bytes": size,
        "artifact_name": row.get("artifact_name"),
    }
    if kind == "full_backup":
        item["branch_id"] = row.get("branch_id")
    else:
        item["repo_name"] = row.get("repo_name")
    return item


def _snapshot_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "snapshot",
        "id": str(row.get("id") or ""),
        "device_id": row.get("device_id"),
        "received_at": row.get("received_at"),
        "created_at": row.get("created_at"),
        "size_bytes": 0,
    }


def _build_retention_plan() -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    full_rows = _full_backup_rows()
    project_rows = _project_backup_rows()
    snapshot_rows = _snapshot_rows()
    warnings: list[str] = []
    plan: dict[tuple[str, str], dict[str, Any]] = {}

    full_groups = _group_rows(full_rows, lambda row: (row.get("device_id") or "", row.get("branch_id") or f"{row.get('device_id')}:main"))
    project_groups = _group_rows(project_rows, lambda row: (row.get("device_id") or "", row.get("repo_name") or "unknown_repo"))
    snapshot_groups = _group_rows(snapshot_rows, lambda row: (row.get("device_id") or "unknown_device",))

    protected: set[tuple[str, str]] = set()
    for backup_id in _current_head_ids():
        protected.add(("full_backup", backup_id))
    for rows in full_groups.values():
        if rows:
            protected.add(("full_backup", str(rows[0].get("id") or "")))
    for rows in project_groups.values():
        if rows:
            protected.add(("project_backup", str(rows[0].get("id") or "")))
    for rows in snapshot_groups.values():
        if rows:
            protected.add(("snapshot", str(rows[0].get("id") or "")))

    full_cutoff = _cutoff(RETENTION_FULL_BACKUP_DAYS)
    for rows in full_groups.values():
        for index, row in enumerate(rows):
            key = ("full_backup", str(row.get("id") or ""))
            if key in protected:
                continue
            if RETENTION_FULL_BACKUP_KEEP > 0 and index >= RETENTION_FULL_BACKUP_KEEP:
                _add_plan(plan, _backup_item("full_backup", row), "full backup count limit")
            if full_cutoff and _row_time(row) < full_cutoff:
                _add_plan(plan, _backup_item("full_backup", row), "full backup age limit")

    project_cutoff = _cutoff(RETENTION_PROJECT_BACKUP_DAYS)
    for rows in project_groups.values():
        for index, row in enumerate(rows):
            key = ("project_backup", str(row.get("id") or ""))
            if key in protected:
                continue
            if RETENTION_PROJECT_BACKUP_KEEP > 0 and index >= RETENTION_PROJECT_BACKUP_KEEP:
                _add_plan(plan, _backup_item("project_backup", row), "project backup count limit")
            if project_cutoff and _row_time(row) < project_cutoff:
                _add_plan(plan, _backup_item("project_backup", row), "project backup age limit")

    snapshot_cutoff = _cutoff(RETENTION_SNAPSHOT_DAYS)
    for rows in snapshot_groups.values():
        for index, row in enumerate(rows):
            key = ("snapshot", str(row.get("id") or ""))
            if key in protected:
                continue
            if RETENTION_SNAPSHOT_KEEP > 0 and index >= RETENTION_SNAPSHOT_KEEP:
                _add_plan(plan, _snapshot_item(row), "snapshot count limit")
            if snapshot_cutoff and _row_time(row) < snapshot_cutoff:
                _add_plan(plan, _snapshot_item(row), "snapshot age limit")

    backup_rows = [("full_backup", row) for row in full_rows] + [("project_backup", row) for row in project_rows]
    backup_bytes = sum(int(row.get("size_bytes") or 0) for _, row in backup_rows)
    planned_bytes = sum(int(item.get("size_bytes") or 0) for item in plan.values() if item.get("kind") != "snapshot")
    remaining_bytes = max(0, backup_bytes - planned_bytes)
    if RETENTION_MAX_BYTES > 0 and remaining_bytes > RETENTION_MAX_BYTES:
        for kind, row in sorted(backup_rows, key=lambda pair: _row_time(pair[1])):
            key = (kind, str(row.get("id") or ""))
            if key in protected or key in plan:
                continue
            _add_plan(plan, _backup_item(kind, row), "server total size limit")
            remaining_bytes -= int(row.get("size_bytes") or 0)
            if remaining_bytes <= RETENTION_MAX_BYTES:
                break
        if remaining_bytes > RETENTION_MAX_BYTES:
            warnings.append("Retention could not reduce server storage below the total limit without deleting protected latest/head backups.")

    usage = {
        "full_backup_count": len(full_rows),
        "project_backup_count": len(project_rows),
        "snapshot_count": len(snapshot_rows),
        "full_backup_bytes": sum(int(row.get("size_bytes") or 0) for row in full_rows),
        "project_backup_bytes": sum(int(row.get("size_bytes") or 0) for row in project_rows),
        "backup_bytes": backup_bytes,
        "protected_count": len(protected),
    }
    return sorted(plan.values(), key=lambda item: (str(item.get("kind")), str(item.get("received_at") or ""))), usage, warnings


def _delete_retention_item(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    item_id = str(item.get("id") or "")
    result = dict(item)
    if not item_id:
        result["deleted"] = False
        result["error"] = "missing id"
        return result
    conn = connect_db()
    try:
        if kind == "snapshot":
            conn.execute("DELETE FROM snapshots WHERE id = ?", (item_id,))
            conn.commit()
            result["deleted"] = True
            return result
        if kind not in {"full_backup", "project_backup"}:
            result["deleted"] = False
            result["error"] = f"unsupported retention kind: {kind}"
            return result
        artifact_name = str(item.get("artifact_name") or "")
        artifact = _backup_artifact_path(kind, artifact_name)
        root = full_backups_dir() if kind == "full_backup" else project_backups_dir()
        if artifact_name and (not is_within(artifact, root)):
            result["deleted"] = False
            result["error"] = "artifact path escaped storage directory"
            return result
        if artifact_name and artifact.exists():
            artifact.unlink()
            result["artifact_removed"] = True
        else:
            result["artifact_removed"] = False
        table = "full_backups" if kind == "full_backup" else "project_backups"
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        conn.commit()
        result["deleted"] = True
        return result
    except Exception as exc:
        result["deleted"] = False
        result["error"] = str(exc)
        return result
    finally:
        conn.close()


def prune_retention(dry_run: bool = True) -> dict[str, Any]:
    plan, usage, warnings = _build_retention_plan()
    planned_bytes = sum(int(item.get("size_bytes") or 0) for item in plan)
    result: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "policy": retention_policy(),
        "usage": usage,
        "planned_count": len(plan),
        "planned_bytes": planned_bytes,
        "planned": plan,
        "warnings": warnings,
    }
    if dry_run:
        return result
    deleted = [_delete_retention_item(item) for item in plan]
    result["deleted"] = deleted
    result["deleted_count"] = sum(1 for item in deleted if item.get("deleted"))
    result["deleted_bytes"] = sum(int(item.get("size_bytes") or 0) for item in deleted if item.get("deleted"))
    result["success"] = all(item.get("deleted") for item in deleted)
    result["usage_after"] = _build_retention_plan()[1]
    return result


def decode_backup_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        raise ValueError("Missing X-Codex-Backup-Metadata header")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        metadata = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid X-Codex-Backup-Metadata header: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Backup metadata must be a JSON object")
    return metadata


def decode_project_backup_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        raise ValueError("Missing X-Codex-Project-Backup-Metadata header")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        metadata = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid X-Codex-Project-Backup-Metadata header: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Project backup metadata must be a JSON object")
    return metadata


def save_full_backup(metadata: dict[str, Any], source, length: int) -> dict[str, Any]:
    backup_id = str(metadata.get("id") or secrets.token_hex(16))
    device_id = str(metadata.get("device_id") or "unknown_device")
    created_at = str(metadata.get("created_at") or "")
    branch_id = str(metadata.get("branch_id") or f"{device_id}:main")
    parent_backup_id = str(metadata.get("parent_backup_id") or "")
    encrypted = bool(metadata.get("encrypted"))
    format_version = int(metadata.get("format_version") or 1)
    expected_sha256 = str(metadata.get("archive_sha256") or "")
    suffix = ".zip.enc" if encrypted else ".zip"
    filename = f"{safe_filename(backup_id)}{suffix}"
    target = full_backups_dir() / filename
    temp = target.with_suffix(target.suffix + ".tmp")
    if not is_within(target, full_backups_dir()):
        raise RuntimeError("Backup artifact path escaped storage directory")

    digest = __import__("hashlib").sha256()
    remaining = length
    written = 0
    with temp.open("wb") as fh:
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    if written != length:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Incomplete upload: expected {length} bytes, received {written} bytes")

    actual_sha256 = digest.hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        temp.unlink(missing_ok=True)
        raise RuntimeError("Uploaded backup sha256 does not match metadata")

    temp.replace(target)
    received_at = utc_now()
    metadata = dict(metadata)
    metadata.update(
        {
            "id": backup_id,
            "device_id": device_id,
            "branch_id": branch_id,
            "parent_backup_id": parent_backup_id,
            "created_at": created_at,
            "received_at": received_at,
            "artifact_sha256": actual_sha256,
            "size_bytes": written,
            "encrypted": encrypted,
            "format_version": format_version,
        }
    )
    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO full_backups (
                id, device_id, created_at, received_at, content_digest, artifact_sha256,
                size_bytes, encrypted, format_version, artifact_name, metadata_json,
                branch_id, parent_backup_id, sync_state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id=excluded.device_id,
                created_at=excluded.created_at,
                received_at=excluded.received_at,
                content_digest=excluded.content_digest,
                artifact_sha256=excluded.artifact_sha256,
                size_bytes=excluded.size_bytes,
                encrypted=excluded.encrypted,
                format_version=excluded.format_version,
                artifact_name=excluded.artifact_name,
                metadata_json=excluded.metadata_json,
                branch_id=excluded.branch_id,
                parent_backup_id=excluded.parent_backup_id,
                sync_state=excluded.sync_state
            """,
            (
                backup_id,
                device_id,
                created_at,
                received_at,
                str(metadata.get("content_digest") or ""),
                actual_sha256,
                written,
                1 if encrypted else 0,
                format_version,
                filename,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                branch_id,
                parent_backup_id,
                "complete",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    state = mark_backup_complete(device_id, backup_id, branch_id, str(metadata.get("content_digest") or ""), parent_backup_id, received_at)
    retention = prune_retention(dry_run=False)
    return {
        "backup_id": backup_id,
        "device_id": device_id,
        "received_at": received_at,
        "bytes": written,
        "sha256": actual_sha256,
        "branch_id": branch_id,
        "parent_backup_id": parent_backup_id,
        "device_state": state,
        "retention": {"deleted_count": retention.get("deleted_count", 0), "deleted_bytes": retention.get("deleted_bytes", 0)},
    }


def list_full_backups(limit: int = 50, offset: int = 0, device_id: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    params: list[Any] = []
    where = ""
    if device_id:
        where = "WHERE device_id = ?"
        params.append(device_id)
    params.extend([limit, offset])
    conn = connect_db()
    try:
        rows = conn.execute(
            f"""
            SELECT id, device_id, created_at, received_at, content_digest, artifact_sha256,
                   size_bytes, encrypted, format_version, branch_id, parent_backup_id, sync_state
            FROM full_backups
            {where}
            ORDER BY received_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_full_backup(backup_id: str) -> dict[str, Any] | None:
    conn = connect_db()
    try:
        row = conn.execute("SELECT * FROM full_backups WHERE id = ?", (backup_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def save_project_backup(metadata: dict[str, Any], source, length: int) -> dict[str, Any]:
    backup_id = str(metadata.get("id") or secrets.token_hex(16))
    device_id = str(metadata.get("device_id") or "unknown_device")
    repo_name = str(metadata.get("repo_name") or "unknown_repo")
    repo_root = str(metadata.get("repo_root") or "")
    branch = str(metadata.get("branch") or "")
    commit_sha = str(metadata.get("commit") or "")
    created_at = str(metadata.get("created_at") or "")
    expected_sha256 = str(metadata.get("archive_sha256") or "")
    filename = f"{safe_filename(repo_name)}-{safe_filename(backup_id)}.zip"
    target = project_backups_dir() / filename
    temp = target.with_suffix(target.suffix + ".tmp")
    if not is_within(target, project_backups_dir()):
        raise RuntimeError("Project backup artifact path escaped storage directory")

    digest = __import__("hashlib").sha256()
    remaining = length
    written = 0
    with temp.open("wb") as fh:
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    if written != length:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Incomplete upload: expected {length} bytes, received {written} bytes")
    actual_sha256 = digest.hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        temp.unlink(missing_ok=True)
        raise RuntimeError("Uploaded project backup sha256 does not match metadata")

    temp.replace(target)
    received_at = utc_now()
    metadata = dict(metadata)
    metadata.update(
        {
            "id": backup_id,
            "device_id": device_id,
            "repo_name": repo_name,
            "repo_root": repo_root,
            "branch": branch,
            "commit": commit_sha,
            "created_at": created_at,
            "received_at": received_at,
            "artifact_sha256": actual_sha256,
            "size_bytes": written,
        }
    )
    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO project_backups (
                id, device_id, repo_name, repo_root, branch, commit_sha, created_at,
                received_at, artifact_sha256, size_bytes, artifact_name, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id=excluded.device_id,
                repo_name=excluded.repo_name,
                repo_root=excluded.repo_root,
                branch=excluded.branch,
                commit_sha=excluded.commit_sha,
                created_at=excluded.created_at,
                received_at=excluded.received_at,
                artifact_sha256=excluded.artifact_sha256,
                size_bytes=excluded.size_bytes,
                artifact_name=excluded.artifact_name,
                metadata_json=excluded.metadata_json
            """,
            (
                backup_id,
                device_id,
                repo_name,
                repo_root,
                branch,
                commit_sha,
                created_at,
                received_at,
                actual_sha256,
                written,
                filename,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    retention = prune_retention(dry_run=False)
    return {
        "backup_id": backup_id,
        "device_id": device_id,
        "repo_name": repo_name,
        "received_at": received_at,
        "bytes": written,
        "sha256": actual_sha256,
        "retention": {"deleted_count": retention.get("deleted_count", 0), "deleted_bytes": retention.get("deleted_bytes", 0)},
    }


def list_project_backups(limit: int = 50, offset: int = 0, repo_name: str | None = None, device_id: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    filters: list[str] = []
    params: list[Any] = []
    if repo_name:
        filters.append("repo_name = ?")
        params.append(repo_name)
    if device_id:
        filters.append("device_id = ?")
        params.append(device_id)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    params.extend([limit, offset])
    conn = connect_db()
    try:
        rows = conn.execute(
            f"""
            SELECT id, device_id, repo_name, repo_root, branch, commit_sha, created_at,
                   received_at, artifact_sha256, size_bytes
            FROM project_backups
            {where}
            ORDER BY received_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_project_backup(backup_id: str) -> dict[str, Any] | None:
    conn = connect_db()
    try:
        row = conn.execute("SELECT * FROM project_backups WHERE id = ?", (backup_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


class SyncHandler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")
        sys.stdout.flush()

    def check_token(self) -> bool:
        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            self.send_error_response(HTTPStatus.UNAUTHORIZED, "Missing or invalid Authorization header")
            return False
        token_value = auth_header.split(" ", 1)[1].strip()
        if not secrets.compare_digest(token_value, self.token):
            self.send_error_response(HTTPStatus.FORBIDDEN, "Invalid API Token")
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self.check_token():
            return

        if parsed.path == "/api/snapshots":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            device_id = query.get("device_id", [None])[0]
            snapshots = list_snapshots(limit=limit, offset=offset, device_id=device_id)
            self.send_json_response(HTTPStatus.OK, {"success": True, "snapshots": snapshots, "limit": limit, "offset": offset})
            return

        if parsed.path == "/api/device-state":
            query = parse_qs(parsed.query)
            device_id = query.get("device_id", [""])[0]
            if not device_id:
                self.send_error_response(HTTPStatus.BAD_REQUEST, "device_id is required")
                return
            state = get_device_state(device_id)
            self.send_json_response(HTTPStatus.OK, {"success": True, "device_state": state})
            return

        if parsed.path == "/api/devices":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["200"])[0])
            offset = int(query.get("offset", ["0"])[0])
            self.send_json_response(HTTPStatus.OK, {"success": True, "devices": list_device_states(limit=limit, offset=offset)})
            return

        if parsed.path == "/api/version":
            self.send_json_response(HTTPStatus.OK, version_info())
            return

        if parsed.path == "/api/retention":
            self.send_json_response(HTTPStatus.OK, prune_retention(dry_run=True))
            return

        if parsed.path == "/api/full-backups":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            device_id = query.get("device_id", [None])[0]
            backups = list_full_backups(limit=limit, offset=offset, device_id=device_id)
            self.send_json_response(HTTPStatus.OK, {"success": True, "full_backups": backups, "limit": limit, "offset": offset})
            return

        if parsed.path == "/api/project-backups":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
            repo_name = query.get("repo_name", [None])[0]
            device_id = query.get("device_id", [None])[0]
            backups = list_project_backups(limit=limit, offset=offset, repo_name=repo_name, device_id=device_id)
            self.send_json_response(HTTPStatus.OK, {"success": True, "project_backups": backups, "limit": limit, "offset": offset})
            return

        if parsed.path.startswith("/api/full-backups/"):
            parts = [part for part in parsed.path.split("/") if part]
            backup_id = unquote(parts[2]) if len(parts) >= 3 else ""
            record = get_full_backup(backup_id)
            if record is None:
                self.send_error_response(HTTPStatus.NOT_FOUND, f"Full backup ID '{backup_id}' not found")
                return
            if len(parts) >= 4 and parts[3] == "download":
                artifact = full_backups_dir() / str(record["artifact_name"])
                if not is_within(artifact, full_backups_dir()) or not artifact.exists():
                    self.send_error_response(HTTPStatus.NOT_FOUND, "Backup artifact file not found")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(artifact.stat().st_size))
                self.send_header("X-Codex-Backup-Id", backup_id)
                self.send_header("X-Codex-Backup-Sha256", str(record["artifact_sha256"]))
                self.end_headers()
                with artifact.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            metadata = json.loads(record["metadata_json"])
            self.send_json_response(HTTPStatus.OK, {"success": True, "full_backup": metadata})
            return

        if parsed.path.startswith("/api/project-backups/"):
            parts = [part for part in parsed.path.split("/") if part]
            backup_id = unquote(parts[2]) if len(parts) >= 3 else ""
            record = get_project_backup(backup_id)
            if record is None:
                self.send_error_response(HTTPStatus.NOT_FOUND, f"Project backup ID '{backup_id}' not found")
                return
            if len(parts) >= 4 and parts[3] == "download":
                artifact = project_backups_dir() / str(record["artifact_name"])
                if not is_within(artifact, project_backups_dir()) or not artifact.exists():
                    self.send_error_response(HTTPStatus.NOT_FOUND, "Project backup artifact file not found")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(artifact.stat().st_size))
                self.send_header("X-Codex-Project-Backup-Id", backup_id)
                self.send_header("X-Codex-Project-Backup-Sha256", str(record["artifact_sha256"]))
                self.end_headers()
                with artifact.open("rb") as fh:
                    while chunk := fh.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            metadata = json.loads(record["metadata_json"])
            self.send_json_response(HTTPStatus.OK, {"success": True, "project_backup": metadata})
            return

        if parsed.path.startswith("/api/snapshots/"):
            snapshot_id = unquote(parsed.path.split("/")[-1])
            if not snapshot_id:
                self.send_error_response(HTTPStatus.BAD_REQUEST, "Missing snapshot id")
                return
            payload = get_snapshot(snapshot_id)
            if payload is None:
                self.send_error_response(HTTPStatus.NOT_FOUND, f"Snapshot ID '{snapshot_id}' not found")
                return
            self.send_json_response(HTTPStatus.OK, payload)
            return

        self.send_error_response(HTTPStatus.NOT_FOUND, "API endpoint not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.check_token():
            return

        if parsed.path == "/api/retention/prune":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY_BYTES:
                    self.send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Retention request payload is too large")
                    return
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    self.send_error_response(HTTPStatus.BAD_REQUEST, "JSON payload must be an object")
                    return
                result = prune_retention(dry_run=bool(payload.get("dry_run", False)))
            except Exception as exc:
                self.send_error_response(HTTPStatus.BAD_REQUEST, f"Failed to run retention prune: {exc}")
                return
            self.send_json_response(HTTPStatus.OK, result)
            return

        if parsed.path == "/api/changes":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self.send_error_response(HTTPStatus.BAD_REQUEST, "Empty request body")
                    return
                if length > MAX_BODY_BYTES:
                    self.send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Change payload is too large")
                    return
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    self.send_error_response(HTTPStatus.BAD_REQUEST, "JSON payload must be an object")
                    return
                result = save_change_notification(payload)
            except Exception as exc:
                self.send_error_response(HTTPStatus.BAD_REQUEST, f"Failed to save change notification: {exc}")
                return
            state = result.get("device_state") or {}
            status = "dirty" if state.get("dirty") else str(state.get("sync_state") or "clean")
            print(f" => Recorded change event {result['event_id']} for device '{result['device_id']}' ({status})")
            self.send_json_response(HTTPStatus.CREATED, result)
            return

        if parsed.path == "/api/full-backups":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self.send_error_response(HTTPStatus.BAD_REQUEST, "Empty request body")
                    return
                if length > FULL_BACKUP_MAX_BODY_BYTES:
                    self.send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Full backup payload is too large")
                    return
                metadata = decode_backup_metadata(self.headers.get("X-Codex-Backup-Metadata"))
                result = save_full_backup(metadata, self.rfile, length)
            except Exception as exc:
                self.send_error_response(HTTPStatus.BAD_REQUEST, f"Failed to receive full backup: {exc}")
                return
            print(f" => Saved full backup {result['backup_id']} for device '{result['device_id']}' ({result['bytes']} bytes)")
            self.send_json_response(HTTPStatus.CREATED, {"success": True, "message": "Full backup saved successfully", **result})
            return

        if parsed.path == "/api/project-backups":
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self.send_error_response(HTTPStatus.BAD_REQUEST, "Empty request body")
                    return
                if length > PROJECT_BACKUP_MAX_BODY_BYTES:
                    self.send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Project backup payload is too large")
                    return
                metadata = decode_project_backup_metadata(self.headers.get("X-Codex-Project-Backup-Metadata"))
                result = save_project_backup(metadata, self.rfile, length)
            except Exception as exc:
                self.send_error_response(HTTPStatus.BAD_REQUEST, f"Failed to receive project backup: {exc}")
                return
            print(f" => Saved project backup {result['backup_id']} for repo '{result['repo_name']}' ({result['bytes']} bytes)")
            self.send_json_response(HTTPStatus.CREATED, {"success": True, "message": "Project backup saved successfully", **result})
            return

        if parsed.path != "/api/snapshots":
            self.send_error_response(HTTPStatus.NOT_FOUND, "API endpoint not found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                self.send_error_response(HTTPStatus.BAD_REQUEST, "Empty request body")
                return
            if length > MAX_BODY_BYTES:
                self.send_error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Snapshot payload is too large")
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                self.send_error_response(HTTPStatus.BAD_REQUEST, "JSON payload must be an object")
                return
        except Exception as exc:
            self.send_error_response(HTTPStatus.BAD_REQUEST, f"Invalid JSON payload: {exc}")
            return

        try:
            result = save_snapshot(payload)
        except Exception as exc:
            self.send_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to save snapshot: {exc}")
            return

        print(f" => Saved snapshot {result['snapshot_id']} for device '{result['device_id']}'")
        self.send_json_response(HTTPStatus.CREATED, {"success": True, "message": "Snapshot saved successfully", **result})

    def send_json_response(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def send_error_response(self, status: HTTPStatus, message: str) -> None:
        print(f" [Error] {status} - {message}")
        self.send_json_response(status, {"success": False, "error": message, "status": int(status)})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


def main() -> None:
    init_db()
    token, token_source = load_or_create_token()
    SyncHandler.token = token
    server = ThreadingHTTPServer((HOST, PORT), SyncHandler)

    print("=" * 60)
    print("Codex Sync server started")
    print(f"Listen: http://{HOST}:{PORT}")
    print(f"SQLite DB: {DB_PATH}")
    print(f"Token source: {token_source}")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Codex Sync server...")
        server.server_close()


if __name__ == "__main__":
    main()
