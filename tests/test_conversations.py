import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import codex_channels, conversations, wsl
from codex_sync.config import AppConfig


THREADS_DDL = """
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  model_provider TEXT,
  title TEXT,
  preview TEXT,
  first_user_message TEXT,
  cwd TEXT,
  rollout_path TEXT NOT NULL,
  created_at INTEGER,
  updated_at INTEGER,
  archived INTEGER DEFAULT 0
);
"""

# id, provider, title, preview, first_user_message, cwd, created_at, updated_at
SEED = [
    ("c1", "custom", "算法题", "快速排序", "帮我写快速排序", "/projA", 100, 200),
    ("c2", "openai", "加密讲解", "AES 原理", "讲讲 AES", "/projB", 101, 201),
    ("a1", "anyrouter", "打个招呼", "hi", "你好", "/projA", 102, 202),
]


class ConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._old_home = os.environ.get("CODEX_SYNC_HOME")
        os.environ["CODEX_SYNC_HOME"] = str(base / "app")

        self.db = base / "state_5.sqlite"
        self.wsl_db = base / "wsl_state_9.sqlite"
        sessions = base / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db)
        con.executescript(THREADS_DDL)
        for tid, prov, title, preview, fum, cwd, ca, ua in SEED:
            rp = sessions / f"rollout-{tid}.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": cwd, "model_provider": prov}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions> 系统注入说明"}]}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": fum}]}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{\"cmd\":\"ls\"}"}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "file-list"}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "这是回复 " + title}]}}, ensure_ascii=False),
                json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "事件流应被忽略"}}, ensure_ascii=False),
            ]
            rp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            con.execute(
                "INSERT INTO threads (id,model_provider,title,preview,first_user_message,cwd,rollout_path,created_at,updated_at,archived)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                (tid, prov, title, preview, fum, cwd, str(rp), ca, ua),
            )
        con.commit()
        con.close()

        self.wsl_rollout = sessions / "rollout-w1.jsonl"
        self.wsl_rollout.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session_meta", "payload": {"id": "w1", "cwd": "/home/me/proj", "model_provider": "openai"}}, ensure_ascii=False),
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "WSL 里的问题"}]}}, ensure_ascii=False),
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "WSL 里的回复"}]}}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        wsl_con = sqlite3.connect(self.wsl_db)
        wsl_con.executescript(THREADS_DDL)
        wsl_con.execute(
            "INSERT INTO threads (id,model_provider,title,preview,first_user_message,cwd,rollout_path,created_at,updated_at,archived)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            ("w1", "openai", "WSL 会话", "WSL 预览", "WSL 里的问题", "/home/me/proj", "/home/me/.codex/sessions/rollout-w1.jsonl", 300, 400),
        )
        wsl_con.commit()
        wsl_con.close()

        self.patchers = [
            mock.patch.object(codex_channels, "codex_state_db", return_value=self.db),
            mock.patch.object(conversations, "codex_state_db", return_value=self.db),
            mock.patch.object(codex_channels, "codex_running", return_value=False),
            mock.patch.object(codex_channels, "current_codex_provider", return_value="custom"),
            mock.patch.object(conversations, "current_codex_provider", return_value="custom"),
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

    def test_list_basic(self) -> None:
        res = conversations.list_conversations()
        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 3)
        self.assertEqual(set(res["providers"]), {"custom", "openai", "anyrouter"})
        self.assertEqual(res["current_provider"], "custom")
        # 默认按 updated_at 倒序：a1(202) 在最前
        self.assertEqual(res["conversations"][0]["id"], "a1")

    def test_list_search(self) -> None:
        res = conversations.list_conversations(search="加密")
        self.assertEqual([c["id"] for c in res["conversations"]], ["c2"])

    def test_list_filter_provider(self) -> None:
        res = conversations.list_conversations(provider="custom")
        self.assertEqual([c["id"] for c in res["conversations"]], ["c1"])

    def test_read_conversation_messages(self) -> None:
        res = conversations.read_conversation("c1")  # include_tools 默认 True
        self.assertTrue(res["success"])
        roles = [m["role"] for m in res["messages"]]
        self.assertEqual(roles, ["user", "tool_call", "tool_output", "assistant"])  # event_msg 被忽略
        self.assertEqual(res["messages"][0]["text"], "帮我写快速排序")
        self.assertIn("这是回复 算法题", res["messages"][-1]["text"])

    def test_read_conversation_no_tools(self) -> None:
        res = conversations.read_conversation("c1", include_tools=False)
        self.assertEqual([m["role"] for m in res["messages"]], ["user", "assistant"])

    def test_developer_excluded_by_default(self) -> None:
        default = conversations.read_conversation("c1")
        self.assertNotIn("developer", [m["role"] for m in default["messages"]])
        with_sys = conversations.read_conversation("c1", include_developer=True)
        self.assertEqual(with_sys["messages"][0]["role"], "developer")

    def test_export_markdown(self) -> None:
        res = conversations.export_conversation("c1")  # include_tools 默认 False
        self.assertTrue(res["success"])
        text = Path(res["path"]).read_text(encoding="utf-8")
        self.assertTrue(res["path"].endswith(".md"))
        self.assertIn("# 算法题", text)
        self.assertIn("## 用户", text)
        self.assertIn("帮我写快速排序", text)
        self.assertIn("## 助手", text)
        self.assertNotIn("工具调用", text)  # 默认不含工具
        self.assertNotIn("系统注入", text)  # 默认不含 developer 系统注入

    def test_export_json(self) -> None:
        res = conversations.export_conversation("c1", fmt="json")
        self.assertTrue(res["path"].endswith(".json"))
        data = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["thread"]["id"], "c1")

    def test_merge_threads(self) -> None:
        res = codex_channels.merge_threads(self.cfg, ["a1"], target="custom")
        self.assertTrue(res["success"])
        self.assertEqual(res["moved"], 1)
        self.assertEqual(self._provider("a1"), "custom")
        # rollout 权威源也改了
        first = json.loads((Path(self.tmp.name) / "sessions" / "rollout-a1.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["payload"]["model_provider"], "custom")
        state = codex_channels._read_merge_state()
        self.assertEqual(state["a1"]["original_provider"], "anyrouter")

    def test_merge_threads_skips_already_target(self) -> None:
        res = codex_channels.merge_threads(self.cfg, ["c1"], target="custom")  # c1 本就是 custom
        self.assertFalse(res["success"])

    def test_restore_threads(self) -> None:
        codex_channels.merge_threads(self.cfg, ["a1"], target="custom")
        res = codex_channels.restore_threads(self.cfg, ["a1"])
        self.assertTrue(res["success"])
        self.assertEqual(self._provider("a1"), "anyrouter")
        self.assertNotIn("a1", codex_channels._read_merge_state())

    def _mock_wsl_read_file(self, distro: str, rel: str) -> tuple[bytes | None, str | None]:
        if distro != "Ubuntu":
            return None, f"unexpected distro {distro}"
        if rel == "state_9.sqlite":
            return self.wsl_db.read_bytes(), None
        if rel == "sessions/rollout-w1.jsonl":
            return self.wsl_rollout.read_bytes(), None
        if rel == "config.toml":
            return b'model_provider = "openai"\n', None
        return None, f"missing {rel}"

    def test_list_wsl_conversations_marks_home(self) -> None:
        with (
            mock.patch.object(wsl, "_wsl_state_db_rel", return_value="state_9.sqlite"),
            mock.patch.object(wsl, "_read_wsl_file", side_effect=self._mock_wsl_read_file),
        ):
            res = wsl.list_wsl_conversations("Ubuntu")

        self.assertTrue(res["success"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["current_provider"], "openai")
        self.assertEqual(res["conversations"][0]["id"], "w1")
        self.assertEqual(res["conversations"][0]["home_id"], "wsl:Ubuntu")
        self.assertEqual(res["providers"], ["openai"])

    def test_read_wsl_conversation_messages(self) -> None:
        with (
            mock.patch.object(wsl, "_wsl_state_db_rel", return_value="state_9.sqlite"),
            mock.patch.object(wsl, "_wsl_codex_home", return_value="/home/me/.codex"),
            mock.patch.object(wsl, "_read_wsl_file", side_effect=self._mock_wsl_read_file),
        ):
            res = wsl.read_wsl_conversation("Ubuntu", "w1", include_tools=False)

        self.assertTrue(res["success"])
        self.assertEqual(res["thread"]["home_id"], "wsl:Ubuntu")
        self.assertEqual([m["role"] for m in res["messages"]], ["user", "assistant"])
        self.assertEqual(res["messages"][0]["text"], "WSL 里的问题")
        self.assertEqual(res["messages"][1]["text"], "WSL 里的回复")


if __name__ == "__main__":
    unittest.main()
