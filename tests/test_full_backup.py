import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import sync_server
from codex_sync.config import AppConfig
from codex_sync.full_backup import (
    create_full_backup_package,
    download_full_backup,
    full_backup_now,
    get_remote_device_state,
    list_full_backups,
    notify_codex_changed,
    restore_full_backup,
    scan_full_backup_changes,
    state_path,
)
from codex_sync.wsl import upload_wsl_full_backup, wsl_backup_state_path


class FullBackupTests(unittest.TestCase):
    def test_full_backup_package_includes_conversations_and_excludes_auth(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                root = Path(codex_home)
                (root / "sessions").mkdir()
                (root / "sessions" / "session.jsonl").write_text("conversation", encoding="utf-8")
                (root / "session_index.jsonl").write_text("index", encoding="utf-8")
                (root / "auth.json").write_text("secret", encoding="utf-8")

                first = create_full_backup_package(AppConfig(device_id="full-test"), force=False)
                self.assertTrue(first["created"])
                with zipfile.ZipFile(first["archive"]) as zf:
                    names = set(zf.namelist())
                self.assertIn("codex/sessions/session.jsonl", names)
                self.assertIn("codex/session_index.jsonl", names)
                self.assertNotIn("codex/auth.json", names)

                second = create_full_backup_package(AppConfig(device_id="full-test"), force=False)
                self.assertFalse(second["created"])
                self.assertEqual(second["reason"], "no changes")
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_upload_list_download_and_restore_full_backup(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                root = Path(codex_home)
                (root / "sessions").mkdir()
                (root / "sessions" / "session.jsonl").write_text("conversation", encoding="utf-8")
                (root / "history.jsonl").write_text("history", encoding="utf-8")

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "full-backup-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="full-device")
                try:
                    changed = notify_codex_changed(cfg, event_id="event-1", event="UserPromptSubmit", cwd=str(root))
                    self.assertTrue(changed["success"])
                    self.assertTrue(changed["device_state"]["dirty"])
                    self.assertEqual(changed["device_state"]["sync_state"], "dirty")

                    uploaded = full_backup_now(cfg, force=True, upload=True, allow_plaintext_upload=True)
                    self.assertTrue(uploaded["success"])
                    backup_id = uploaded["package"]["backup_id"]
                    self.assertEqual(uploaded["upload"]["branch_id"], "full-device:main")
                    self.assertFalse(uploaded["upload"]["device_state"]["dirty"])
                    self.assertEqual(uploaded["upload"]["device_state"]["sync_state"], "clean")
                    self.assertEqual(uploaded["upload"]["device_state"]["head_backup_id"], backup_id)

                    listed = list_full_backups(cfg)
                    self.assertTrue(listed["success"])
                    self.assertEqual(listed["full_backups"][0]["id"], backup_id)
                    self.assertEqual(listed["full_backups"][0]["branch_id"], "full-device:main")

                    state = get_remote_device_state(cfg)
                    self.assertTrue(state["success"])
                    self.assertEqual(state["device_state"]["head_backup_id"], backup_id)

                    downloaded = download_full_backup(cfg, backup_id)
                    self.assertTrue(downloaded["success"])
                    self.assertTrue(Path(downloaded["path"]).exists())

                    (root / "sessions" / "session.jsonl").unlink()
                    restored = restore_full_backup(cfg, downloaded["path"], confirm_backup_id=backup_id)
                    self.assertTrue(restored["success"])
                    self.assertEqual((root / "sessions" / "session.jsonl").read_text(encoding="utf-8"), "conversation")

                    delayed = notify_codex_changed(
                        cfg,
                        event_id="old-event",
                        event="UserPromptSubmit",
                        changed_at="2000-01-01T00:00:00+00:00",
                    )
                    self.assertTrue(delayed["success"])
                    self.assertFalse(delayed["device_state"]["dirty"])
                    self.assertEqual(delayed["device_state"]["sync_state"], "clean")
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

    def test_upload_wsl_full_backup_uses_wsl_state(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as tmp_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "wsl-full-backup-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                cfg = AppConfig(server_url=f"http://127.0.0.1:{httpd.server_port}", api_token=token, device_id="windows-device")

                def build_wsl_archive(backup_id: str, digest: str) -> Path:
                    archive = Path(tmp_dir) / f"{backup_id}.zip"
                    manifest = {
                        "id": backup_id,
                        "type": "codex_full_backup",
                        "format_version": 1,
                        "created_at": "2026-06-07T00:00:00+00:00",
                        "device_id": "wsl:Ubuntu-24.04",
                        "distro": "Ubuntu-24.04",
                        "codex_home": "~/.codex",
                        "encrypted": False,
                        "encryption": "none",
                        "content_digest": digest,
                        "included_count": 1,
                        "included_bytes": 12,
                        "source": "wsl",
                    }
                    with zipfile.ZipFile(archive, "w") as zf:
                        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                        zf.writestr("codex/history.jsonl", "conversation\n")
                    return archive

                try:
                    first_archive = build_wsl_archive("wsl-one", "digest-one")
                    refused = upload_wsl_full_backup(cfg, "Ubuntu-24.04", first_archive)
                    self.assertFalse(refused["success"])

                    first = upload_wsl_full_backup(cfg, "Ubuntu-24.04", first_archive, allow_plaintext_upload=True)
                    self.assertTrue(first["success"])
                    self.assertEqual(first["device_id"], "wsl:Ubuntu-24.04")
                    self.assertEqual(first["branch_id"], "wsl:Ubuntu-24.04:main")
                    self.assertEqual(first["parent_backup_id"], "")
                    self.assertFalse(first["device_state"]["dirty"])
                    self.assertFalse(state_path().exists())

                    wsl_state = json.loads(wsl_backup_state_path("Ubuntu-24.04").read_text(encoding="utf-8"))
                    self.assertEqual(wsl_state["last_uploaded_backup_id"], "wsl-one")
                    self.assertEqual(wsl_state["last_uploaded_content_digest"], "digest-one")

                    second = upload_wsl_full_backup(
                        cfg,
                        "Ubuntu-24.04",
                        build_wsl_archive("wsl-two", "digest-two"),
                        allow_plaintext_upload=True,
                    )
                    self.assertTrue(second["success"])
                    self.assertEqual(second["parent_backup_id"], "wsl-one")
                    self.assertEqual(second["device_state"]["sync_state"], "clean")

                    listed = list_full_backups(cfg)
                    self.assertTrue(listed["success"])
                    ids = {item["id"] for item in listed["full_backups"]}
                    self.assertIn("wsl-one", ids)
                    self.assertIn("wsl-two", ids)
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

    def test_scan_waits_for_quiet_period_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                root = Path(codex_home)
                (root / "sessions").mkdir()
                (root / "sessions" / "session.jsonl").write_text("conversation", encoding="utf-8")

                cfg = AppConfig(device_id="quiet-device", full_backup_quiet_seconds=60)
                deferred = scan_full_backup_changes(cfg, create_package=True)
                self.assertTrue(deferred["changed"])
                self.assertTrue(deferred["package_deferred"])
                self.assertNotIn("package", deferred)

                immediate = scan_full_backup_changes(
                    AppConfig(device_id="quiet-device", full_backup_quiet_seconds=0),
                    create_package=True,
                )
                self.assertTrue(immediate["changed"])
                self.assertIn("package", immediate)
                self.assertTrue(immediate["package"]["created"])
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_periodic_scan_marks_dirty_and_upload_conflict_creates_branch(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                root = Path(codex_home)
                (root / "sessions").mkdir()
                session_file = root / "sessions" / "session.jsonl"
                session_file.write_text("base conversation", encoding="utf-8")

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "branch-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                cfg = AppConfig(
                    server_url=f"http://127.0.0.1:{httpd.server_port}",
                    api_token=token,
                    device_id="branch-device",
                    full_backup_quiet_seconds=0,
                )
                try:
                    scan = scan_full_backup_changes(cfg, create_package=True, notify_dirty=True, check_remote=True)
                    self.assertTrue(scan["dirty_notification"]["success"])
                    self.assertTrue(scan["package"]["created"])
                    dirty_state = get_remote_device_state(cfg)
                    self.assertTrue(dirty_state["device_state"]["dirty"])

                    first = full_backup_now(cfg, force=True, upload=True, allow_plaintext_upload=True)
                    self.assertTrue(first["success"])
                    first_id = first["package"]["backup_id"]

                    sync_server.mark_backup_complete(
                        "branch-device",
                        "remote-head",
                        "branch-device:main",
                        "remote-digest",
                        first_id,
                        sync_server.utc_now(),
                    )
                    session_file.write_text("local divergent conversation", encoding="utf-8")

                    divergent = full_backup_now(cfg, force=True, upload=True, allow_plaintext_upload=True)
                    self.assertTrue(divergent["success"])
                    self.assertEqual(divergent["remote_reconcile"]["action"], "created_branch")
                    self.assertNotEqual(divergent["upload"]["branch_id"], "branch-device:main")
                    self.assertEqual(divergent["upload"]["parent_backup_id"], first_id)
                    self.assertEqual(divergent["upload"]["device_state"]["sync_state"], "diverged")
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


if __name__ == "__main__":
    unittest.main()
