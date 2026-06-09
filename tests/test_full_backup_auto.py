"""Tests for the "close laptop, resume on another machine" autopilot additions:
headless auto-upload of encrypted full backups, one-click restore-latest, and
portable encryption key export/import."""
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import sync_server
from codex_sync.config import AppConfig
from codex_sync.full_backup import (
    create_full_backup_package,
    encryption_key_path,
    export_encryption_key,
    import_encryption_key,
    open_full_backup_zip,
    restore_latest_full_backup,
    scan_full_backup_changes,
)


class _EnvMixin(unittest.TestCase):
    """Isolate CODEX_SYNC_HOME / CODEX_HOME into throwaway temp dirs per test."""

    def setUp(self) -> None:
        self._sync = tempfile.TemporaryDirectory()
        self._codex = tempfile.TemporaryDirectory()
        self._old_sync = os.environ.get("CODEX_SYNC_HOME")
        self._old_codex = os.environ.get("CODEX_HOME")
        self._old_pass = os.environ.get("CODEX_SYNC_FULL_BACKUP_PASSPHRASE")
        os.environ["CODEX_SYNC_HOME"] = self._sync.name
        os.environ["CODEX_HOME"] = self._codex.name
        os.environ.pop("CODEX_SYNC_FULL_BACKUP_PASSPHRASE", None)

    def tearDown(self) -> None:
        for var, old in (
            ("CODEX_SYNC_HOME", self._old_sync),
            ("CODEX_HOME", self._old_codex),
            ("CODEX_SYNC_FULL_BACKUP_PASSPHRASE", self._old_pass),
        ):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old
        self._sync.cleanup()
        self._codex.cleanup()

    def _seed_conversation(self, text: str = "conversation") -> Path:
        root = Path(self._codex.name)
        (root / "sessions").mkdir(exist_ok=True)
        (root / "sessions" / "session.jsonl").write_text(text, encoding="utf-8")
        return root


class AutoUploadTests(_EnvMixin):
    def test_scan_auto_uploads_encrypted_package_when_enabled(self) -> None:
        self._seed_conversation()
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="auto-dev", full_backup_auto_upload=True)
        with mock.patch("codex_sync.full_backup.upload_full_backup", return_value={"success": True, "status": 201}) as up:
            result = scan_full_backup_changes(cfg, create_package=True, force=True, upload=True)
        self.assertTrue(result["package"]["created"])
        up.assert_called_once()
        self.assertTrue(result["upload"]["success"])

    def test_scan_skips_upload_when_auto_upload_disabled(self) -> None:
        self._seed_conversation()
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="auto-dev", full_backup_auto_upload=False)
        with mock.patch("codex_sync.full_backup.upload_full_backup") as up:
            result = scan_full_backup_changes(cfg, create_package=True, force=True, upload=True)
        up.assert_not_called()
        self.assertNotIn("upload", result)

    def test_scan_skips_upload_without_server_url(self) -> None:
        self._seed_conversation()
        cfg = AppConfig(server_url="", device_id="auto-dev", full_backup_auto_upload=True)
        with mock.patch("codex_sync.full_backup.upload_full_backup") as up:
            scan_full_backup_changes(cfg, create_package=True, force=True, upload=True)
        up.assert_not_called()

    def test_scan_without_upload_flag_does_not_upload(self) -> None:
        self._seed_conversation()
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="auto-dev", full_backup_auto_upload=True)
        with mock.patch("codex_sync.full_backup.upload_full_backup") as up:
            result = scan_full_backup_changes(cfg, create_package=True, force=True)  # upload defaults to False
        up.assert_not_called()
        self.assertNotIn("upload", result)

    def test_plaintext_package_auto_upload_is_refused(self) -> None:
        # Encryption disabled => plaintext package; auto upload attempts but is refused
        # by upload_full_backup before any network call (no allow_plaintext_upload).
        self._seed_conversation()
        cfg = AppConfig(
            server_url="http://127.0.0.1:9",
            device_id="auto-dev",
            full_backup_auto_upload=True,
            full_backup_encryption_enabled=False,
            full_backup_allow_plaintext_upload=False,
        )
        result = scan_full_backup_changes(cfg, create_package=True, force=True, upload=True)
        self.assertFalse(result["package"]["encrypted"])
        self.assertFalse(result["upload"]["success"])
        self.assertIn("plaintext", result["upload"]["error"].lower())


class RestoreLatestTests(_EnvMixin):
    def _listing(self, rows):
        return {"success": True, "full_backups": rows}

    def test_restore_latest_picks_newest_and_passes_confirm_id(self) -> None:
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="b-machine")
        listing = self._listing([
            {"id": "new-id", "device_id": "a-machine", "branch_id": "a-machine:main", "received_at": "2026-01-02T00:00:00Z"},
            {"id": "old-id", "device_id": "a-machine", "branch_id": "a-machine:main", "received_at": "2026-01-01T00:00:00Z"},
        ])
        with mock.patch("codex_sync.full_backup.list_full_backups", return_value=listing), \
             mock.patch("codex_sync.full_backup.download_full_backup", return_value={"success": True, "path": "/tmp/x.zip.enc", "bytes": 10, "encrypted": True}) as dl, \
             mock.patch("codex_sync.full_backup.restore_full_backup", return_value={"success": True, "backup_id": "new-id", "restored_count": 3}) as rs:
            result = restore_latest_full_backup(cfg)
        self.assertTrue(result["success"])
        dl.assert_called_once_with(cfg, "new-id")
        _, kwargs = rs.call_args
        self.assertEqual(kwargs.get("confirm_backup_id"), "new-id")
        self.assertEqual(result["selected"]["id"], "new-id")

    def test_restore_latest_filters_by_device(self) -> None:
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="b-machine")
        listing = self._listing([
            {"id": "other", "device_id": "c-machine", "branch_id": "c:main", "received_at": "2026-01-03T00:00:00Z"},
            {"id": "want", "device_id": "a-machine", "branch_id": "a:main", "received_at": "2026-01-02T00:00:00Z"},
        ])
        with mock.patch("codex_sync.full_backup.list_full_backups", return_value=listing), \
             mock.patch("codex_sync.full_backup.download_full_backup", return_value={"success": True, "path": "/tmp/x", "bytes": 1}) as dl, \
             mock.patch("codex_sync.full_backup.restore_full_backup", return_value={"success": True}):
            restore_latest_full_backup(cfg, device_id="a-machine")
        dl.assert_called_once_with(cfg, "want")

    def test_restore_latest_handles_empty_listing(self) -> None:
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="b")
        with mock.patch("codex_sync.full_backup.list_full_backups", return_value={"success": True, "full_backups": []}):
            result = restore_latest_full_backup(cfg)
        self.assertFalse(result["success"])

    def test_restore_latest_surfaces_list_failure(self) -> None:
        cfg = AppConfig(server_url="http://127.0.0.1:9", device_id="b")
        with mock.patch("codex_sync.full_backup.list_full_backups", return_value={"success": False, "error": "boom"}):
            result = restore_latest_full_backup(cfg)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "boom")


class EncryptionKeyPortabilityTests(_EnvMixin):
    def test_export_then_import_roundtrip(self) -> None:
        cfg = AppConfig(device_id="key-dev")
        out = Path(self._sync.name) / "exported-key.json"
        exported = export_encryption_key(cfg, out)
        self.assertTrue(exported["success"])
        self.assertTrue(out.exists())
        key_id = exported["key_id"]
        encryption_key_path().unlink(missing_ok=True)  # simulate a fresh machine
        imported = import_encryption_key(cfg, out)
        self.assertTrue(imported["success"])
        self.assertEqual(imported["key_id"], key_id)
        self.assertTrue(encryption_key_path().exists())

    def test_imported_key_can_decrypt_existing_package(self) -> None:
        root = self._seed_conversation()
        cfg = AppConfig(device_id="key-dev")
        pkg = create_full_backup_package(cfg, force=True)
        self.assertTrue(pkg["encrypted"])
        out = Path(self._sync.name) / "key.json"
        export_encryption_key(cfg, out)

        encryption_key_path().unlink()
        with self.assertRaises(Exception):
            with open_full_backup_zip(cfg, pkg["archive"]) as zf:
                zf.namelist()

        imported = import_encryption_key(cfg, out)
        self.assertTrue(imported["success"])
        with open_full_backup_zip(cfg, pkg["archive"]) as zf:
            self.assertIn("manifest.json", zf.namelist())
        self.assertEqual(root.name, Path(self._codex.name).name)


class ActionRoutingTests(_EnvMixin):
    def test_export_encryption_key_action_with_explicit_path(self) -> None:
        from codex_sync.web_desktop import DesktopRuntime

        runtime = DesktopRuntime()
        out = Path(self._sync.name) / "k.json"
        result = runtime.run_action("export-encryption-key", {"output_path": str(out)})
        self.assertTrue(result["success"])
        self.assertTrue(out.exists())

    def test_restore_latest_full_backup_action_routes(self) -> None:
        from codex_sync.web_desktop import DesktopRuntime

        runtime = DesktopRuntime()
        with mock.patch("codex_sync.web_desktop.restore_latest_full_backup", return_value={"success": True, "restored_count": 0}) as rl:
            result = runtime.run_action("restore-latest-full-backup", {"device_id": "a"})
        rl.assert_called_once()
        self.assertTrue(result["success"])


class AutopilotEndToEndTests(unittest.TestCase):
    """Real HTTP round-trip: auto-upload an encrypted package, then restore the
    latest one back onto a freshly-emptied Codex home (the "close laptop, resume
    on another machine" main path)."""

    def test_auto_upload_then_restore_latest_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as data_dir:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_pass = os.environ.get("CODEX_SYNC_FULL_BACKUP_PASSPHRASE")
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            os.environ.pop("CODEX_SYNC_FULL_BACKUP_PASSPHRASE", None)
            try:
                root = Path(codex_home)
                (root / "sessions").mkdir()
                (root / "sessions" / "session.jsonl").write_text("resume-me", encoding="utf-8")

                sync_server.DATA_DIR = Path(data_dir).resolve()
                sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
                sync_server.init_db()
                token = "autopilot-token"
                sync_server.SyncHandler.token = token
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), sync_server.SyncHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                cfg = AppConfig(
                    server_url=f"http://127.0.0.1:{httpd.server_port}",
                    api_token=token,
                    device_id="laptop-a",
                    full_backup_auto_upload=True,
                )
                try:
                    scan = scan_full_backup_changes(
                        cfg, create_package=True, force=True, upload=True, notify_dirty=True, check_remote=True
                    )
                    self.assertTrue(scan["package"]["created"])
                    self.assertTrue(scan["package"]["encrypted"])
                    self.assertTrue(scan["upload"]["success"], scan.get("upload"))

                    # Simulate first launch on another machine: local conversation is gone.
                    (root / "sessions" / "session.jsonl").unlink()
                    restored = restore_latest_full_backup(cfg)
                    self.assertTrue(restored["success"], restored)
                    self.assertEqual(restored["selected"]["device_id"], "laptop-a")
                    self.assertEqual((root / "sessions" / "session.jsonl").read_text(encoding="utf-8"), "resume-me")
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)
            finally:
                sync_server.DATA_DIR = old_data_dir
                sync_server.DB_PATH = old_db_path
                for var, old in (
                    ("CODEX_SYNC_HOME", old_sync),
                    ("CODEX_HOME", old_codex),
                    ("CODEX_SYNC_FULL_BACKUP_PASSPHRASE", old_pass),
                ):
                    if old is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = old


if __name__ == "__main__":
    unittest.main()
