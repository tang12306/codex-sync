from __future__ import annotations

import base64
import hashlib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .config import AppConfig
from .paths import app_dir, ensure_app_dirs
from .redact import redact_text
from .util import run_cmd, safe_filename, utc_now, write_json


DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _server_unsupported_message(exc: urllib.error.HTTPError) -> str:
    if exc.code == 404:
        return "同步服务器尚未支持项目备份接口，请先在设置页执行“更新部署”或运行 python -m codex_sync deploy-server --update --yes。"
    return str(exc)


DENY_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "auth.json",
    "deploy.json",
    "id_rsa",
    "id_ed25519",
}

DENY_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
}

DENY_PARTS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


# git_root/git_state 走多个 git 子进程；status() 每 5s 轮询与 sync-health 都会反复调用。
# 用进程内短期缓存（按 cwd 键，monotonic + TTL）合并这些重复调用，避免持续 fork git。
# 备份/内容指纹等需要实时结果的调用方传 max_age=0 绕过缓存。
_GIT_CACHE_TTL = 10.0
_git_root_cache: dict[str, tuple[float, Path | None]] = {}
_git_state_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_git_cache_lock = threading.Lock()


def _git_cache_key(cwd: str | Path | None) -> str:
    if cwd is None:
        try:
            return str(Path.cwd())
        except OSError:
            return "."
    return str(cwd)


def _compute_git_root(cwd: str | Path | None = None) -> Path | None:
    code, out, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if code != 0 or not out:
        return None
    return Path(out).resolve()


def git_root(cwd: str | Path | None = None, max_age: float = _GIT_CACHE_TTL) -> Path | None:
    if max_age <= 0:
        return _compute_git_root(cwd)
    key = _git_cache_key(cwd)
    now = time.monotonic()
    with _git_cache_lock:
        entry = _git_root_cache.get(key)
        if entry is not None and now - entry[0] <= max_age:
            return entry[1]
    root = _compute_git_root(cwd)
    with _git_cache_lock:
        _git_root_cache[key] = (time.monotonic(), root)
    return root


def _compute_git_state(cwd: str | Path | None = None) -> dict[str, Any]:
    root = git_root(cwd)
    if not root:
        return {"is_repo": False}

    def git(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        return run_cmd(["git", *args], cwd=root, timeout=timeout)

    _, branch, _ = git(["branch", "--show-current"])
    _, commit, _ = git(["rev-parse", "HEAD"])
    _, status, _ = git(["status", "--porcelain=v1"])
    _, remote_url, _ = git(["remote", "get-url", "origin"])
    _, untracked, _ = git(["ls-files", "--others", "--exclude-standard"])
    return {
        "is_repo": True,
        "root": str(root),
        "branch": branch or "detached",
        "commit": commit,
        "dirty": bool(status),
        "status": status,
        "remote_origin": remote_url,
        "untracked": [line for line in untracked.splitlines() if line.strip()],
    }


def git_state(cwd: str | Path | None = None, max_age: float = _GIT_CACHE_TTL) -> dict[str, Any]:
    """读取 Git 工作区状态。默认走 max_age 秒的进程内缓存；调用方修改返回 dict 顶层是安全的（返回浅拷贝），但不要原地修改 untracked 列表。"""
    if max_age <= 0:
        return _compute_git_state(cwd)
    key = _git_cache_key(cwd)
    now = time.monotonic()
    with _git_cache_lock:
        entry = _git_state_cache.get(key)
        if entry is not None and now - entry[0] <= max_age:
            return dict(entry[1])
    state = _compute_git_state(cwd)
    with _git_cache_lock:
        _git_state_cache[key] = (time.monotonic(), state)
    return dict(state)


def _is_safe_untracked(path: Path) -> bool:
    if path.name in DENY_FILE_NAMES:
        return False
    if path.suffix.lower() in DENY_SUFFIXES:
        return False
    return not any(part in DENY_PARTS for part in path.parts)


def _project_root(cwd: str | Path | None) -> Path:
    path = Path(cwd) if cwd is not None else Path.cwd()
    if path.is_file():
        path = path.parent
    return path.resolve()


def create_filesystem_project_snapshot(cwd: str | Path | None, config: AppConfig, project: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_app_dirs()
    root = _project_root(cwd)
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"Project directory does not exist: {root}")
    stamp = utc_now().replace(":", "").replace("+", "Z")
    project_name = safe_filename(root.name)
    snapshot_dir = app_dir() / "snapshots" / project_name / stamp
    files_dir = snapshot_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[dict[str, Any]] = []
    for src in root.rglob("*"):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(root)
        except ValueError:
            continue
        rel_posix = rel.as_posix()
        if not _is_safe_untracked(rel):
            skipped.append({"path": rel_posix, "reason": "excluded"})
            continue
        size = src.stat().st_size
        if size > config.max_untracked_copy_bytes:
            skipped.append({"path": rel_posix, "reason": "too_large", "bytes": size})
            continue
        dst = files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel_posix)

    project = project or {
        "is_repo": False,
        "root": str(root),
        "name": project_name,
        "dirty": None,
        "branch": None,
        "commit": None,
    }
    project.setdefault("root", str(root))
    project.setdefault("name", project_name)
    write_json(snapshot_dir / "metadata.json", {"created_at": utc_now(), "project": project})
    write_json(snapshot_dir / "files_manifest.json", {"copied": copied, "skipped": skipped})
    return {"snapshot_dir": str(snapshot_dir), "metadata": {"project": project, "copied_files": copied, "skipped_files": skipped}}


def create_patch_snapshot(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    ensure_app_dirs()
    root = git_root(cwd)
    if not root:
        raise RuntimeError("Current directory is not inside a Git repository.")
    state = git_state(root, max_age=0)
    stamp = utc_now().replace(":", "").replace("+", "Z")
    repo_name = safe_filename(root.name)
    snapshot_dir = app_dir() / "snapshots" / repo_name / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    code, diff, err = run_cmd(["git", "diff", "--binary", "HEAD"], cwd=root, timeout=120)
    if code != 0:
        raise RuntimeError(f"git diff failed: {err}")

    (snapshot_dir / "diff.patch").write_text(redact_text(diff), encoding="utf-8")
    (snapshot_dir / "status.txt").write_text(state.get("status", ""), encoding="utf-8")
    write_json(snapshot_dir / "metadata.json", {"created_at": utc_now(), "repo": state})

    copied: list[str] = []
    skipped: list[str] = []
    for rel in state.get("untracked", []):
        src = root / rel
        if not src.is_file() or not _is_safe_untracked(Path(rel)):
            skipped.append(rel)
            continue
        if src.stat().st_size > config.max_untracked_copy_bytes:
            skipped.append(rel)
            continue
        dst = snapshot_dir / "untracked" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    write_json(snapshot_dir / "untracked_manifest.json", {"copied": copied, "skipped": skipped})
    return {"snapshot_dir": str(snapshot_dir), "metadata": {"repo": state, "copied_untracked": copied, "skipped_untracked": skipped}}


def create_commit_patch_snapshot(cwd: str | Path | None, config: AppConfig, commit_ref: str | None = None) -> dict[str, Any]:
    ensure_app_dirs()
    root = git_root(cwd)
    if not root:
        raise RuntimeError("Current directory is not inside a Git repository.")
    ref = commit_ref or "HEAD"
    code, commit, err = run_cmd(["git", "rev-parse", ref], cwd=root)
    if code != 0 or not commit:
        raise RuntimeError(f"git rev-parse failed: {err}")
    state = git_state(root, max_age=0)
    stamp = utc_now().replace(":", "").replace("+", "Z")
    repo_name = safe_filename(root.name)
    snapshot_dir = app_dir() / "snapshots" / repo_name / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    code, patch, err = run_cmd(["git", "show", "--binary", "--format=fuller", "--stat", "--patch", commit], cwd=root, timeout=120)
    if code != 0:
        raise RuntimeError(f"git show failed: {err}")

    (snapshot_dir / "commit.patch").write_text(redact_text(patch), encoding="utf-8")
    write_json(snapshot_dir / "metadata.json", {"created_at": utc_now(), "repo": state, "commit": commit})
    return {"snapshot_dir": str(snapshot_dir), "metadata": {"repo": state, "commit": commit}}


def create_full_project_snapshot(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    root = _project_root(cwd)
    project = git_state(root, max_age=0)
    if project.get("is_repo"):
        return create_filesystem_project_snapshot(root, config, project=project)
    return create_filesystem_project_snapshot(root, config)


def project_backup_dir() -> Path:
    ensure_app_dirs()
    path = app_dir() / "project-backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_header(metadata: dict[str, Any]) -> str:
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def create_project_backup_package(
    cwd: str | Path | None,
    config: AppConfig,
    *,
    mode: str = "worktree",
    trigger_reason: str = "manual",
    commit_ref: str | None = None,
) -> dict[str, Any]:
    root = git_root(cwd)
    if root:
        if mode in {"full", "git_full", "baseline"}:
            snapshot = create_full_project_snapshot(root, config)
            project = snapshot.get("metadata", {}).get("project", {})
            source_mode = "git_full"
        elif mode == "git_commit":
            snapshot = create_commit_patch_snapshot(root, config, commit_ref=commit_ref)
            project = dict(snapshot.get("metadata", {}).get("repo", {}))
            project["commit"] = snapshot.get("metadata", {}).get("commit") or project.get("commit")
            source_mode = "git_commit"
        else:
            snapshot = create_patch_snapshot(root, config)
            project = snapshot.get("metadata", {}).get("repo", {})
            source_mode = "git_patch"
    else:
        snapshot = create_filesystem_project_snapshot(cwd, config)
        project = snapshot.get("metadata", {}).get("project", {})
        source_mode = "filesystem"
    repo_name = Path(str(project.get("root") or "project")).name or "project"
    archive_repo_name = safe_filename(repo_name)
    backup_id = str(uuid.uuid4())
    created_at = utc_now()
    archive = project_backup_dir() / f"{created_at.replace(':', '').replace('+', 'Z')}-{archive_repo_name}-{backup_id}.zip"
    manifest = {
        "id": backup_id,
        "type": "project_backup",
        "format_version": 1,
        "source_mode": source_mode,
        "backup_kind": "full" if source_mode in {"git_full", "filesystem"} else "patch",
        "created_at": created_at,
        "device_id": config.device_id,
        "repo_name": repo_name,
        "repo_root": project.get("root"),
        "branch": project.get("branch"),
        "commit": project.get("commit"),
        "commit_ref": commit_ref,
        "dirty": project.get("dirty"),
        "trigger_reason": trigger_reason,
        "snapshot": snapshot,
    }
    snapshot_dir = Path(str(snapshot["snapshot_dir"]))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for file in snapshot_dir.rglob("*"):
            if file.is_file():
                zf.write(file, arcname=f"snapshot/{file.relative_to(snapshot_dir).as_posix()}")
    sha = _hash_file(archive)
    manifest["archive"] = str(archive)
    manifest["archive_sha256"] = sha
    manifest["archive_bytes"] = archive.stat().st_size
    write_json(archive.with_suffix(".manifest.json"), manifest)
    return {
        "success": True,
        "backup_id": backup_id,
        "archive": str(archive),
        "archive_sha256": sha,
        "archive_bytes": archive.stat().st_size,
        "manifest": manifest,
    }


def upload_project_backup(config: AppConfig, archive: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    if not config.server_url:
        return {"success": False, "error": "server_url is empty", "archive": str(archive_path)}
    metadata = {
        "id": manifest.get("id"),
        "type": manifest.get("type") or "project_backup",
        "format_version": manifest.get("format_version", 1),
        "source_mode": manifest.get("source_mode"),
        "backup_kind": manifest.get("backup_kind"),
        "device_id": manifest.get("device_id"),
        "repo_name": manifest.get("repo_name"),
        "repo_root": manifest.get("repo_root"),
        "branch": manifest.get("branch"),
        "commit": manifest.get("commit"),
        "trigger_reason": manifest.get("trigger_reason"),
        "created_at": manifest.get("created_at"),
        "archive_sha256": _hash_file(archive_path),
        "archive_bytes": archive_path.stat().st_size,
    }
    headers = {
        "User-Agent": "codex-sync/0.1",
        "Content-Type": "application/zip",
        "Content-Length": str(archive_path.stat().st_size),
        "X-Codex-Project-Backup-Metadata": _metadata_header(metadata),
    }
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    with archive_path.open("rb") as fh:
        request = urllib.request.Request(
            config.server_url.rstrip("/") + "/api/project-backups",
            data=fh,
            headers=headers,
            method="POST",
        )
        try:
            with DIRECT_OPENER.open(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"raw": text}
                return {"success": response.status == 201, "status": response.status, **payload}
        except urllib.error.HTTPError as exc:
            return {"success": False, "error": _server_unsupported_message(exc), "status": exc.code, "archive": str(archive_path), "backup_id": manifest.get("id")}
        except Exception as exc:
            return {"success": False, "error": str(exc), "archive": str(archive_path), "backup_id": manifest.get("id")}


def backup_project_to_server(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    package = create_project_backup_package(cwd, config, mode="full", trigger_reason="manual_full")
    uploaded = upload_project_backup(config, package["archive"], package["manifest"])
    result = {"success": bool(uploaded.get("success")), "package": package, "upload": uploaded}
    if not result["success"]:
        result["error"] = uploaded.get("error") or "project backup upload failed"
    return result


def list_project_backups(
    config: AppConfig,
    *,
    limit: int = 50,
    offset: int = 0,
    repo_name: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    if not config.server_url:
        return {"success": False, "error": "server_url is empty"}
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    query: dict[str, str] = {
        "limit": str(max(1, min(int(limit), 200))),
        "offset": str(max(0, int(offset))),
    }
    if repo_name:
        query["repo_name"] = repo_name
    if device_id:
        query["device_id"] = device_id
    request = urllib.request.Request(
        config.server_url.rstrip("/") + f"/api/project-backups?{urlencode(query)}",
        headers=headers,
        method="GET",
    )
    try:
        with DIRECT_OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": _server_unsupported_message(exc), "status": exc.code}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def project_backup_download_dir() -> Path:
    path = project_backup_dir() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_headers(config: AppConfig) -> dict[str, str]:
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    return headers


def download_project_backup(config: AppConfig, backup_id: str, output: str | Path | None = None) -> dict[str, Any]:
    if not backup_id:
        return {"success": False, "error": "backup_id is required"}
    if not config.server_url:
        return {"success": False, "error": "server_url is empty", "backup_id": backup_id}
    output_path = Path(output) if output else project_backup_download_dir() / f"{safe_filename(backup_id)}.zip"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        config.server_url.rstrip("/") + f"/api/project-backups/{quote(backup_id, safe='')}/download",
        headers=_project_headers(config),
        method="GET",
    )
    try:
        digest = hashlib.sha256()
        total = 0
        expected = ""
        with DIRECT_OPENER.open(request, timeout=120) as response, output_path.open("wb") as fh:
            expected = response.headers.get("X-Codex-Project-Backup-Sha256", "")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
        actual = digest.hexdigest()
        if expected and actual != expected:
            output_path.unlink(missing_ok=True)
            return {"success": False, "error": "downloaded project backup sha256 mismatch", "backup_id": backup_id}
        return {"success": True, "backup_id": backup_id, "path": str(output_path), "bytes": total, "sha256": actual}
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": _server_unsupported_message(exc), "status": exc.code, "backup_id": backup_id}
    except Exception as exc:
        return {"success": False, "error": str(exc), "backup_id": backup_id}


def _safe_restore_target(root: Path, relative: str) -> Path | None:
    rel = Path(relative)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None
    target = root / rel
    try:
        root_resolved = root.resolve()
        parent = target.parent.resolve()
        if parent == root_resolved or root_resolved in parent.parents:
            return target
    except OSError:
        return None
    return None


def _project_restore_root(target_dir: str | Path) -> Path:
    root = Path(target_dir).expanduser()
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"Restore target is not a directory: {root}")
    return root.resolve()


def _read_project_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    return json.loads(zf.read("manifest.json").decode("utf-8"))


def _project_file_members(zf: zipfile.ZipFile) -> list[str]:
    return [name for name in zf.namelist() if name.startswith("snapshot/files/") and not name.endswith("/")]


def _project_untracked_members(zf: zipfile.ZipFile) -> list[str]:
    return [name for name in zf.namelist() if name.startswith("snapshot/untracked/") and not name.endswith("/")]


def _project_patch_member(zf: zipfile.ZipFile, source_mode: str) -> str | None:
    candidates = ["snapshot/diff.patch"] if source_mode == "git_patch" else ["snapshot/commit.patch", "snapshot/diff.patch"]
    names = set(zf.namelist())
    for name in candidates:
        if name in names:
            return name
    return None


def _write_temp_patch(backup_id: str, patch_bytes: bytes) -> Path:
    temp_dir = project_backup_dir() / "restore-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"{safe_filename(backup_id)}.patch"
    path.write_bytes(patch_bytes)
    return path


def preview_project_backup_restore(archive: str | Path, target_dir: str | Path) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    try:
        target_root = _project_restore_root(target_dir)
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    with zipfile.ZipFile(archive_path) as zf:
        manifest = _read_project_manifest(zf)
        backup_id = str(manifest.get("id") or "")
        source_mode = str(manifest.get("source_mode") or "")
        if source_mode in {"filesystem", "git_full"}:
            planned: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for name in _project_file_members(zf):
                rel = name[len("snapshot/files/") :]
                target = _safe_restore_target(target_root, rel)
                if target is None:
                    skipped.append({"path": rel, "reason": "unsafe path"})
                    continue
                info = zf.getinfo(name)
                status = "create"
                if target.exists():
                    status = "same_size" if target.stat().st_size == info.file_size else "overwrite"
                planned.append({"path": rel, "status": status, "bytes": info.file_size})
            return {
                "success": True,
                "backup_id": backup_id,
                "source_mode": source_mode,
                "target_dir": str(target_root),
                "restore_type": "full",
                "file_count": len(planned),
                "overwrite_count": sum(1 for item in planned if item["status"] in {"overwrite", "same_size"}),
                "planned": planned[:100],
                "skipped": skipped,
            }
        if source_mode in {"git_patch", "git_commit"}:
            patch_member = _project_patch_member(zf, source_mode)
            if patch_member is None:
                return {"success": False, "error": "project patch file not found in backup", "backup_id": backup_id}
            patch_bytes = zf.read(patch_member)
            if patch_bytes.strip():
                patch_path = _write_temp_patch(backup_id, patch_bytes)
                code, out, err = run_cmd(["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)], cwd=target_root, timeout=120)
                patch_ok = code == 0
            else:
                out = err = ""
                patch_ok = True
            untracked = [name[len("snapshot/untracked/") :] for name in _project_untracked_members(zf)]
            return {
                "success": patch_ok,
                "backup_id": backup_id,
                "source_mode": source_mode,
                "target_dir": str(target_root),
                "restore_type": "patch",
                "patch_ok": patch_ok,
                "patch_stdout": out,
                "patch_stderr": err,
                "untracked_count": len(untracked),
                "untracked": untracked[:100],
                "error": None if patch_ok else (err or out or "git apply --check failed"),
            }
        return {"success": False, "error": f"unsupported project backup source_mode: {source_mode}", "backup_id": backup_id}


def restore_project_backup(config: AppConfig, archive: str | Path, target_dir: str | Path, confirm_backup_id: str, overwrite: bool = True) -> dict[str, Any]:
    archive_path = Path(archive)
    if not archive_path.exists():
        return {"success": False, "error": f"archive not found: {archive_path}"}
    try:
        target_root = _project_restore_root(target_dir)
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    with zipfile.ZipFile(archive_path) as zf:
        manifest = _read_project_manifest(zf)
        backup_id = str(manifest.get("id") or "")
        if confirm_backup_id != backup_id:
            return {"success": False, "error": "Refusing restore without exact confirm_backup_id match", "backup_id": backup_id}
        source_mode = str(manifest.get("source_mode") or "")
        preflight = None
        if target_root.exists() and any(target_root.iterdir()):
            try:
                preflight = create_project_backup_package(target_root, config, mode="full", trigger_reason=f"before_project_restore_{backup_id[:8]}")
            except Exception as exc:
                return {"success": False, "error": f"Failed to create preflight project backup: {exc}", "backup_id": backup_id}
        target_root.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        skipped: list[dict[str, Any]] = []
        if source_mode in {"filesystem", "git_full"}:
            for name in _project_file_members(zf):
                rel = name[len("snapshot/files/") :]
                target = _safe_restore_target(target_root, rel)
                if target is None:
                    skipped.append({"path": rel, "reason": "unsafe path"})
                    continue
                if target.exists() and not overwrite:
                    skipped.append({"path": rel, "reason": "exists"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(chunk)
                restored.append(rel)
            return {
                "success": True,
                "backup_id": backup_id,
                "source_mode": source_mode,
                "target_dir": str(target_root),
                "restored_count": len(restored),
                "restored": restored[:200],
                "skipped": skipped,
                "preflight_backup": preflight,
            }
        if source_mode in {"git_patch", "git_commit"}:
            patch_member = _project_patch_member(zf, source_mode)
            if patch_member is None:
                return {"success": False, "error": "project patch file not found in backup", "backup_id": backup_id, "preflight_backup": preflight}
            patch_bytes = zf.read(patch_member)
            if patch_bytes.strip():
                patch_path = _write_temp_patch(backup_id, patch_bytes)
                code, out, err = run_cmd(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=target_root, timeout=120)
                if code != 0:
                    return {"success": False, "error": err or out or "git apply failed", "backup_id": backup_id, "preflight_backup": preflight}
                restored.append(str(Path(patch_member).name))
            for name in _project_untracked_members(zf):
                rel = name[len("snapshot/untracked/") :]
                target = _safe_restore_target(target_root, rel)
                if target is None:
                    skipped.append({"path": rel, "reason": "unsafe path"})
                    continue
                if target.exists() and not overwrite:
                    skipped.append({"path": rel, "reason": "exists"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(chunk)
                restored.append(rel)
            return {
                "success": True,
                "backup_id": backup_id,
                "source_mode": source_mode,
                "target_dir": str(target_root),
                "restored_count": len(restored),
                "restored": restored[:200],
                "skipped": skipped,
                "preflight_backup": preflight,
            }
        return {"success": False, "error": f"unsupported project backup source_mode: {source_mode}", "backup_id": backup_id, "preflight_backup": preflight}


def preview_project_backup_from_server(config: AppConfig, backup_id: str, target_dir: str | Path) -> dict[str, Any]:
    downloaded = download_project_backup(config, backup_id)
    if not downloaded.get("success"):
        return {"success": False, "download": downloaded, "error": downloaded.get("error") or "project backup download failed"}
    preview = preview_project_backup_restore(str(downloaded["path"]), target_dir)
    return {"success": bool(preview.get("success")), "download": downloaded, "preview": preview, "error": preview.get("error")}


def restore_project_backup_from_server(
    config: AppConfig,
    backup_id: str,
    target_dir: str | Path,
    *,
    confirm_backup_id: str,
    overwrite: bool = True,
) -> dict[str, Any]:
    downloaded = download_project_backup(config, backup_id)
    if not downloaded.get("success"):
        return {"success": False, "download": downloaded, "error": downloaded.get("error") or "project backup download failed"}
    restored = restore_project_backup(config, str(downloaded["path"]), target_dir, confirm_backup_id=confirm_backup_id, overwrite=overwrite)
    return {"success": bool(restored.get("success")), "download": downloaded, "restore": restored, "error": restored.get("error")}


def restore_latest_project_backup(
    config: AppConfig,
    repo_name: str,
    target_dir: str | Path,
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    """挑该项目云端最新的完整备份（backup_kind=full）一键恢复到 target_dir。

    用于「合上 A 机，在 B 机一键拿到最新完整代码」：无需手动选基线/补丁。
    完整快照含工作树全部安全文件（不含 .git 历史），恢复即得可用源码目录。
    """
    if not repo_name:
        return {"success": False, "error": "repo_name is required"}
    listing = list_project_backups(config, repo_name=repo_name, limit=200)
    if not isinstance(listing, dict) or listing.get("success") is False:
        error = listing.get("error") if isinstance(listing, dict) else None
        return {"success": False, "error": error or "无法获取云端项目备份列表", "repo_name": repo_name}
    backups = listing.get("project_backups")
    if not isinstance(backups, list) or not backups:
        return {"success": False, "error": f"云端没有项目 {repo_name} 的备份", "repo_name": repo_name}
    full = [b for b in backups if isinstance(b, dict) and b.get("backup_kind") == "full" and b.get("id")]
    if not full:
        return {"success": False, "error": f"项目 {repo_name} 在云端只有补丁备份、没有可独立恢复的完整包", "repo_name": repo_name}
    chosen = max(full, key=lambda b: str(b.get("received_at") or b.get("created_at") or ""))
    backup_id = str(chosen.get("id"))
    result = restore_project_backup_from_server(config, backup_id, target_dir, confirm_backup_id=backup_id, overwrite=overwrite)
    result.setdefault("backup_id", backup_id)
    result["selected"] = chosen
    return result


def backup_to_github(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    snapshot = create_patch_snapshot(cwd, config)
    source_root = git_root(cwd)
    if not source_root:
        raise RuntimeError("Current directory is not inside a Git repository.")

    remote_name = config.github_remote or "origin"
    code, remote_url, err = run_cmd(["git", "remote", "get-url", remote_name], cwd=source_root)
    if code != 0 or not remote_url:
        raise RuntimeError(f"Cannot read Git remote '{remote_name}': {err}")

    repo_name = safe_filename(source_root.name)
    mirror_dir = app_dir() / "git-backups" / repo_name
    branch = f"{config.backup_branch_prefix}/{safe_filename(config.device_id)}"

    if not mirror_dir.exists():
        code, _, err = run_cmd(["git", "clone", remote_url, str(mirror_dir)], timeout=300)
        if code != 0:
            raise RuntimeError(f"git clone failed: {err}")
    else:
        code, _, err = run_cmd(["git", "fetch", remote_name], cwd=mirror_dir, timeout=120)
        if code != 0:
            raise RuntimeError(f"git fetch failed: {err}")

    code, _, _ = run_cmd(["git", "checkout", branch], cwd=mirror_dir)
    if code != 0:
        code, _, _ = run_cmd(["git", "checkout", "--orphan", branch], cwd=mirror_dir)
        if code != 0:
            raise RuntimeError(f"Cannot create backup branch '{branch}'.")
        for child in mirror_dir.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    dest = mirror_dir / "snapshots" / repo_name / Path(snapshot["snapshot_dir"]).name
    shutil.copytree(snapshot["snapshot_dir"], dest, dirs_exist_ok=True)
    write_json(mirror_dir / "latest.json", {"repo": repo_name, "latest_snapshot": str(dest), "updated_at": utc_now()})

    run_cmd(["git", "add", "snapshots", "latest.json"], cwd=mirror_dir, timeout=60)
    code, _, err = run_cmd(["git", "commit", "-m", f"codex autosave artifact: {repo_name} {config.device_id}"], cwd=mirror_dir, timeout=120)
    if code != 0 and "nothing to commit" not in err.lower():
        raise RuntimeError(f"git commit failed: {err}")
    code, _, err = run_cmd(["git", "push", "-u", remote_name, branch], cwd=mirror_dir, timeout=300)
    if code != 0:
        raise RuntimeError(f"git push failed: {err}")

    return {"branch": branch, "mirror_dir": str(mirror_dir), "snapshot": snapshot}
