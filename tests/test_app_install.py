"""Tests for the 0.3.0 installer: custom install location, progress callback,
version-aware status, and uninstall. All system side effects (registry,
shortcuts, process stop, directory removal) are mocked so the suite is safe and
cross-platform."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import __version__, app_install


class CopyWithProgressTests(unittest.TestCase):
    def test_copies_bytes_and_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.bin"
            dst = Path(d) / "out" / "dst.bin"
            src.write_bytes(b"x" * (3 * 1024 * 1024))  # 3 MB → multiple chunks
            events: list[float] = []
            app_install._copy_with_progress(src, dst, lambda f, m: events.append(f), 0.1, 0.7)
            self.assertEqual(dst.read_bytes(), src.read_bytes())
            self.assertTrue(events)
            self.assertTrue(all(0.1 <= f <= 0.7 for f in events))
            self.assertGreaterEqual(events[-1], 0.6)


class InstallAppTests(unittest.TestCase):
    def test_installs_to_custom_target_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fake_exe = Path(d) / "CodexSyncSetup.exe"
            fake_exe.write_bytes(b"FAKE-EXE-CONTENT")
            target_root = Path(d) / "Custom" / "CodexSync"
            events: list[tuple[float, str]] = []
            with mock.patch.object(app_install, "_is_windows", return_value=True), \
                 mock.patch.object(app_install, "_is_frozen_exe", return_value=True), \
                 mock.patch.object(app_install, "_current_executable", return_value=fake_exe), \
                 mock.patch.object(app_install, "_stop_processes_for_executable", return_value={"success": True, "stopped": []}), \
                 mock.patch.object(app_install, "_create_shortcut", return_value={"success": True, "path": "x"}), \
                 mock.patch.object(app_install, "_write_software_values") as wsv, \
                 mock.patch.object(app_install, "_register_uninstall") as reg, \
                 mock.patch.object(app_install, "startup_status", return_value={"supported": True, "enabled": False}), \
                 mock.patch.object(app_install, "app_install_status", return_value={}):
                res = app_install.install_app(
                    target_dir=str(target_root),
                    progress=lambda f, m: events.append((f, m)),
                )
            self.assertTrue(res["success"])
            self.assertTrue(res["copied"])
            self.assertEqual(res["install_dir"], str(target_root))
            self.assertEqual((target_root / "CodexSync.exe").read_bytes(), b"FAKE-EXE-CONTENT")
            self.assertTrue(events)
            self.assertEqual(events[-1][0], 1.0)
            wsv.assert_called_once()
            written = wsv.call_args.args[0]
            self.assertEqual(written["InstallDir"], str(target_root))
            self.assertEqual(written["Version"], __version__)
            reg.assert_called_once()

    def test_unwritable_target_returns_friendly_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fake_exe = Path(d) / "setup.exe"
            fake_exe.write_bytes(b"x")
            with mock.patch.object(app_install, "_is_windows", return_value=True), \
                 mock.patch.object(app_install, "_is_frozen_exe", return_value=True), \
                 mock.patch.object(app_install, "_current_executable", return_value=fake_exe), \
                 mock.patch.object(Path, "mkdir", side_effect=OSError("denied")):
                res = app_install.install_app(target_dir=str(Path(d) / "blocked"))
            self.assertFalse(res["success"])
            self.assertTrue(res["needs_other_location"])


class StatusTests(unittest.TestCase):
    def test_reports_installed_version_and_update_available(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            app_dir = Path(d) / "app"
            app_dir.mkdir()
            (app_dir / "CodexSync.exe").write_bytes(b"x")
            with mock.patch.object(app_install, "install_dir", return_value=app_dir), \
                 mock.patch.object(app_install, "installed_version", return_value="0.2.0"), \
                 mock.patch.object(app_install, "_current_executable", return_value=Path(d) / "other.exe"), \
                 mock.patch.object(app_install, "startup_status", return_value={"supported": True, "enabled": False}):
                st = app_install.app_install_status()
            self.assertEqual(st["installed_version"], "0.2.0")
            self.assertTrue(st["installed_available"])
            self.assertFalse(st["installed"])
            self.assertTrue(st["update_available"])
            self.assertEqual(st["version"], __version__)


class UninstallTests(unittest.TestCase):
    def test_removes_shortcuts_and_schedules_dir_removal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            app_dir = base / "CodexSync"
            app_dir.mkdir()
            desktop = base / "d.lnk"
            start_menu = base / "s.lnk"
            uninstall = base / "u.lnk"
            for shortcut in (desktop, start_menu, uninstall):
                shortcut.write_text("lnk", encoding="utf-8")
            with mock.patch.object(app_install, "_is_windows", return_value=True), \
                 mock.patch.object(app_install, "install_dir", return_value=app_dir), \
                 mock.patch.object(app_install, "_desktop_shortcut", return_value=desktop), \
                 mock.patch.object(app_install, "_start_menu_shortcut", return_value=start_menu), \
                 mock.patch.object(app_install, "_uninstall_shortcut", return_value=uninstall), \
                 mock.patch.object(app_install, "_stop_processes_for_executable", return_value={"success": True, "stopped": []}), \
                 mock.patch.object(app_install, "set_startup_enabled", return_value={"success": True}), \
                 mock.patch.object(app_install, "_unregister_uninstall") as unreg, \
                 mock.patch.object(app_install, "_delete_software_key") as delkey, \
                 mock.patch.object(app_install, "_schedule_dir_removal", return_value=True) as sched:
                res = app_install.uninstall_app()
            self.assertTrue(res["success"])
            self.assertFalse(desktop.exists())
            self.assertFalse(start_menu.exists())
            self.assertFalse(uninstall.exists())
            self.assertEqual(len(res["removed_shortcuts"]), 3)
            sched.assert_called_once_with(app_dir)
            unreg.assert_called_once()
            delkey.assert_called_once()


if __name__ == "__main__":
    unittest.main()
