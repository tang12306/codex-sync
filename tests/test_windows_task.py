import os
import tempfile
import unittest
from unittest import mock

from codex_sync import windows_task


class WindowsTaskTests(unittest.TestCase):
    def test_install_task_uses_schtasks_with_interval(self) -> None:
        with mock.patch("codex_sync.windows_task.run_cmd", return_value=(0, "ok", "")) as run:
            result = windows_task.install_windows_task(minutes=3)

        self.assertTrue(result["success"])
        args = run.call_args.args[0]
        self.assertEqual(args[0], "schtasks.exe")
        self.assertIn("/SC", args)
        self.assertIn("MINUTE", args)
        self.assertIn("/MO", args)
        self.assertIn("3", args)
        self.assertIn(windows_task.TASK_NAME, args)

    def test_frozen_exe_task_calls_itself(self) -> None:
        exe = r"C:\Apps\CodexSync\CodexSync.exe"
        with mock.patch.object(windows_task.sys, "frozen", True, create=True), \
             mock.patch.object(windows_task.sys, "executable", exe), \
             mock.patch("codex_sync.windows_task._ensure_launcher") as launcher:
            command = windows_task._task_command()
        launcher.assert_not_called()
        self.assertTrue(command.endswith('" sync-now'))
        self.assertIn("CodexSync.exe", command)

    def test_source_launcher_calls_valid_sync_now(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = d
            try:
                text = windows_task._ensure_launcher().read_text(encoding="utf-8")
            finally:
                if old is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old
        self.assertIn('main(["sync-now"])', text)


if __name__ == "__main__":
    unittest.main()

