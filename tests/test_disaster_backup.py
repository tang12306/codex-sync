import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from codex_sync.config import AppConfig
from codex_sync.disaster_backup import create_disaster_backup


class DisasterBackupTests(unittest.TestCase):
    def test_backup_includes_sessions_and_excludes_auth(self) -> None:
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

                result = create_disaster_backup(AppConfig(), reason="test", force=True)

                self.assertTrue(result["created"])
                with zipfile.ZipFile(result["archive"]) as zf:
                    names = set(zf.namelist())
                self.assertIn("codex/sessions/session.jsonl", names)
                self.assertIn("codex/session_index.jsonl", names)
                self.assertNotIn("codex/auth.json", names)
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
