import io
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import sync_server
from codex_sync.config import AppConfig
from codex_sync.full_backup import list_remote_devices, summarize_sync_health, _write_state
from codex_sync.git_backup import (
    backup_project_to_server,
    download_project_backup,
    list_project_backups,
    preview_project_backup_restore,
    restore_project_backup,
)
from codex_sync.util import run_cmd
from codex_sync.server import (
    check_server_compatibility,
    flush_outbox,
    generate_remote_resume_context,
    get_remote_snapshot_detail,
    list_remote_snapshots,
    sync_once,
)


class HttpIntegrationTests(unittest.TestCase):
    def test_server_version_endpoint_and_compatibility_check(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            sync_server.DATA_DIR = Path(data_dir).resolve()
            sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
            try:
                sync_server.init_db()
                token = "version-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="version-device")
                    compat = check_server_compatibility(cfg)
                    self.assertTrue(compat["success"])
                    self.assertTrue(compat["compatible"])
                    self.assertFalse(compat["needs_update"])
                    server = compat["server"]
                    self.assertGreaterEqual(server["api_version"], compat["expected"]["api_version"])
                    self.assertTrue(server["features"]["retention"])
                    self.assertTrue(server["features"]["wsl_full_backups"])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
            finally:
                sync_server.DATA_DIR = old_data_dir
                sync_server.DB_PATH = old_db_path

    def test_sync_list_detail_resume_and_outbox_flush(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "integration-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                server_url = f"http://127.0.0.1:{httpd.server_port}"
                try:
                    secret = "sk-testabcdefghijklmnopqrstuvwxyz123456"
                    Path(codex_home, "config.toml").write_text(f"api_key = '{secret}'\n", encoding="utf-8")
                    cfg = AppConfig(server_url=server_url, api_token=token, device_id="integration-device")

                    synced = sync_once(cfg, cwd=sync_home)
                    self.assertTrue(synced["sent"])

                    skipped = sync_once(cfg, cwd=sync_home, skip_unchanged=True)
                    self.assertTrue(skipped["skipped"])
                    self.assertFalse(skipped["sent"])

                    listed = list_remote_snapshots(cfg)
                    self.assertTrue(listed["success"])
                    self.assertEqual(len(listed["snapshots"]), 1)
                    snapshot_id = synced["snapshot_id"]

                    detail = get_remote_snapshot_detail(cfg, snapshot_id)
                    self.assertTrue(detail["success"])
                    snapshot = detail["snapshot"]
                    self.assertEqual(snapshot["id"], snapshot_id)
                    self.assertEqual(snapshot["codex"]["configs"], {})
                    preview = snapshot["codex"]["config_files"]["config.toml"]["redacted_preview"]
                    self.assertNotIn(secret, preview)
                    self.assertIn("<redacted>", preview)

                    resume = generate_remote_resume_context(cfg, snapshot_id)
                    self.assertTrue(resume["success"])
                    self.assertTrue(Path(resume["path"]).exists())

                    queued = sync_once(AppConfig(server_url="", api_token=token, device_id="queued-device"), cwd=sync_home)
                    self.assertTrue(queued["queued"])
                    flushed = flush_outbox(cfg)
                    self.assertGreaterEqual(flushed["sent"], 1)
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
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_devices_listing_and_sync_health(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "devices-token"
                sync_server.SyncHandler.token = token

                # 直接调服务器内部函数制造多设备状态（同进程同 DB）
                sync_server.save_change_notification({"device_id": "dev-A", "content_digest": "d1", "branch_id": "dev-A:main"})
                sync_server.mark_backup_complete("dev-B", "bk1", "dev-B:main", "d2", "", sync_server.utc_now())

                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                server_url = f"http://127.0.0.1:{httpd.server_port}"
                try:
                    cfg = AppConfig(server_url=server_url, api_token=token, device_id="dev-self")

                    listed = list_remote_devices(cfg)
                    self.assertTrue(listed["success"])
                    by_id = {d["device_id"]: d for d in listed["devices"]}
                    self.assertIn("dev-A", by_id)
                    self.assertIn("dev-B", by_id)
                    self.assertTrue(by_id["dev-A"]["dirty"])
                    self.assertEqual(by_id["dev-A"]["sync_state"], "dirty")
                    self.assertFalse(by_id["dev-B"]["dirty"])
                    self.assertEqual(by_id["dev-B"]["sync_state"], "clean")

                    # 本机无 state.json → needs_upload False；pending 只含 dirty 的 dev-A（排除本机与 clean 的 dev-B）
                    health = summarize_sync_health(cfg)
                    self.assertTrue(health["success"])
                    self.assertEqual(health["this_device_id"], "dev-self")
                    self.assertFalse(health["local"]["needs_upload"])
                    self.assertEqual({d["device_id"] for d in health["pending_devices"]}, {"dev-A"})

                    # 写本机 full_backup state.json 制造未上传变更 → needs_upload True
                    _write_state({"branch_id": "dev-self:main", "last_content_digest": "X", "last_uploaded_content_digest": "Y"})
                    self.assertTrue(summarize_sync_health(cfg)["local"]["needs_upload"])
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

    def test_project_backup_upload_list_download_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as restore_parent:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                repo = Path(repo_dir)
                self.assertEqual(run_cmd(["git", "init"], cwd=repo)[0], 0)
                self.assertEqual(run_cmd(["git", "config", "user.email", "test@example.com"], cwd=repo)[0], 0)
                self.assertEqual(run_cmd(["git", "config", "user.name", "Test"], cwd=repo)[0], 0)
                (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
                self.assertEqual(run_cmd(["git", "add", "tracked.txt"], cwd=repo)[0], 0)
                self.assertEqual(run_cmd(["git", "commit", "-m", "base"], cwd=repo)[0], 0)
                (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
                (repo / "note.txt").write_text("untracked\n", encoding="utf-8")

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "project-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="project-device")
                    uploaded = backup_project_to_server(repo, cfg)
                    self.assertTrue(uploaded["success"])
                    backup_id = uploaded["package"]["backup_id"]
                    self.assertEqual(uploaded["upload"]["backup_id"], backup_id)

                    listed = list_project_backups(cfg)
                    self.assertTrue(listed["success"])
                    self.assertEqual(listed["project_backups"][0]["id"], backup_id)
                    self.assertEqual(listed["project_backups"][0]["repo_name"], repo.name)
                    self.assertEqual(listed["project_backups"][0]["source_mode"], "git_full")
                    self.assertEqual(listed["project_backups"][0]["backup_kind"], "full")
                    filtered = list_project_backups(cfg, limit=1, repo_name=repo.name)
                    self.assertTrue(filtered["success"])
                    self.assertEqual(filtered["limit"], 1)
                    self.assertEqual(filtered["project_backups"][0]["id"], backup_id)

                    downloaded = download_project_backup(cfg, backup_id)
                    self.assertTrue(downloaded["success"])
                    target = Path(restore_parent) / "restored-project"
                    preview = preview_project_backup_restore(downloaded["path"], target)
                    self.assertTrue(preview["success"])
                    self.assertEqual(preview["restore_type"], "full")

                    restored = restore_project_backup(cfg, downloaded["path"], target, confirm_backup_id=backup_id)
                    self.assertTrue(restored["success"])
                    self.assertEqual((target / "tracked.txt").read_text(encoding="utf-8"), "changed\n")
                    self.assertEqual((target / "note.txt").read_text(encoding="utf-8"), "untracked\n")
                    self.assertFalse((target / ".git").exists())
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

    def test_project_backup_uploads_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                project = Path(project_dir)
                (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
                (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "project-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="project-device")
                    uploaded = backup_project_to_server(project, cfg)
                    self.assertTrue(uploaded["success"])
                    self.assertEqual(uploaded["package"]["manifest"]["source_mode"], "filesystem")
                    with zipfile.ZipFile(uploaded["package"]["archive"]) as zf:
                        names = set(zf.namelist())
                    self.assertIn("snapshot/files/app.py", names)
                    self.assertNotIn("snapshot/files/.env", names)
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

    def test_server_retention_prunes_old_records_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            old_policy = (
                sync_server.RETENTION_FULL_BACKUP_KEEP,
                sync_server.RETENTION_FULL_BACKUP_DAYS,
                sync_server.RETENTION_PROJECT_BACKUP_KEEP,
                sync_server.RETENTION_PROJECT_BACKUP_DAYS,
                sync_server.RETENTION_SNAPSHOT_KEEP,
                sync_server.RETENTION_SNAPSHOT_DAYS,
                sync_server.RETENTION_MAX_BYTES,
            )
            sync_server.DATA_DIR = Path(data_dir).resolve()
            sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
            sync_server.RETENTION_FULL_BACKUP_KEEP = 99
            sync_server.RETENTION_PROJECT_BACKUP_KEEP = 99
            sync_server.RETENTION_SNAPSHOT_KEEP = 99
            sync_server.RETENTION_FULL_BACKUP_DAYS = 0
            sync_server.RETENTION_PROJECT_BACKUP_DAYS = 0
            sync_server.RETENTION_SNAPSHOT_DAYS = 0
            sync_server.RETENTION_MAX_BYTES = 0
            try:
                sync_server.init_db()
                for idx in range(3):
                    full_body = f"full-{idx}".encode("utf-8")
                    sync_server.save_full_backup(
                        {
                            "id": f"full-{idx}",
                            "device_id": "dev-ret",
                            "branch_id": "dev-ret:main",
                            "content_digest": f"digest-{idx}",
                            "created_at": f"2026-06-07T00:00:0{idx}+00:00",
                        },
                        io.BytesIO(full_body),
                        len(full_body),
                    )
                    project_body = f"project-{idx}".encode("utf-8")
                    sync_server.save_project_backup(
                        {
                            "id": f"proj-{idx}",
                            "device_id": "dev-ret",
                            "repo_name": "repo-ret",
                            "created_at": f"2026-06-07T00:00:0{idx}+00:00",
                        },
                        io.BytesIO(project_body),
                        len(project_body),
                    )
                    sync_server.save_snapshot(
                        {
                            "id": f"snap-{idx}",
                            "device_id": "dev-ret",
                            "created_at": f"2026-06-07T00:00:0{idx}+00:00",
                        }
                    )

                conn = sync_server.connect_db()
                try:
                    for idx in range(3):
                        stamp = f"2026-06-07T00:00:0{idx}+00:00"
                        conn.execute("UPDATE full_backups SET received_at=? WHERE id=?", (stamp, f"full-{idx}"))
                        conn.execute("UPDATE project_backups SET received_at=? WHERE id=?", (stamp, f"proj-{idx}"))
                        conn.execute("UPDATE snapshots SET received_at=? WHERE id=?", (stamp, f"snap-{idx}"))
                    conn.commit()
                finally:
                    conn.close()

                sync_server.RETENTION_FULL_BACKUP_KEEP = 2
                sync_server.RETENTION_PROJECT_BACKUP_KEEP = 2
                sync_server.RETENTION_SNAPSHOT_KEEP = 2
                preview = sync_server.prune_retention(dry_run=True)
                self.assertTrue(preview["success"])
                self.assertEqual(preview["planned_count"], 3)
                self.assertEqual({item["id"] for item in preview["planned"]}, {"full-0", "proj-0", "snap-0"})

                pruned = sync_server.prune_retention(dry_run=False)
                self.assertTrue(pruned["success"])
                self.assertEqual(pruned["deleted_count"], 3)
                self.assertFalse((sync_server.full_backups_dir() / "full-0.zip").exists())
                self.assertFalse((sync_server.project_backups_dir() / "repo-ret-proj-0.zip").exists())
                self.assertTrue((sync_server.full_backups_dir() / "full-2.zip").exists())

                self.assertEqual({row["id"] for row in sync_server.list_full_backups(limit=50)}, {"full-1", "full-2"})
                self.assertEqual({row["id"] for row in sync_server.list_project_backups(limit=50)}, {"proj-1", "proj-2"})
                self.assertEqual({row["id"] for row in sync_server.list_snapshots(limit=50)}, {"snap-1", "snap-2"})
            finally:
                (
                    sync_server.RETENTION_FULL_BACKUP_KEEP,
                    sync_server.RETENTION_FULL_BACKUP_DAYS,
                    sync_server.RETENTION_PROJECT_BACKUP_KEEP,
                    sync_server.RETENTION_PROJECT_BACKUP_DAYS,
                    sync_server.RETENTION_SNAPSHOT_KEEP,
                    sync_server.RETENTION_SNAPSHOT_DAYS,
                    sync_server.RETENTION_MAX_BYTES,
                ) = old_policy
                sync_server.DATA_DIR = old_data_dir
                sync_server.DB_PATH = old_db_path


if __name__ == "__main__":
    unittest.main()
