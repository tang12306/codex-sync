"""Tests for the 0.3.x follow-ups: setting an encryption passphrase auto-enables
encryption, and the shared codex-homes probe cache (incl. WSL) is reused by both
the overview homepage and the conversation workspace scan."""
import os
import tempfile
import unittest
from unittest import mock

from codex_sync import web_desktop


def _fake_homes_with_wsl() -> dict:
    return {
        "success": True,
        "homes": [
            {"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True},
            {"id": "wsl:Ubuntu", "kind": "wsl", "distro": "Ubuntu", "label": "WSL · Ubuntu", "ok": True, "has_codex": True},
        ],
    }


class PassphraseAutoEncryptTests(unittest.TestCase):
    def test_setting_passphrase_enables_encryption_and_disables_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = home
            try:
                from codex_sync.config import load_config
                from codex_sync.web_desktop import DesktopRuntime

                runtime = DesktopRuntime()
                # 先模拟旧的明文配置：关加密、开明文
                runtime.save_config({"full_backup_encryption_enabled": False, "full_backup_allow_plaintext_upload": True})
                cfg = load_config()
                self.assertFalse(cfg.full_backup_encryption_enabled)
                self.assertTrue(cfg.full_backup_allow_plaintext_upload)

                # 设置密码 → 自动转加密、关明文
                runtime.save_config({"full_backup_encryption_passphrase": "shared-secret"})
                cfg = load_config()
                self.assertTrue(cfg.full_backup_encryption_enabled)
                self.assertFalse(cfg.full_backup_allow_plaintext_upload)
                self.assertEqual(cfg.full_backup_encryption_passphrase, "shared-secret")
            finally:
                if old is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old

    def test_saving_without_passphrase_leaves_flags_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = home
            try:
                from codex_sync.config import load_config
                from codex_sync.web_desktop import DesktopRuntime

                runtime = DesktopRuntime()
                runtime.save_config({"full_backup_encryption_enabled": False, "full_backup_allow_plaintext_upload": True})
                # 不带 passphrase 的普通保存不应强行开加密
                runtime.save_config({"sync_interval_seconds": 120})
                cfg = load_config()
                self.assertFalse(cfg.full_backup_encryption_enabled)
                self.assertTrue(cfg.full_backup_allow_plaintext_upload)
            finally:
                if old is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old


class CodexHomesCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        web_desktop.invalidate_codex_homes_cache()

    def tearDown(self) -> None:
        web_desktop.invalidate_codex_homes_cache()

    def test_peek_empty_then_refresh_then_invalidate(self) -> None:
        self.assertIsNone(web_desktop._peek_codex_homes())
        fake = _fake_homes_with_wsl()
        with mock.patch.object(web_desktop, "list_codex_homes", return_value=fake) as lch:
            refreshed = web_desktop._refresh_codex_homes()
            lch.assert_called_once()
        self.assertEqual(refreshed, fake)
        peeked = web_desktop._peek_codex_homes()
        self.assertIsNotNone(peeked)
        self.assertIn("wsl:Ubuntu", [h["id"] for h in peeked["homes"]])
        web_desktop.invalidate_codex_homes_cache()
        self.assertIsNone(web_desktop._peek_codex_homes())

    def test_overview_reuses_shared_cache_with_wsl(self) -> None:
        with mock.patch.object(web_desktop, "list_codex_homes", return_value=_fake_homes_with_wsl()):
            web_desktop._refresh_codex_homes()
        # _peek 命中 → 不应再调 list_channels（也即无需快速清单兜底）
        with mock.patch.object(web_desktop, "list_channels", side_effect=AssertionError("should reuse cache")):
            overview = web_desktop._overview_codex_homes()
        self.assertIn("wsl:Ubuntu", [h["id"] for h in overview["homes"]])
        self.assertEqual(overview.get("homes_source"), "shared_cache")

    def test_conversation_scan_reuses_shared_cache_without_probe(self) -> None:
        with mock.patch.object(web_desktop, "list_codex_homes", return_value=_fake_homes_with_wsl()):
            web_desktop._refresh_codex_homes()
        with mock.patch.object(
            web_desktop,
            "_conversation_workspace_for_home",
            side_effect=lambda home: {"home": home, "conversations": {"success": True, "conversations": []}, "channels": {"success": True}},
        ):
            snapshot = web_desktop._scan_conversation_workspace(probe_homes=False)
        self.assertFalse(snapshot["probe_homes"])
        self.assertIn("wsl:Ubuntu", [h["id"] for h in snapshot["usable_homes"]])

    def test_conversation_scan_with_probe_refreshes_cache(self) -> None:
        self.assertIsNone(web_desktop._peek_codex_homes())
        with mock.patch.object(web_desktop, "list_codex_homes", return_value=_fake_homes_with_wsl()), \
             mock.patch.object(
                 web_desktop,
                 "_conversation_workspace_for_home",
                 side_effect=lambda home: {"home": home, "conversations": {"success": True, "conversations": []}, "channels": {"success": True}},
             ):
            snapshot = web_desktop._scan_conversation_workspace(probe_homes=True)
        self.assertTrue(snapshot["probe_homes"])
        # probe 之后共享缓存应被填充，供后续首页/对话命中
        peeked = web_desktop._peek_codex_homes()
        self.assertIsNotNone(peeked)
        self.assertIn("wsl:Ubuntu", [h["id"] for h in peeked["homes"]])


if __name__ == "__main__":
    unittest.main()
