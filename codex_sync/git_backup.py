from __future__ import annotations

import base64
import hashlib
import json
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

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


def git_root(cwd: str | Path | None = None) -> Path | None:
    code, out, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if code != 0 or not out:
        return None
    return Path(out).resolve()


def git_state(cwd: str | Path | None = None) -> dict[str, Any]:
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


def create_filesystem_project_snapshot(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
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

    project = {
        "is_repo": False,
        "root": str(root),
        "name": project_name,
        "dirty": None,
        "branch": None,
        "commit": None,
    }
    write_json(snapshot_dir / "metadata.json", {"created_at": utc_now(), "project": project})
    write_json(snapshot_dir / "files_manifest.json", {"copied": copied, "skipped": skipped})
    return {"snapshot_dir": str(snapshot_dir), "metadata": {"project": project, "copied_files": copied, "skipped_files": skipped}}


def create_patch_snapshot(cwd: str | Path | None, config: AppConfig) -> dict[str, Any]:
    ensure_app_dirs()
    root = git_root(cwd)
    if not root:
        raise RuntimeError("Current directory is not inside a Git repository.")
    state = git_state(root)
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
    state = git_state(root)
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
        if mode == "git_commit":
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
    package = create_project_backup_package(cwd, config)
    uploaded = upload_project_backup(config, package["archive"], package["manifest"])
    result = {"success": bool(uploaded.get("success")), "package": package, "upload": uploaded}
    if not result["success"]:
        result["error"] = uploaded.get("error") or "project backup upload failed"
    return result


def list_project_backups(config: AppConfig) -> dict[str, Any]:
    if not config.server_url:
        return {"success": False, "error": "server_url is empty"}
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(config.server_url.rstrip("/") + "/api/project-backups", headers=headers, method="GET")
    try:
        with DIRECT_OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": _server_unsupported_message(exc), "status": exc.code}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
