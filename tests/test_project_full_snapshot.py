"""Tests for 0.3.3: project auto-backup unified full snapshot + one-click restore-latest."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync.config import AppConfig


class ModeForReasonTests(unittest.TestCase):
    def test_all_reasons_return_full(self) -> None:
        from codex_sync import project_auto_backup as pab

        for reason in ("realtime", "git-post-commit", "codex-stop", "manual", "anything"):
            self.assertEqual(pab._mode_for_reason(reason), "full")


class ContentIdTests(unittest.TestCase):
    def test_full_mode_uses_content_hash_and_is_stable(self) -> None:
        from codex_sync import project_auto_backup as pab

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.txt").write_text("hello", encoding="utf-8")
            cfg = AppConfig()
            cid1 = pab._content_id(root, "full", cfg, is_repo=True)
            cid2 = pab._content_id(root, "full", cfg, is_repo=True)
            self.assertTrue(cid1.startswith("filesystem:"))
            self.assertEqual(cid1, cid2)  # 内容不变 → id 稳定，去重跳过
            (root / "a.txt").write_text("world!!", encoding="utf-8")
            cid3 = pab._content_id(root, "full", cfg, is_repo=True)
            self.assertNotEqual(cid1, cid3)  # 内容变化 → id 变，重新上传


class RestoreLatestProjectBackupTests(unittest.TestCase):
    def test_picks_latest_full_backup(self) -> None:
        from codex_sync import git_backup

        cfg = AppConfig(server_url="http://x")
        listing = {
            "success": True,
            "project_backups": [
                {"id": "patch-new", "backup_kind": "patch", "received_at": "2026-06-09T13:00:00+00:00"},
                {"id": "full-old", "backup_kind": "full", "received_at": "2026-06-08T10:00:00+00:00"},
                {"id": "full-new", "backup_kind": "full", "received_at": "2026-06-09T12:00:00+00:00"},
            ],
        }
        with mock.patch.object(git_backup, "list_project_backups", return_value=listing), \
             mock.patch.object(git_backup, "restore_project_backup_from_server", return_value={"success": True}) as rs:
            result = git_backup.restore_latest_project_backup(cfg, "myrepo", "/tmp/target")
        self.assertTrue(result["success"])
        self.assertEqual(result["backup_id"], "full-new")
        args, kwargs = rs.call_args
        self.assertEqual(args[1], "full-new")  # backup_id 位置参数
        self.assertEqual(kwargs.get("confirm_backup_id"), "full-new")

    def test_no_full_package_returns_error(self) -> None:
        from codex_sync import git_backup

        cfg = AppConfig(server_url="http://x")
        listing = {
            "success": True,
            "project_backups": [
                {"id": "patch1", "backup_kind": "patch", "received_at": "2026-06-09T13:00:00+00:00"},
            ],
        }
        with mock.patch.object(git_backup, "list_project_backups", return_value=listing):
            result = git_backup.restore_latest_project_backup(cfg, "myrepo", "/tmp/target")
        self.assertFalse(result["success"])
        self.assertIn("完整包", result["error"])

    def test_missing_repo_name(self) -> None:
        from codex_sync import git_backup

        cfg = AppConfig(server_url="http://x")
        result = git_backup.restore_latest_project_backup(cfg, "", "/tmp/target")
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
