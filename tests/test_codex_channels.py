import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import codex_channels
from codex_sync.config import AppConfig


# 复刻真实 threads 表的关键结构（含 rollout_path）与「只监听 updated_at」的时间戳触发器
THREADS_DDL = """
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  model_provider TEXT,
  title TEXT,
  cwd TEXT,
  rollout_path TEXT NOT NULL,
  created_at INTEGER,
  updated_at INTEGER,
  created_at_ms INTEGER,
  updated_at_ms INTEGER
);
CREATE TRIGGER threads_updated_at_ms_after_update
AFTER UPDATE OF updated_at ON threads
WHEN NEW.updated_at != OLD.updated_at AND NEW.updated_at_ms IS OLD.updated_at_ms
BEGIN
  UPDATE threads SET updated_at_ms = NEW.updated_at * 1000 WHERE id = NEW.id;
END;
"""

# id, model_provider, title, cwd, created_at, updated_at, created_at_ms, updated_at_ms
SEED = [
    ("t1", "openai", "O1", "/p1", 100, 200, 100000, 200000),
    ("t2", "openai", "O2", "/p2", 101, 201, 101000, 201000),
    ("t3", "custom", "C1", "/p3", 102, 202, 102000, 202000),
    ("t4", "anyrouter", "A1", "/p4", 103, 203, 103000, 203000),
]


class ChannelMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._old_home = os.environ.get("CODEX_SYNC_HOME")
        os.environ["CODEX_SYNC_HOME"] = str(base / "app")  # 隔离 codex_sync 状态目录

        self.db = base / "state_5.sqlite"
        self.rollout_dir = base / "sessions"
        self.rollout_dir.mkdir(parents=True, exist_ok=True)

        con = sqlite3.connect(self.db)
        con.executescript(THREADS_DDL)
        for tid, prov, title, cwd, ca, ua, cam, uam in SEED:
            rp = self.rollout_dir / f"rollout-{tid}.jsonl"
            # 第一行 session_meta（权威源），其后两行普通内容（含中文，用于验证只改首行、不动其余）
            lines = [
                json.dumps(
                    {
                        "timestamp": "2025-01-01T00:00:00.000Z",
                        "type": "session_meta",
                        "payload": {"id": tid, "cwd": cwd, "source": "vscode", "model_provider": prov},
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"type": "response_item", "payload": {"text": f"内容 {tid} 你好"}}, ensure_ascii=False),
                json.dumps({"type": "event_msg", "payload": {"message": "结束"}}, ensure_ascii=False),
            ]
            rp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            con.execute(
                "INSERT INTO threads (id,model_provider,title,cwd,rollout_path,created_at,updated_at,created_at_ms,updated_at_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, prov, title, cwd, str(rp), ca, ua, cam, uam),
            )
        con.commit()
        con.close()

        self.patchers = [
            mock.patch.object(codex_channels, "codex_state_db", return_value=self.db),
            mock.patch.object(codex_channels, "codex_running", return_value=False),
            mock.patch.object(codex_channels, "current_codex_provider", return_value="custom"),
            mock.patch.object(codex_channels, "create_disaster_backup", return_value={"created": False}),
        ]
        for p in self.patchers:
            p.start()
        self.cfg = AppConfig()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        if self._old_home is None:
            os.environ.pop("CODEX_SYNC_HOME", None)
        else:
            os.environ["CODEX_SYNC_HOME"] = self._old_home
        self.tmp.cleanup()

    def _provider(self, tid: str) -> str:
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT model_provider FROM threads WHERE id=?", (tid,)).fetchone()[0]
        finally:
            con.close()

    def _updated_ms(self, tid: str) -> int:
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT updated_at_ms FROM threads WHERE id=?", (tid,)).fetchone()[0]
        finally:
            con.close()

    def _rollout_lines(self, tid: str) -> list[str]:
        rp = self.rollout_dir / f"rollout-{tid}.jsonl"
        return rp.read_text(encoding="utf-8").splitlines()

    def _rollout_provider(self, tid: str) -> str:
        return json.loads(self._rollout_lines(tid)[0])["payload"]["model_provider"]

    def test_merge_moves_and_records_origin(self) -> None:
        res = codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom")
        self.assertTrue(res["success"])
        self.assertEqual(res["moved"], 2)
        self.assertEqual(self._provider("t1"), "custom")
        self.assertEqual(self._provider("t2"), "custom")
        state = codex_channels._read_merge_state()
        self.assertEqual(state["t1"]["original_provider"], "openai")
        self.assertEqual(state["t1"]["current_provider"], "custom")
        self.assertTrue(state["t1"]["rollout_path"].endswith("rollout-t1.jsonl"))

    def test_merge_rewrites_rollout_authority(self) -> None:
        res = codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom")
        self.assertEqual(res["rollout"]["changed"], 2)
        self.assertEqual(res["rollout"]["failed"], [])
        # rollout 权威源已改写为目标渠道
        self.assertEqual(self._rollout_provider("t1"), "custom")
        self.assertEqual(self._rollout_provider("t2"), "custom")
        # 只改第一行：其余行原样保留、中文不损坏、行数不变
        lines = self._rollout_lines("t1")
        self.assertEqual(len(lines), 3)
        self.assertIn("内容 t1 你好", lines[1])
        self.assertEqual(json.loads(lines[2])["payload"]["message"], "结束")

    def test_merge_does_not_touch_timestamp(self) -> None:
        before = self._updated_ms("t1")
        codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom")
        self.assertEqual(self._updated_ms("t1"), before)  # 触发器未被误触发

    def test_all_others(self) -> None:
        res = codex_channels.merge_channels(self.cfg, all_others=True, target="custom")
        self.assertTrue(res["success"])
        self.assertEqual(res["moved"], 3)  # openai 2 + anyrouter 1，t3 本就是 custom
        for tid in ("t1", "t2", "t4"):
            self.assertEqual(self._provider(tid), "custom")
            self.assertEqual(self._rollout_provider(tid), "custom")

    def test_restore_all(self) -> None:
        codex_channels.merge_channels(self.cfg, all_others=True, target="custom")
        res = codex_channels.restore_channels(self.cfg, all_merged=True)
        self.assertTrue(res["success"])
        self.assertEqual(self._provider("t1"), "openai")
        self.assertEqual(self._provider("t4"), "anyrouter")
        # rollout 权威源也被还原
        self.assertEqual(self._rollout_provider("t1"), "openai")
        self.assertEqual(self._rollout_provider("t4"), "anyrouter")
        self.assertEqual(codex_channels._read_merge_state(), {})

    def test_restore_selected_origin(self) -> None:
        codex_channels.merge_channels(self.cfg, all_others=True, target="custom")
        codex_channels.restore_channels(self.cfg, sources=["openai"])
        self.assertEqual(self._provider("t1"), "openai")
        self.assertEqual(self._rollout_provider("t1"), "openai")
        self.assertEqual(self._provider("t4"), "custom")  # anyrouter 未还原
        self.assertEqual(self._rollout_provider("t4"), "custom")
        state = codex_channels._read_merge_state()
        self.assertNotIn("t1", state)
        self.assertIn("t4", state)

    def test_preserve_original_across_re_merge(self) -> None:
        codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom")
        codex_channels.merge_channels(self.cfg, sources=["custom"], target="anyrouter")
        state = codex_channels._read_merge_state()
        self.assertEqual(state["t1"]["original_provider"], "openai")  # 仍指向最初真实渠道
        self.assertEqual(state["t1"]["current_provider"], "anyrouter")
        self.assertEqual(self._provider("t1"), "anyrouter")
        self.assertEqual(self._rollout_provider("t1"), "anyrouter")

    def test_codex_running_blocks_write(self) -> None:
        with mock.patch.object(codex_channels, "codex_running", return_value=True):
            res = codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom")
        self.assertFalse(res["success"])
        self.assertTrue(res.get("needs_close"))  # 默认不关，提示上层弹确认
        self.assertIn("Codex", res["error"])
        self.assertEqual(self._provider("t1"), "openai")  # 未改动
        self.assertEqual(self._rollout_provider("t1"), "openai")  # rollout 也未改动

    def test_close_running_auto_closes_then_merges(self) -> None:
        with mock.patch.object(codex_channels, "codex_running", return_value=True), mock.patch.object(
            codex_channels, "stop_codex", return_value={"stopped": True, "still_running": False}
        ) as stop:
            res = codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom", close_running=True)
        stop.assert_called_once()  # 用户确认后调用 stop_codex 清场
        self.assertTrue(res["success"])
        self.assertEqual(res["moved"], 2)
        self.assertEqual(self._provider("t1"), "custom")
        self.assertEqual(self._rollout_provider("t1"), "custom")

    def test_close_running_but_still_running_aborts(self) -> None:
        with mock.patch.object(codex_channels, "codex_running", return_value=True), mock.patch.object(
            codex_channels, "stop_codex", return_value={"stopped": False, "still_running": True}
        ):
            res = codex_channels.merge_channels(self.cfg, sources=["openai"], target="custom", close_running=True)
        self.assertFalse(res["success"])
        self.assertTrue(res.get("needs_close"))
        self.assertEqual(self._provider("t1"), "openai")  # 关不掉则不动库

    def test_stop_codex_confirms_exit(self) -> None:
        with mock.patch.object(codex_channels, "run_cmd", return_value=(0, "", "")) as rc, mock.patch.object(
            codex_channels, "codex_running", return_value=False
        ):
            res = codex_channels.stop_codex()
        rc.assert_called()
        self.assertTrue(res["stopped"])
        self.assertFalse(res["still_running"])

    def test_list_channels(self) -> None:
        res = codex_channels.list_channels()
        self.assertTrue(res["success"])
        counts = {c["provider"]: c["threads"] for c in res["channels"]}
        self.assertEqual(counts, {"openai": 2, "custom": 1, "anyrouter": 1})
        self.assertEqual(res["current_provider"], "custom")

    def test_rollout_set_provider_handles_missing(self) -> None:
        res = codex_channels._rollout_set_provider(self.rollout_dir / "nope.jsonl", "custom")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "missing")


if __name__ == "__main__":
    unittest.main()
