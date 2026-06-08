import os
import tempfile
import unittest
from typing import Any

from codex_sync import app_update


def sample_release(tag: str = "v0.1.4") -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://github.com/tang12306/codex-sync/releases/tag/{tag}",
        "published_at": "2026-06-07T00:00:00Z",
        "assets": [
            {
                "name": f"CodexSyncSetup-{tag}-windows-x64.exe",
                "browser_download_url": f"https://github.com/tang12306/codex-sync/releases/download/{tag}/CodexSyncSetup-{tag}-windows-x64.exe",
                "size": 5678,
                "content_type": "application/vnd.microsoft.portable-executable",
            },
            {
                "name": f"CodexSyncSetup-{tag}-windows-x64.exe.sha256",
                "browser_download_url": f"https://github.com/tang12306/codex-sync/releases/download/{tag}/CodexSyncSetup-{tag}-windows-x64.exe.sha256",
                "size": 80,
                "content_type": "text/plain",
            },
            {
                "name": f"CodexSync-{tag}-windows-x64.zip",
                "browser_download_url": f"https://github.com/tang12306/codex-sync/releases/download/{tag}/CodexSync-{tag}-windows-x64.zip",
                "size": 1234,
                "content_type": "application/zip",
            },
            {
                "name": f"CodexSync-{tag}-windows-x64.zip.sha256",
                "browser_download_url": f"https://github.com/tang12306/codex-sync/releases/download/{tag}/CodexSync-{tag}-windows-x64.zip.sha256",
                "size": 80,
                "content_type": "text/plain",
            },
        ],
    }


class AppUpdateTests(unittest.TestCase):
    def test_compare_versions(self) -> None:
        self.assertLess(app_update.compare_versions("0.1.3", "v0.1.4"), 0)
        self.assertEqual(app_update.compare_versions("v0.1.4", "0.1.4"), 0)
        self.assertGreater(app_update.compare_versions("0.2.0", "0.1.9"), 0)

    def test_check_app_update_picks_windows_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = tmp
            try:
                result = app_update.check_app_update(
                    force=True,
                    current_version="0.1.3",
                    fetch_json=lambda _url, _headers, _timeout: sample_release(),
                )
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_home

        self.assertTrue(result["success"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "0.1.4")
        self.assertEqual(result["windows_asset"]["name"], "CodexSyncSetup-v0.1.4-windows-x64.exe")
        self.assertEqual(result["checksum_asset"]["name"], "CodexSyncSetup-v0.1.4-windows-x64.exe.sha256")

    def test_check_app_update_uses_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_home = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = tmp
            try:
                first = app_update.check_app_update(
                    force=True,
                    current_version="0.1.3",
                    fetch_json=lambda _url, _headers, _timeout: sample_release(),
                )
                self.assertFalse(first["cached"])

                def fail_fetch(_url: str, _headers: dict[str, str], _timeout: int) -> dict[str, Any]:
                    raise AssertionError("fresh cache should avoid network access")

                cached = app_update.check_app_update(current_version="0.1.3", fetch_json=fail_fetch)
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_home

        self.assertTrue(cached["success"])
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["latest_version"], "0.1.4")


if __name__ == "__main__":
    unittest.main()
