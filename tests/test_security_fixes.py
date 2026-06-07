import os
import tempfile
import unittest
from pathlib import Path

import sync_server
from codex_sync.collector import collect_codex_state
from codex_sync.config import AppConfig
from codex_sync import server as client_server


class SecurityFixTests(unittest.TestCase):
    def test_server_uses_sqlite_for_snapshot_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_data_dir = sync_server.DATA_DIR
            old_db_path = sync_server.DB_PATH
            sync_server.DATA_DIR = Path(tmp).resolve()
            sync_server.DB_PATH = sync_server.DATA_DIR / "snapshots.sqlite3"
            try:
                sync_server.init_db()
                result = sync_server.save_snapshot(
                    {
                        "id": r"x\..\..\evil",
                        "device_id": r"device\..\escape",
                        "created_at": r"2026-01-01\..\bad",
                        "cwd": "c:/work",
                        "type": "codex_sync_snapshot",
                    }
                )
                self.assertEqual(result["snapshot_id"], r"x\..\..\evil")
                fetched = sync_server.get_snapshot(r"x\..\..\evil")
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched["cwd"], "c:/work")
                self.assertEqual(len(list(Path(tmp).glob("**/*.json"))), 0)
            finally:
                sync_server.DATA_DIR = old_data_dir
                sync_server.DB_PATH = old_db_path

    def test_collector_does_not_upload_raw_config_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                secret = "sk-testabcdefghijklmnopqrstuvwxyz123456"
                Path(codex_home, "config.toml").write_text(f"api_key = '{secret}'\n", encoding="utf-8")

                state = collect_codex_state(AppConfig())

                self.assertEqual(state["configs"], {})
                preview = state["config_files"]["config.toml"]["redacted_preview"]
                self.assertNotIn(secret, preview)
                self.assertIn("<redacted>", preview)
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_restore_requires_confirmation_and_skips_hooks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_get = client_server.get_from_server
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                snapshot_id = "snap-restore-1"

                def fake_get(_config, _path, timeout=15):
                    return {
                        "id": snapshot_id,
                        "codex": {
                            "configs": {
                                "config.toml": "model = 'test'\n",
                                "hooks.json": '{"hooks": {"Stop": []}}',
                            }
                        },
                    }

                client_server.get_from_server = fake_get

                denied = client_server.restore_snapshot_locally(AppConfig(server_url="http://example"), snapshot_id)
                self.assertFalse(denied["success"])
                self.assertIn("confirm_snapshot_id", denied["error"])

                restored = client_server.restore_snapshot_locally(
                    AppConfig(server_url="http://example"),
                    snapshot_id,
                    confirm_snapshot_id=snapshot_id,
                )
                self.assertTrue(restored["success"])
                self.assertIn("config.toml", restored["restored"])
                self.assertIn("hooks.json", restored["skipped"])
                self.assertTrue(Path(codex_home, "config.toml").exists())
                self.assertFalse(Path(codex_home, "hooks.json").exists())
            finally:
                client_server.get_from_server = old_get
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync
                if old_codex is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = old_codex

    def test_preview_restore_returns_diff_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home, tempfile.TemporaryDirectory() as codex_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            old_codex = os.environ.get("CODEX_HOME")
            old_get = client_server.get_from_server
            os.environ["CODEX_SYNC_HOME"] = sync_home
            os.environ["CODEX_HOME"] = codex_home
            try:
                snapshot_id = "snap-preview-1"
                local_secret = "sk-localabcdefghijklmnopqrstuvwxyz123456"
                remote_secret = "sk-remoteabcdefghijklmnopqrstuvwxyz123456"
                Path(codex_home, "config.toml").write_text(f"model = 'local'\napi_key = '{local_secret}'\n", encoding="utf-8")

                def fake_get(_config, _path, timeout=15):
                    return {"id": snapshot_id, "codex": {"configs": {"config.toml": f"model = 'remote'\napi_key = '{remote_secret}'\n"}}}

                client_server.get_from_server = fake_get
                preview = client_server.preview_restore_snapshot(AppConfig(server_url="http://example"), snapshot_id)
                self.assertTrue(preview["success"])
                self.assertTrue(preview["files"][0]["changed"])
                diff = preview["files"][0]["diff"]
                self.assertIn("-model = 'local'", diff)
                self.assertIn("+model = 'remote'", diff)
                self.assertNotIn(local_secret, diff)
                self.assertNotIn(remote_secret, diff)
                self.assertIn("api_key=<redacted>", diff)
                self.assertEqual(Path(codex_home, "config.toml").read_text(encoding="utf-8"), f"model = 'local'\napi_key = '{local_secret}'\n")
            finally:
                client_server.get_from_server = old_get
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
