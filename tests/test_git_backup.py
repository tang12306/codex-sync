import time
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import git_backup


class GitCacheTests(unittest.TestCase):
    """git_root / git_state 的进程内短期缓存：命中、TTL 失效、max_age=0 绕过、返回值隔离。"""

    def setUp(self) -> None:
        git_backup._git_root_cache.clear()
        git_backup._git_state_cache.clear()

    def tearDown(self) -> None:
        git_backup._git_root_cache.clear()
        git_backup._git_state_cache.clear()

    def test_git_state_caches_within_ttl(self) -> None:
        with mock.patch.object(git_backup, "_compute_git_state", return_value={"is_repo": True, "root": "/r", "untracked": []}) as compute:
            git_backup.git_state("/r", max_age=30)
            git_backup.git_state("/r", max_age=30)
        self.assertEqual(compute.call_count, 1)  # 第二次命中缓存，不重跑 git 子进程

    def test_git_state_max_age_zero_bypasses_cache(self) -> None:
        with mock.patch.object(git_backup, "_compute_git_state", return_value={"is_repo": False}) as compute:
            git_backup.git_state("/r", max_age=0)
            git_backup.git_state("/r", max_age=0)
        self.assertEqual(compute.call_count, 2)  # 写操作路径每次实时重算

    def test_git_state_ttl_expiry_recomputes(self) -> None:
        with mock.patch.object(git_backup, "_compute_git_state", return_value={"is_repo": True, "root": "/r", "untracked": []}) as compute:
            git_backup.git_state("/r", max_age=0.01)
            time.sleep(0.03)
            git_backup.git_state("/r", max_age=0.01)
        self.assertEqual(compute.call_count, 2)  # TTL 过期后重算

    def test_git_state_returns_copy_isolated_from_cache(self) -> None:
        shared = {"is_repo": True, "root": "/r", "untracked": []}
        with mock.patch.object(git_backup, "_compute_git_state", return_value=shared):
            first = git_backup.git_state("/r", max_age=30)
        first["root"] = "MUTATED"  # 调用方修改返回值
        with mock.patch.object(git_backup, "_compute_git_state", side_effect=AssertionError("should hit cache, not recompute")):
            second = git_backup.git_state("/r", max_age=30)
        self.assertEqual(second["root"], "/r")  # 缓存未被外部修改污染

    def test_git_root_caches_within_ttl(self) -> None:
        with mock.patch.object(git_backup, "_compute_git_root", return_value=Path("/r")) as compute:
            git_backup.git_root("/r", max_age=30)
            git_backup.git_root("/r", max_age=30)
        self.assertEqual(compute.call_count, 1)

    def test_git_root_max_age_zero_bypasses_cache(self) -> None:
        with mock.patch.object(git_backup, "_compute_git_root", return_value=Path("/r")) as compute:
            git_backup.git_root("/r", max_age=0)
            git_backup.git_root("/r", max_age=0)
        self.assertEqual(compute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
