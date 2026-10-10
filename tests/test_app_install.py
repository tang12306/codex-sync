"""Tests for install status / startup / uninstall cleanup after switching to the Inno Setup installer."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import app_install, app_update, hooks, web_desktop, windows_task


ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "installer" / "CodexSync.iss").read_text(encoding="utf-8")


def _iss_normal_define(name: str) -> str:
    """取 .iss 里非 TestMode 分支（#else 之后）或全局的 #define 值。"""
    text = ISS.split("#else", 1)[1] if name in ("AppIdGuid", "AppName") else ISS
    match = re.search(rf'#define\s+{name}\s+"([^"]*)"', text)
    assert match, name
    return match.group(1)


class InstallerContractTest(unittest.TestCase):
    """安装程序和 Python 端共用的标识必须一致，否则状态读取 / 自启 / 关闭程序会失效。"""

    def test_app_id_matches_installer(self) -> None:
        self.assertEqual(app_install.APP_ID, "{" + _iss_normal_define("AppIdGuid") + "}")

    def test_mutex_matches_single_instance(self) -> None:
        self.assertEqual(_iss_normal_define("AppMutexName"), web_desktop.SingleInstance().name)

    def test_run_value_matches_startup_value(self) -> None:
        self.assertEqual(_iss_normal_define("RunValueName"), app_install.STARTUP_VALUE)
        self.assertEqual(_iss_normal_define("AppExeName"), app_install.EXE_NAME)


class InstallStatusTest(unittest.TestCase):
    def test_status_reads_uninstall_values(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            app_dir = Path(d) / "CodexSync"
            app_dir.mkdir()
            (app_dir / "CodexSync.exe").write_bytes(b"MZ")
            uninstaller = app_dir / "unins000.exe"
            uninstaller.write_bytes(b"MZ")
            values = {
                "Inno Setup: App Path": str(app_dir),
                "DisplayVersion": "0.5.0",
                "UninstallString": f'"{uninstaller}"',
            }
            with mock.patch.object(app_install, "_read_uninstall_values", return_value=values), \
                 mock.patch.object(app_install, "_current_executable", return_value=Path(d) / "other.exe"), \
                 mock.patch.object(app_install, "startup_status", return_value={"supported": True, "enabled": False}):
                st = app_install.app_install_status()
                found = app_install.uninstaller_path()
        self.assertTrue(st["installed_available"])
        self.assertFalse(st["installed"])
        self.assertEqual(st["installed_version"], "0.5.0")
        self.assertEqual(st["install_dir"], str(app_dir))
        self.assertEqual(st["uninstaller"], str(uninstaller))
        self.assertEqual(found, uninstaller)

    def test_status_without_install(self) -> None:
        with mock.patch.object(app_install, "_read_uninstall_values", return_value={}), \
             mock.patch.object(app_install, "startup_status", return_value={"supported": True, "enabled": False}):
            st = app_install.app_install_status()
        self.assertFalse(st["installed_available"])
        self.assertIsNone(st["install_dir"])
        self.assertIsNone(st["installed_version"])

    def test_startup_requires_installed_exe(self) -> None:
        with mock.patch.object(app_install, "_is_windows", return_value=True), \
             mock.patch.object(app_install, "_read_uninstall_values", return_value={}):
            res = app_install.set_startup_enabled(True)
        self.assertFalse(res["success"])
        self.assertTrue(res["needs_install"])


class UninstallCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.tmp.name
        self.hooks_file = Path(self.tmp.name) / "hooks.json"

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_home
        self.tmp.cleanup()

    def _write_hooks(self, installed_exe: str) -> None:
        def entry(command: str) -> dict:
            return {"hooks": [{"type": "command", "commandWindows": command}]}

        data = {
            "hooks": {
                "Stop": [
                    entry(f'"{installed_exe}" capture --event Stop'),
                    entry(r"pythonw.exe C:\src\codex_sync\hook_runner.py capture --event Stop"),
                    entry("notify-me.exe"),
                ],
                "SessionStart": [entry(f'"{installed_exe}" capture --event SessionStart')],
            }
        }
        self.hooks_file.write_text(json.dumps(data), encoding="utf-8")

    def test_uninstall_hooks_only_removes_matching_exe(self) -> None:
        exe = r"C:\Users\me\AppData\Local\Programs\CodexSync\CodexSync.exe"
        self._write_hooks(exe)
        res = hooks.uninstall_hooks(match=exe)
        self.assertTrue(res["success"])
        self.assertEqual(sorted(res["removed"]), ["SessionStart", "Stop"])
        data = json.loads(self.hooks_file.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", data["hooks"])
        commands = [e["hooks"][0]["commandWindows"] for e in data["hooks"]["Stop"]]
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("hook_runner.py" in c for c in commands))
        self.assertIn("notify-me.exe", commands)

    def test_uninstall_hooks_without_match_removes_all_ours(self) -> None:
        self._write_hooks(r"C:\X\CodexSync.exe")
        hooks.uninstall_hooks()
        data = json.loads(self.hooks_file.read_text(encoding="utf-8"))
        self.assertEqual([e["hooks"][0]["commandWindows"] for e in data["hooks"]["Stop"]], ["notify-me.exe"])

    def test_cleanup_before_uninstall_removes_only_own_task(self) -> None:
        exe = Path(r"C:\Apps\CodexSync\CodexSync.exe")
        self._write_hooks(str(exe))
        other_task = {"installed": True, "task_to_run": r'"C:\Python\pythonw.exe" "C:\Users\me\.codex-sync\run-sync.pyw"'}
        with mock.patch.object(app_install, "_current_executable", return_value=exe), \
             mock.patch.object(windows_task, "windows_task_status", return_value=other_task), \
             mock.patch.object(windows_task, "uninstall_windows_task") as uninstall_task:
            res = app_install.cleanup_before_uninstall()
        uninstall_task.assert_not_called()
        self.assertEqual(res["task"], {"skipped": True})
        self.assertEqual(sorted(res["hooks"]["removed"]), ["SessionStart", "Stop"])

        own_task = {"installed": True, "task_to_run": f'"{exe}" sync-now'}
        with mock.patch.object(app_install, "_current_executable", return_value=exe), \
             mock.patch.object(windows_task, "windows_task_status", return_value=own_task), \
             mock.patch.object(windows_task, "uninstall_windows_task", return_value={"success": True}) as uninstall_task:
            res = app_install.cleanup_before_uninstall()
        uninstall_task.assert_called_once()
        self.assertEqual(res["task"], {"success": True})


class UpdateInstallerTest(unittest.TestCase):
    def test_rejects_paths_outside_download_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d) / "updates"
            downloads.mkdir()
            outside = Path(d) / "CodexSyncSetup-v9.9.9-windows-x64.exe"
            outside.write_bytes(b"MZ")
            wrong_name = downloads / "evil.exe"
            wrong_name.write_bytes(b"MZ")
            with mock.patch.object(app_update, "_default_download_dir", return_value=downloads), \
                 mock.patch.object(app_update.subprocess, "Popen") as popen:
                self.assertFalse(app_update.launch_update_installer(str(outside))["success"])
                self.assertFalse(app_update.launch_update_installer(str(wrong_name))["success"])
                self.assertFalse(app_update.launch_update_installer("")["success"])
                missing = app_update.launch_update_installer(str(downloads / "CodexSyncSetup-v1.0.0-windows-x64.exe"))
                self.assertFalse(missing["success"])
                popen.assert_not_called()

    def test_launches_downloaded_installer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            downloads = Path(d)
            setup = downloads / "CodexSyncSetup-v9.9.9-windows-x64.exe"
            setup.write_bytes(b"MZ")
            with mock.patch.object(app_update, "_default_download_dir", return_value=downloads), \
                 mock.patch.object(app_update.subprocess, "Popen") as popen:
                res = app_update.launch_update_installer(str(setup))
            self.assertTrue(res["success"])
            popen.assert_called_once()
            self.assertEqual(Path(popen.call_args.args[0][0]), setup.resolve())


if __name__ == "__main__":
    unittest.main()
