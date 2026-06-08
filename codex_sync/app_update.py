from __future__ import annotations

import hashlib
import json
import re
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .paths import app_dir, ensure_app_dirs
from .util import safe_filename, utc_now, write_json


GITHUB_REPO = "tang12306/codex-sync"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
DEFAULT_CACHE_SECONDS = 6 * 60 * 60
USER_AGENT = f"codex-sync/{__version__}"

_VERSION_RE = re.compile(r"(\d+(?:\.\d+){0,3})")
_SHA256_RE = re.compile(r"\b[a-fA-F0-9]{64}\b")


JsonFetcher = Callable[[str, dict[str, str], int], dict[str, Any]]


def update_cache_path() -> Path:
    return app_dir() / "update-state.json"


def parse_version(value: str) -> tuple[int, ...]:
    match = _VERSION_RE.search(str(value or ""))
    if not match:
        return (0,)
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def compare_versions(current: str, latest: str) -> int:
    current_parts = parse_version(current)
    latest_parts = parse_version(latest)
    size = max(len(current_parts), len(latest_parts))
    current_parts = current_parts + (0,) * (size - len(current_parts))
    latest_parts = latest_parts + (0,) * (size - len(latest_parts))
    if current_parts < latest_parts:
        return -1
    if current_parts > latest_parts:
        return 1
    return 0


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Accept": accept, "User-Agent": USER_AGENT}


def _fetch_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body or "{}")
    return data if isinstance(data, dict) else {}


def _http_error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            if isinstance(data, dict):
                detail = str(data.get("message") or "")
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        return f"GitHub API HTTP {exc.code}{suffix}"
    if isinstance(exc, URLError):
        return f"GitHub API connection failed: {exc.reason}"
    return str(exc)


def _asset_summary(asset: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(asset, dict):
        return None
    return {
        "name": str(asset.get("name") or ""),
        "url": str(asset.get("browser_download_url") or ""),
        "size": int(asset.get("size") or 0),
        "content_type": str(asset.get("content_type") or ""),
    }


def _pick_windows_asset(assets: list[Any]) -> dict[str, Any] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        lower = name.lower()
        if lower.endswith((".sha256", ".sig", ".asc")):
            continue
        score = 0
        if lower.endswith(".exe"):
            score += 60
        if lower.endswith(".zip"):
            score += 25
        if "setup" in lower or "install" in lower:
            score += 25
        if "windows" in lower or "win" in lower:
            score += 35
        if "x64" in lower or "amd64" in lower:
            score += 20
        if "codexsync" in lower or "codex-sync" in lower:
            score += 20
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def _pick_checksum_asset(assets: list[Any], package_asset: dict[str, Any] | None) -> dict[str, Any] | None:
    package_name = str(package_asset.get("name") or "").lower() if package_asset else ""
    package_stem = package_name.rsplit(".", 1)[0] if package_name else ""
    candidates = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        lower = str(item.get("name") or "").lower()
        if "sha256" not in lower:
            continue
        score = 10
        if package_name and package_name in lower:
            score += 50
        if package_stem and package_stem in lower:
            score += 40
        if "windows" in lower or "win" in lower:
            score += 15
        candidates.append((score, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _release_to_result(release: dict[str, Any], current_version: str) -> dict[str, Any]:
    tag = str(release.get("tag_name") or "").strip()
    latest_version = tag[1:] if tag.startswith(("v", "V")) else tag
    if not latest_version:
        latest_version = str(release.get("name") or "").strip()
    if not latest_version:
        return {"success": False, "error": "GitHub release did not include a version tag.", "current_version": current_version}

    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    package_asset = _pick_windows_asset(assets)
    checksum_asset = _pick_checksum_asset(assets, package_asset)
    release_url = str(release.get("html_url") or "").strip() or (f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}" if tag else LATEST_RELEASE_PAGE)

    return {
        "success": True,
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_tag": tag or latest_version,
        "update_available": compare_versions(current_version, latest_version) < 0,
        "release_url": release_url,
        "release_name": str(release.get("name") or tag or latest_version),
        "published_at": str(release.get("published_at") or ""),
        "assets_count": len(assets),
        "windows_asset": _asset_summary(package_asset),
        "checksum_asset": _asset_summary(checksum_asset),
        "checked_at": utc_now(),
        "cached": False,
    }


def _parse_checked_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _read_cache() -> dict[str, Any] | None:
    path = update_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _cache_is_fresh(data: dict[str, Any], current_version: str, cache_seconds: int) -> bool:
    if str(data.get("current_version") or "") != current_version:
        return False
    checked_at = _parse_checked_at(str(data.get("checked_at") or ""))
    if checked_at is None:
        return False
    age = (datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()
    return age >= 0 and age < cache_seconds


def _write_cache(result: dict[str, Any]) -> None:
    ensure_app_dirs()
    write_json(update_cache_path(), result)


def check_app_update(
    *,
    force: bool = False,
    current_version: str | None = None,
    cache_seconds: int = DEFAULT_CACHE_SECONDS,
    fetch_json: JsonFetcher | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    current_version = current_version or __version__
    if not force:
        cached = _read_cache()
        if cached and _cache_is_fresh(cached, current_version, cache_seconds):
            result = dict(cached)
            result["cached"] = True
            return result

    fetch = fetch_json or _fetch_json
    try:
        release = fetch(LATEST_RELEASE_API, _headers(), timeout)
        result = _release_to_result(release, current_version)
        if result.get("success"):
            _write_cache(result)
        return result
    except Exception as exc:
        return {
            "success": False,
            "current_version": current_version,
            "error": _http_error_message(exc),
            "checked_at": utc_now(),
            "cached_result": _read_cache(),
        }


def _validate_github_download_url(url: str) -> bool:
    return url.startswith(f"https://github.com/{GITHUB_REPO}/releases/download/")


def _default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists() and downloads.is_dir():
        return downloads / "codex-sync-updates"
    return app_dir() / "downloads"


def _download_bytes(url: str, path: Path, timeout: int) -> str:
    digest = hashlib.sha256()
    request = Request(url, headers=_headers("application/octet-stream"))
    with urlopen(request, timeout=timeout) as response, path.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_url(url: str, timeout: int) -> str:
    request = Request(url, headers=_headers("text/plain"))
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _expected_sha256(checksum_asset: dict[str, Any] | None, timeout: int) -> str | None:
    url = str((checksum_asset or {}).get("url") or "")
    if not url or not _validate_github_download_url(url):
        return None
    text = _read_text_url(url, timeout)
    match = _SHA256_RE.search(text)
    return match.group(0).lower() if match else None


def download_latest_update(*, force: bool = False, timeout: int = 60, target_dir: str | Path | None = None) -> dict[str, Any]:
    update = check_app_update(force=force, timeout=10)
    if not update.get("success"):
        return update
    asset = update.get("windows_asset") if isinstance(update.get("windows_asset"), dict) else None
    url = str((asset or {}).get("url") or "")
    name = safe_filename(str((asset or {}).get("name") or "CodexSyncSetup-update.exe"))
    if not asset or not url:
        return {**update, "success": False, "error": "Latest release does not include a Windows download asset."}
    if not _validate_github_download_url(url):
        return {**update, "success": False, "error": "Unexpected GitHub download URL."}

    destination_dir = Path(target_dir).expanduser() if target_dir else _default_download_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / name
    try:
        actual = _download_bytes(url, destination, timeout)
        expected = _expected_sha256(update.get("checksum_asset"), timeout)
    except Exception as exc:
        return {**update, "success": False, "error": _http_error_message(exc)}
    if expected and actual.lower() != expected:
        try:
            destination.unlink()
        except OSError:
            pass
        return {
            **update,
            "success": False,
            "error": "Downloaded file checksum did not match the release checksum.",
            "expected_sha256": expected,
            "actual_sha256": actual,
        }
    return {
        **update,
        "success": True,
        "downloaded": True,
        "path": str(destination),
        "sha256": actual,
        "verified": bool(expected),
    }


def open_update_page(*, force: bool = False) -> dict[str, Any]:
    update = check_app_update(force=force, timeout=10)
    url = str(update.get("release_url") or LATEST_RELEASE_PAGE)
    opened = webbrowser.open(url)
    if opened:
        result = dict(update) if update.get("success") else {"current_version": update.get("current_version"), "update_error": update.get("error")}
        result.update({"success": True, "opened": True, "url": url})
        return result
    return {"success": False, "opened": False, "url": url, "error": "Failed to open the GitHub release page."}
