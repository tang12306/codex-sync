"""Codex 跨渠道对话合并：把选定渠道的对话并入当前渠道的对话列表，可一键还原。

Codex 桌面/VS Code 扩展按 ~/.codex/state_*.sqlite 的 threads.model_provider 过滤对话列表，
切换渠道(model_provider)后看不到其他渠道的对话。本模块改写对话归属，让选定渠道的对话出现在
目标渠道列表里，并把「原渠道归属」记到本地映射文件以便还原。

关键：Codex 把每个会话 rollout .jsonl 的 session_meta.payload.model_provider 当作权威源，
app-server 会用它 backfill 覆盖 threads.model_provider。因此必须**双写**——同时改：
  1. 数据库 threads.model_provider（UI 即时过滤依据）；
  2. 对应 rollout .jsonl 第一行 session_meta 的 model_provider（防 backfill 覆盖）。
只改库不改 rollout，重启/重载 Codex 后会被纠正回原渠道（已实测）。

约束：threads 的时间戳触发器只监听 updated_at/created_at，只改 model_provider 不会触发，
排序不变；改库/改 rollout 必须在 Codex（含 VS Code 扩展的 app-server 子进程）完全关闭时进行，
否则其内存数据会覆盖修改。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import AppConfig
from .disaster_backup import create_disaster_backup
from .paths import app_dir, codex_home, ensure_app_dirs
from .util import run_cmd, utc_now, write_json


# ---------- 定位 Codex 数据与渠道 ----------

# 一次「对话工作区」扫描会从 list_conversations / list_channels 等多处反复定位 state 库，
# 每次都 glob+stat 整个 ~/.codex。用进程内短期缓存合并这些重复的文件系统扫描。
_STATE_DB_CACHE_TTL = 5.0
_state_db_cache: dict[str, tuple[float, Path]] = {}
_state_db_cache_lock = threading.Lock()


def _resolve_state_db(home: Path) -> Path:
    candidates = sorted(home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"未找到 Codex 状态数据库 state_*.sqlite（目录 {home}）")
    return candidates[0]


def codex_state_db(max_age: float = _STATE_DB_CACHE_TTL) -> Path:
    """定位 Codex 桌面应用的状态库（state_*.sqlite，版本号可能变，取最新的）。

    TTL 内直接复用缓存路径以跳过 glob+stat（仍校验文件存在，防 Codex 升级换库后用到失效路径）。
    写操作（合并/还原）后会调 invalidate_state_db_cache() 主动失效。
    """
    home = codex_home()
    key = str(home)
    if max_age > 0:
        now = time.monotonic()
        with _state_db_cache_lock:
            entry = _state_db_cache.get(key)
            if entry is not None and now - entry[0] <= max_age and entry[1].exists():
                return entry[1]
    db = _resolve_state_db(home)
    if max_age > 0:
        with _state_db_cache_lock:
            _state_db_cache[key] = (time.monotonic(), db)
    return db


def invalidate_state_db_cache() -> None:
    with _state_db_cache_lock:
        _state_db_cache.clear()


def current_codex_provider() -> str | None:
    """读 ~/.codex/config.toml 顶层的 model_provider（不引 toml 库，保持零依赖）。"""
    config = codex_home() / "config.toml"
    if not config.exists():
        return None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        m = re.match(r'\s*model_provider\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return None


def codex_running() -> bool:
    """检测 Codex 是否在运行（codex.exe）。改库前必须确保它已关闭。"""
    if os.name == "nt":
        code, out, _ = run_cmd(["tasklist", "/FO", "CSV", "/NH"])
        if code != 0:
            return False
        return "codex.exe" in out.lower()
    code, out, _ = run_cmd(["pgrep", "-fl", "codex"])
    return code == 0 and bool(out.strip())


def stop_codex(timeout: float = 5.0) -> dict[str, Any]:
    """关闭 Codex 后台进程（写操作前在用户确认后清场）。

    Codex 后台(codex.exe/app-server)通常是编辑器扩展按需拉起的子进程，关闭后会在
    下次使用 Codex 时自动重启，不影响编辑器本身。轮询确认其确已退出后返回。
    """
    if os.name == "nt":
        result = run_cmd(["taskkill", "/F", "/IM", "codex.exe", "/T"])
    else:
        result = run_cmd(["pkill", "-f", "codex"])
    waited = 0.0
    while waited < timeout and codex_running():
        time.sleep(0.25)
        waited += 0.25
    still = codex_running()
    return {"stopped": not still, "still_running": still, "kill_rc": result[0], "waited_seconds": round(waited, 2)}


# ---------- 可逆映射 ----------

def merge_state_path() -> Path:
    ensure_app_dirs()
    return app_dir() / "channel-merge-state.json"


def _read_merge_state() -> dict[str, Any]:
    path = merge_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_merge_state(data: dict[str, Any]) -> None:
    write_json(merge_state_path(), data)


# ---------- state db 专门备份 ----------

def db_backup_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "codex-db-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_state_db(db: Path) -> dict[str, Any]:
    """改库前把 state 库（含 -wal/-shm）整体备份；先 checkpoint 合并 WAL 保证一致。"""
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except sqlite3.Error:
        pass
    stamp = utc_now().replace(":", "").replace("+", "Z")
    dest = db_backup_dir() / stamp
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        src = db.with_name(db.name + suffix) if suffix else db
        if src.exists():
            shutil.copy2(src, dest / src.name)
            copied.append(src.name)
    return {"dir": str(dest), "files": copied}


# ---------- rollout 权威源改写 ----------

def _rollout_set_provider(rollout_path: str | Path | None, provider: str) -> dict[str, Any]:
    """改写 rollout .jsonl 第一行 session_meta.payload.model_provider。

    Codex 把这里当权威源回填 threads，只改库不改这里会被 backfill 覆盖。
    只重写第一行、其余字节原样流式复制（rollout 可能很大），写临时文件后原子替换。
    """
    if not rollout_path:
        return {"ok": False, "reason": "no_path"}
    p = Path(rollout_path)
    if not p.exists():
        return {"ok": False, "reason": "missing", "path": str(p)}
    try:
        with p.open("rb") as handle:
            first_raw = handle.readline()
            rest_offset = handle.tell()
    except OSError as exc:
        return {"ok": False, "reason": f"read_error:{exc}", "path": str(p)}
    if not first_raw.strip():
        return {"ok": False, "reason": "empty", "path": str(p)}
    try:
        first = json.loads(first_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "reason": "bad_json", "path": str(p)}
    payload = first.get("payload")
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        return {"ok": False, "reason": "no_session_meta", "path": str(p)}
    old = payload.get("model_provider")
    if old == provider:
        return {"ok": True, "changed": False, "old": old, "path": str(p)}
    payload["model_provider"] = provider
    new_first = (json.dumps(first, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = p.with_name(p.name + ".cstmp")
    try:
        with p.open("rb") as src, tmp.open("wb") as dst:
            src.seek(rest_offset)
            dst.write(new_first)
            shutil.copyfileobj(src, dst)
        tmp.replace(p)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "reason": f"write_error:{exc}", "path": str(p)}
    return {"ok": True, "changed": True, "old": old, "new": provider, "path": str(p)}


# ---------- 查询 ----------

def list_channels(check_running: bool = True) -> dict[str, Any]:
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    merge_state = _read_merge_state()
    channels: list[dict[str, Any]] = []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        for row in con.execute(
            "SELECT model_provider AS p, COUNT(*) AS n FROM threads GROUP BY model_provider ORDER BY model_provider"
        ):
            channels.append({"provider": row["p"] or "(unknown)", "threads": row["n"]})
    finally:
        con.close()

    by_target: dict[str, int] = defaultdict(int)
    origin_breakdown: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for info in merge_state.values():
        tgt = info.get("current_provider")
        org = info.get("original_provider")
        if tgt:
            by_target[tgt] += 1
            if org:
                origin_breakdown[tgt][org] += 1

    return {
        "success": True,
        "db": str(db),
        "current_provider": current_codex_provider(),
        "codex_running": codex_running() if check_running else False,
        "codex_running_known": bool(check_running),
        "channels": channels,
        "merged": {
            "total": len(merge_state),
            "by_target": dict(by_target),
            "origin_breakdown": {k: dict(v) for k, v in origin_breakdown.items()},
        },
    }


# ---------- 写操作 ----------

def _ensure_codex_closed(close_running: bool) -> dict[str, Any] | None:
    """写操作前确保 Codex 后台已关闭。返回 None 表示可继续；否则返回错误响应。

    Codex 在跑时：close_running=False → 返回 needs_close 让上层弹确认；
    close_running=True →（用户已确认）调 stop_codex() 关掉后台，关成功才继续。
    """
    if not codex_running():
        return None
    if not close_running:
        return {
            "success": False,
            "needs_close": True,
            "error": "检测到 Codex 正在运行。请关闭 Codex，或确认由程序自动关闭其后台进程后再操作（否则修改会被其内存数据覆盖）。",
        }
    stop = stop_codex()
    if stop.get("still_running"):
        return {"success": False, "needs_close": True, "error": "尝试关闭 Codex 失败，仍在运行，请手动关闭后重试。", "stop": stop}
    return None  # 已成功关闭，可继续


def _select_rows_by_ids(db: Path, ids: list[str]) -> list[sqlite3.Row]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        ph = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT id, model_provider, title, cwd, rollout_path FROM threads WHERE id IN ({ph})", ids
        ).fetchall()
    finally:
        con.close()


def _merge_rows(config: AppConfig, db: Path, rows: list[sqlite3.Row], target: str) -> dict[str, Any]:
    """把给定 threads 行并入 target：灾难备份 + state 备份 + 双写(库+rollout) + 记录可逆映射。"""
    if not rows:
        return {"success": False, "error": "没有可并入的对话（可能它们已在目标渠道）"}
    protection = create_disaster_backup(config, reason="before_channel_merge", force=True)
    db_backup = _backup_state_db(db)
    merge_state = _read_merge_state()
    moved: list[dict[str, Any]] = []
    rollout_results: list[dict[str, Any]] = []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        ids = [r["id"] for r in rows]
        id_ph = ",".join("?" * len(ids))
        con.execute(f"UPDATE threads SET model_provider=? WHERE id IN ({id_ph})", [target, *ids])
        con.commit()
        for r in rows:
            tid = r["id"]
            prior = merge_state.get(tid, {})
            # 保留最初的原渠道（多次并入也指向最早的真实归属）
            original = prior.get("original_provider", r["model_provider"])
            # 双写：改 rollout 权威源，否则 app-server backfill 会用 rollout 覆盖回原渠道
            rollout_results.append({"id": tid, **_rollout_set_provider(r["rollout_path"], target)})
            merge_state[tid] = {
                "original_provider": original,
                "current_provider": target,
                "title": r["title"],
                "cwd": r["cwd"],
                "rollout_path": r["rollout_path"],
                "merged_at": utc_now(),
            }
            moved.append({"id": tid, "from": r["model_provider"], "to": target, "title": r["title"]})
    finally:
        con.close()
    _write_merge_state(merge_state)
    invalidate_state_db_cache()
    return {
        "success": True,
        "moved": len(moved),
        "target": target,
        "items": moved,
        "rollout": {
            "changed": sum(1 for x in rollout_results if x.get("changed")),
            "failed": [x for x in rollout_results if not x.get("ok")],
        },
        "preflight_backup": protection,
        "db_backup": db_backup,
    }


def merge_channels(
    config: AppConfig,
    sources: list[str] | None = None,
    target: str | None = None,
    all_others: bool = False,
    close_running: bool = False,
) -> dict[str, Any]:
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    target = target or current_codex_provider()
    if not target:
        return {"success": False, "error": "无法确定目标渠道（读不到 config.toml 的 model_provider），请显式指定目标"}
    guard = _ensure_codex_closed(close_running)
    if guard is not None:
        return guard

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        existing = [r["p"] for r in con.execute("SELECT DISTINCT model_provider AS p FROM threads") if r["p"]]
        if all_others:
            sources = [p for p in existing if p != target]
        sources = sorted({s for s in (sources or []) if s and s != target})
        if not sources:
            return {"success": False, "error": "没有要并入的源渠道（源渠道为空或与目标渠道相同）"}
        placeholders = ",".join("?" * len(sources))
        rows = con.execute(
            f"SELECT id, model_provider, title, cwd, rollout_path FROM threads WHERE model_provider IN ({placeholders})",
            sources,
        ).fetchall()
    finally:
        con.close()

    res = _merge_rows(config, db, rows, target)
    if res.get("success"):
        res["sources"] = sources
    return res


def merge_threads(
    config: AppConfig,
    thread_ids: list[str] | None,
    target: str | None = None,
    close_running: bool = False,
) -> dict[str, Any]:
    """把指定对话（thread_ids）并入 target（默认当前渠道）——按对话粒度的合并。"""
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    target = target or current_codex_provider()
    if not target:
        return {"success": False, "error": "无法确定目标渠道，请显式指定目标"}
    guard = _ensure_codex_closed(close_running)
    if guard is not None:
        return guard
    ids = sorted({t for t in (thread_ids or []) if t})
    if not ids:
        return {"success": False, "error": "未指定要并入的对话"}
    rows = [r for r in _select_rows_by_ids(db, ids) if r["model_provider"] != target]
    return _merge_rows(config, db, rows, target)


def _restore_rows(config: AppConfig, db: Path, to_restore: dict[str, Any]) -> dict[str, Any]:
    """把 to_restore({tid: info}) 各自还原回 original_provider：双写 + 清映射。"""
    protection = create_disaster_backup(config, reason="before_channel_restore", force=True)
    db_backup = _backup_state_db(db)
    merge_state = _read_merge_state()
    restored: list[dict[str, Any]] = []
    rollout_results: list[dict[str, Any]] = []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        for tid, info in to_restore.items():
            orig = info["original_provider"]
            row = con.execute("SELECT rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
            if row is None:
                continue  # 该对话已不在库中，跳过改库（映射仍会被清除）
            con.execute("UPDATE threads SET model_provider=? WHERE id=?", (orig, tid))
            # 双写：rollout 权威源也改回原渠道，否则 backfill 会把它再拉回并入目标
            rollout_path = info.get("rollout_path") or row["rollout_path"]
            rollout_results.append({"id": tid, **_rollout_set_provider(rollout_path, orig)})
            restored.append({"id": tid, "to": orig})
        con.commit()
    finally:
        con.close()
    for tid in to_restore:
        merge_state.pop(tid, None)
    _write_merge_state(merge_state)
    invalidate_state_db_cache()
    return {
        "success": True,
        "restored": len(restored),
        "items": restored,
        "rollout": {
            "changed": sum(1 for x in rollout_results if x.get("changed")),
            "failed": [x for x in rollout_results if not x.get("ok")],
        },
        "preflight_backup": protection,
        "db_backup": db_backup,
    }


def restore_channels(
    config: AppConfig,
    sources: list[str] | None = None,
    all_merged: bool = False,
    close_running: bool = False,
) -> dict[str, Any]:
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    guard = _ensure_codex_closed(close_running)
    if guard is not None:
        return guard

    merge_state = _read_merge_state()
    if not merge_state:
        return {"success": True, "restored": 0, "message": "没有需要还原的并入记录"}

    if all_merged:
        to_restore = dict(merge_state)
    else:
        wanted = {s for s in (sources or []) if s}
        to_restore = {tid: info for tid, info in merge_state.items() if info.get("original_provider") in wanted}
    if not to_restore:
        return {"success": True, "restored": 0, "message": "没有匹配的并入记录可还原"}

    return _restore_rows(config, db, to_restore)


def restore_threads(
    config: AppConfig,
    thread_ids: list[str] | None,
    close_running: bool = False,
) -> dict[str, Any]:
    """还原指定对话（thread_ids）到各自原始渠道——按对话粒度的还原。"""
    try:
        db = codex_state_db()
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc)}
    guard = _ensure_codex_closed(close_running)
    if guard is not None:
        return guard
    merge_state = _read_merge_state()
    wanted = {t for t in (thread_ids or []) if t}
    to_restore = {tid: info for tid, info in merge_state.items() if tid in wanted}
    if not to_restore:
        return {"success": True, "restored": 0, "message": "选中的对话没有可还原的并入记录"}
    return _restore_rows(config, db, to_restore)
