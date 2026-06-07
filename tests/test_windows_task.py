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


if __name__ == "__main__":
    unittest.main()

