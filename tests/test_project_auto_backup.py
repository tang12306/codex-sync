import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import sync_server
from codex_sync.config import AppConfig
from codex_sync.git_backup import create_project_backup_package, list_project_backups
from codex_sync.project_auto_backup import (
    enqueue_project_auto_backup,
    install_project_git_hook,
    process_project_auto_backup_queue,
    project_auto_backup_status,
    uninstall_project_git_hook,
)
from codex_sync.util import run_cmd


def init_repo(path: Path) -> None:
    self_check = run_cmd(["git", "init"], cwd=path)
    if self_check[0] != 0:
        raise RuntimeError(self_check[2])
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=path)
    run_cmd(["git", "config", "user.name", "Test"], cwd=path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_cmd(["git", "add", "tracked.txt"], cwd=path)
    code, _, err = run_cmd(["git", "commit", "-m", "base"], cwd=path)
    if code != 0:
        raise RuntimeError(err)


class ProjectAutoBackupTests(unittest.TestCase):
    def test_install_and_uninstall_project_git_hook(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                repo = Path(repo_dir)
                init_repo(repo)
                cfg = AppConfig(device_id="auto-device")

                installed = install_project_git_hook(repo, cfg)
                self.assertTrue(installed["success"])
                status = project_auto_backup_status(repo, cfg)
                self.assertTrue(status["enabled_for_git_commit"])

                hook_text = Path(installed["path"]).read_text(encoding="utf-8")
                self.assertIn("project-auto-backup --reason git-post-commit", hook_text)

                removed = uninstall_project_git_hook(repo)
                self.assertTrue(removed["success"])
                status = project_auto_backup_status(repo, cfg)
                self.assertFalse(status["enabled_for_git_commit"])
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_git_commit_project_package_contains_commit_patch(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                repo = Path(repo_dir)
                init_repo(repo)
                (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
                run_cmd(["git", "add", "tracked.txt"], cwd=repo)
                code, _, err = run_cmd(["git", "commit", "-m", "change"], cwd=repo)
                self.assertEqual(code, 0, err)
                _, commit, _ = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo)

                package = create_project_backup_package(repo, AppConfig(device_id="auto-device"), mode="git_commit", trigger_reason="git-post-commit", commit_ref=commit)
                self.assertEqual(package["manifest"]["source_mode"], "git_commit")
                self.assertEqual(package["manifest"]["commit"], commit)
                with zipfile.ZipFile(package["archive"]) as zf:
                    names = set(zf.namelist())
                    self.assertIn("snapshot/commit.patch", names)
                    patch = zf.read("snapshot/commit.patch").decode("utf-8")
                self.assertIn("changed", patch)
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_process_auto_backup_queue_uploads_project_backup(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                repo = Path(repo_dir)
                init_repo(repo)
                (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
                run_cmd(["git", "add", "tracked.txt"], cwd=repo)
                code, _, err = run_cmd(["git", "commit", "-m", "change"], cwd=repo)
                self.assertEqual(code, 0, err)

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "project-auto-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="auto-device")
                    queued = enqueue_project_auto_backup(repo, cfg, reason="git-post-commit")
                    self.assertTrue(queued["queued"])

                    processed = process_project_auto_backup_queue(cfg)
                    self.assertEqual(processed["remaining_count"], 0)
                    self.assertEqual(processed["processed_count"], 1)
                    self.assertTrue(processed["processed"][0]["result"]["success"])

                    listed = list_project_backups(cfg)
                    self.assertTrue(listed["success"])
                    self.assertEqual(len(listed["project_backups"]), 1)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
            finally:
                sync_server.DATA_DIR = old_data_dir
                sync_server.DB_PATH = old_db_path
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync


if __name__ == "__main__":
    unittest.main()
