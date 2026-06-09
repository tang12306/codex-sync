"""Tests for 0.3.1: packaged-exe hook commands, realtime backup config/daemon, and
realtime-backup add/remove actions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync.config import AppConfig, load_config


class HookCommandTests(unittest.TestCase):
    def test_codex_hook_uses_exe_when_frozen(self) -> None:
        from codex_sync import hooks

        with mock.patch.object(hooks.sys, "frozen", True, create=True), \
             mock.patch.object(hooks.sys, "executable", r"C:\\App\\CodexSync.exe"):
            cmd = hooks._command("Stop")
        self.assertIn("CodexSync.exe", cmd)
        self.assertIn("capture", cmd)
        self.assertIn("--event", cmd)
        self.assertNotIn("hook_runner.py", cmd)

    def test_codex_hook_uses_runner_in_source_mode(self) -> None:
        from codex_sync import hooks

        with mock.patch.object(hooks.sys, "frozen", False, create=True):
            cmd = hooks._command("Stop")
        self.assertIn("hook_runner.py", cmd)
        self.assertIn("capture", cmd)

    def test_git_post_commit_uses_exe_when_frozen(self) -> None:
        from codex_sync import project_auto_backup as pab

        with mock.patch.object(pab.sys, "frozen", True, create=True), \
             mock.patch.object(pab.sys, "executable", r"C:\\App\\CodexSync.exe"):
            script = pab._hook_script()
        self.assertIn("project-auto-backup", script)
        self.assertIn("git-post-commit", script)
        self.assertNotIn("hook_runner.py", script)

    def test_git_post_commit_uses_runner_in_source_mode(self) -> None:
        from codex_sync import project_auto_backup as pab

        with mock.patch.object(pab.sys, "frozen", False, create=True):
            script = pab._hook_script()
        self.assertIn("hook_runner.py", script)
        self.assertIn("project-auto-backup", script)


class RealtimeConfigTests(unittest.TestCase):
    def test_defaults_and_from_dict_roundtrip(self) -> None:
        cfg = AppConfig()
        self.assertEqual(cfg.realtime_backup_projects, [])
        self.assertEqual(cfg.realtime_backup_interval_seconds, 300)
        cfg2 = AppConfig.from_dict({"realtime_backup_projects": ["/a", "/b"], "realtime_backup_interval_seconds": 120})
        self.assertEqual(cfg2.realtime_backup_projects, ["/a", "/b"])
        self.assertEqual(cfg2.realtime_backup_interval_seconds, 120)


class RealtimeEnqueueTests(unittest.TestCase):
    def test_enqueue_respects_interval(self) -> None:
        from codex_sync import daemon

        cfg = AppConfig(realtime_backup_projects=["/proj"], realtime_backup_interval_seconds=300)
        last: dict[str, float] = {}
        calls: list[tuple] = []

        def fake_enqueue(path, config, reason="manual", mode=None):
            calls.append((path, reason))
            return {"queued": True}

        with mock.patch.object(daemon, "enqueue_project_auto_backup", fake_enqueue):
            with mock.patch.object(daemon.time, "monotonic", return_value=1000.0):
                daemon._enqueue_realtime_projects(cfg, last, None)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], ("/proj", "realtime"))
            # 间隔内：不重复入队
            with mock.patch.object(daemon.time, "monotonic", return_value=1100.0):
                daemon._enqueue_realtime_projects(cfg, last, None)
            self.assertEqual(len(calls), 1)
            # 超过间隔：再次入队
            with mock.patch.object(daemon.time, "monotonic", return_value=1400.0):
                daemon._enqueue_realtime_projects(cfg, last, None)
            self.assertEqual(len(calls), 2)

    def test_no_projects_is_noop(self) -> None:
        from codex_sync import daemon

        cfg = AppConfig(realtime_backup_projects=[])
        with mock.patch.object(daemon, "enqueue_project_auto_backup", side_effect=AssertionError("should not enqueue")):
            daemon._enqueue_realtime_projects(cfg, {}, None)


class RealtimeActionTests(unittest.TestCase):
    def test_add_then_remove(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as proj:
            old = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = home
            try:
                from codex_sync.web_desktop import DesktopRuntime, _normalize_project_path

                runtime = DesktopRuntime()
                norm = _normalize_project_path(proj)

                r1 = runtime.run_action("realtime-backup-add", {"project_path": proj})
                self.assertTrue(r1["success"])
                self.assertTrue(r1["in_realtime"])
                self.assertIn(norm, load_config().realtime_backup_projects)

                # 重复 add 不产生重复
                runtime.run_action("realtime-backup-add", {"project_path": proj})
                self.assertEqual(load_config().realtime_backup_projects.count(norm), 1)

                r2 = runtime.run_action("realtime-backup-remove", {"project_path": proj})
                self.assertTrue(r2["success"])
                self.assertFalse(r2["in_realtime"])
                self.assertNotIn(norm, load_config().realtime_backup_projects)
            finally:
                if old is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old


class HookDetectionTests(unittest.TestCase):
    def test_matches_frozen_exe_command(self) -> None:
        from codex_sync import hooks

        entry = {"hooks": [{
            "commandWindows": r'"C:\App\CodexSync.exe" capture --event Stop --sync',
            "command": "python3 -m codex_sync capture --event Stop --sync",
        }]}
        self.assertTrue(hooks._contains_codex_sync(entry))

    def test_matches_source_runner_command(self) -> None:
        from codex_sync import hooks

        entry = {"hooks": [{
            "commandWindows": r'pythonw.exe C:\src\codex_sync\hook_runner.py capture --event Stop',
            "command": "python3 -m codex_sync capture --event Stop --sync",
        }]}
        self.assertTrue(hooks._contains_codex_sync(entry))

    def test_ignores_unrelated_command(self) -> None:
        from codex_sync import hooks

        entry = {"hooks": [{"commandWindows": r"C:\other\tool.exe run", "command": "echo hi"}]}
        self.assertFalse(hooks._contains_codex_sync(entry))


class EncryptionStatusKeyIdTests(unittest.TestCase):
    def test_key_id_present_when_passphrase_set(self) -> None:
        from codex_sync import full_backup

        with tempfile.TemporaryDirectory() as home:
            old_home = os.environ.get("CODEX_SYNC_HOME")
            old_env = os.environ.pop(full_backup.ENCRYPTION_ENV_VAR, None)
            os.environ["CODEX_SYNC_HOME"] = home
            try:
                cfg = AppConfig(full_backup_encryption_passphrase="hunter2")
                st = full_backup.encryption_status(cfg)
                self.assertTrue(st["configured"])
                self.assertEqual(st["source"], "config")
                self.assertEqual(st["key_id"], full_backup._encryption_key_id(b"hunter2"))
                self.assertEqual(len(st["key_id"]), 16)
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_home
                if old_env is not None:
                    os.environ[full_backup.ENCRYPTION_ENV_VAR] = old_env

    def test_key_id_empty_when_not_configured(self) -> None:
        from codex_sync import full_backup

        with tempfile.TemporaryDirectory() as home:
            old_home = os.environ.get("CODEX_SYNC_HOME")
            old_env = os.environ.pop(full_backup.ENCRYPTION_ENV_VAR, None)
            os.environ["CODEX_SYNC_HOME"] = home
            try:
                cfg = AppConfig()
                st = full_backup.encryption_status(cfg)
                self.assertFalse(st["configured"])
                self.assertEqual(st["key_id"], "")
            finally:
                if old_home is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_home
                if old_env is not None:
                    os.environ[full_backup.ENCRYPTION_ENV_VAR] = old_env


if __name__ == "__main__":
    unittest.main()
