import os
import tempfile
import unittest
from pathlib import Path

from codex_sync.collector import collect_codex_state
from codex_sync.config import AppConfig


class SecurityFixTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
