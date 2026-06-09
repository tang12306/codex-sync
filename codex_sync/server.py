from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import AppConfig


DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
EXPECTED_SERVER_API_VERSION = 5
REQUIRED_SERVER_FEATURES = {
    "device_state",
    "devices",
    "changes",
    "full_backups",
    "project_backups",
    "retention",
    "wsl_full_backups",
}


def get_from_server(config: AppConfig, path: str, timeout: int = 15) -> Any:
    if not config.server_url:
        raise ValueError("server_url is empty")
    url = config.server_url.rstrip("/") + path
    headers = {"User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    with DIRECT_OPENER.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text)


def post_json_to_server(config: AppConfig, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    if not config.server_url:
        raise ValueError("server_url is empty")
    url = config.server_url.rstrip("/") + path
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "codex-sync/0.1"}
    if config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with DIRECT_OPENER.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text)


def get_server_retention(config: AppConfig) -> dict[str, Any]:
    try:
        return get_from_server(config, "/api/retention")
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_server_version(config: AppConfig) -> dict[str, Any]:
    try:
        return get_from_server(config, "/api/version")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "success": False,
                "needs_update": True,
                "error": "Remote sync server does not expose /api/version. Update the server deployment.",
                "status": exc.code,
            }
        return {"success": False, "error": str(exc), "status": exc.code}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def check_server_compatibility(config: AppConfig) -> dict[str, Any]:
    version = get_server_version(config)
    expected = {
        "api_version": EXPECTED_SERVER_API_VERSION,
        "required_features": sorted(REQUIRED_SERVER_FEATURES),
    }
    if not version.get("success"):
        return {
            "success": False,
            "compatible": False,
            "needs_update": bool(version.get("needs_update")),
            "expected": expected,
            "server": version,
            "error": version.get("error") or "Failed to check server version.",
        }
    try:
        api_version = int(version.get("api_version") or 0)
    except (TypeError, ValueError):
        api_version = 0
    features = version.get("features") if isinstance(version.get("features"), dict) else {}
    missing = sorted(feature for feature in REQUIRED_SERVER_FEATURES if not features.get(feature))
    too_old = api_version < EXPECTED_SERVER_API_VERSION
    compatible = not too_old and not missing
    return {
        "success": True,
        "compatible": compatible,
        "needs_update": not compatible,
        "expected": expected,
        "server": version,
        "missing_features": missing,
        "server_api_version": api_version,
        "message": "Server is compatible." if compatible else "Remote sync server should be updated.",
    }


def prune_server_retention(config: AppConfig, dry_run: bool = False) -> dict[str, Any]:
    try:
        return post_json_to_server(config, "/api/retention/prune", {"dry_run": dry_run}, timeout=120)
    except Exception as exc:
        return {"success": False, "error": str(exc), "dry_run": dry_run}
