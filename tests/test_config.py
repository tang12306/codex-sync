import os
import tempfile
import unittest

from codex_sync.config import AppConfig, load_config, save_config


class ConfigTests(unittest.TestCase):
    def test_round_trip_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = tmp
            try:
                cfg = load_config()
                cfg.server_url = "https://example.test"
                save_config(cfg)
                loaded = load_config()
                self.assertEqual(loaded.server_url, "https://example.test")
            finally:
                if old is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old

    def test_public_config_redacts_api_token(self) -> None:
        cfg = AppConfig(api_token="secret-token", full_backup_encryption_passphrase="backup-secret")
        public = cfg.to_public_dict()
        self.assertEqual(public["api_token"], "<configured>")
        self.assertEqual(public["full_backup_encryption_passphrase"], "<configured>")
        self.assertTrue(public["full_backup_encryption_passphrase_configured"])
        self.assertEqual(cfg.to_dict()["api_token"], "secret-token")
        self.assertEqual(cfg.to_dict()["full_backup_encryption_passphrase"], "backup-secret")

    def test_desktop_close_behavior_defaults_to_ask(self) -> None:
        cfg = AppConfig.from_dict({"desktop_close_behavior": "invalid"})
        self.assertEqual(cfg.desktop_close_behavior, "ask")

        cfg = AppConfig.from_dict({"desktop_close_behavior": "minimize_to_tray"})
        self.assertEqual(cfg.desktop_close_behavior, "minimize_to_tray")


if __name__ == "__main__":
    unittest.main()
