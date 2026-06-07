import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from codex_sync import backup_import, codex_channels, wsl
from codex_sync.config import AppConfig


THREADS_DDL = """
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  rollout_path TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  source TEXT NOT NULL,
  model_provider TEXT NOT NULL,
  cwd TEXT NOT NULL,
  title TEXT NOT NULL,
  sandbox_policy TEXT NOT NULL,
  approval_mode TEXT NOT NULL,
  preview TEXT,
  first_user_message TEXT,
  archived INTEGER DEFAULT 0
);
"""


def _insert_thread(con, tid, prov, title, cwd, ua, rollout_path):
    con.execute(
        "INSERT INTO threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,preview,first_user_message,archived)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (tid, rollout_path, 100, ua, "vscode", prov, cwd, title, "{}", "on-request", title, title),
    )


def _build_backup_zip(zip_path, rows):
    """rows: list of (id, provider, title, cwd, updated_at)。构造含 state_5.sqlite + rollout 的备份 zip。"""
    with tempfile.TemporaryDirectory() as tmpd:
        sdb = Path(tmpd) / "state_5.sqlite"
        con = sqlite3.connect(sdb)
        con.executescript(THREADS_DDL)
        rollouts = {}
        for tid, prov, title, cwd, ua in rows:
            fname = f"rollout-2025-10-09T00-00-00-{tid}.jsonl"
            zentry = f"codex/sessions/2025/10/09/{fname}"
            src_rollout = f"C:\\Users\\src\\.codex\\sessions\\2025\\10\\09\\{fname}"  # 源机绝对路径
            _insert_thread(con, tid, prov, title, cwd, ua, src_rollout)
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": cwd, "model_provider": prov}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "问题 " + title}]}}, ensure_ascii=False),
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "回答 " + title}]}}, ensure_ascii=False),
            ]
            rollouts[zentry] = ("\n".join(lines) + "\n").encode("utf-8")
        con.commit()
        con.close()
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"id": "testbk", "device_id": "other-dev"}))
            zf.write(sdb, "codex/state_5.sqlite")
            for entry, data in rollouts.items():
                zf.writestr(entry, data)


class BackupImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._old_home = os.environ.get("CODEX_SYNC_HOME")
        os.environ["CODEX_SYNC_HOME"] = str(base / "app")

        self.home = base / "codex_home"  # 本机 ~/.codex
        self.home.mkdir(parents=True, exist_ok=True)
        self.local_db = base / "state_5.sqlite"
        con = sqlite3.connect(self.local_db)
        con.executescript(THREADS_DDL)
        _insert_thread(con, "dup", "openai", "重复对话", "/p2", 300, str(self.home / "sessions" / "dup.jsonl"))
        con.commit()
        con.close()

        self.zip1 = base / "bk1.zip"  # newconv(本机无) + dup(同本机 ua=300)
        _build_backup_zip(self.zip1, [("newconv", "anyrouter", "新对话", "/p1", 500), ("dup", "openai", "重复对话", "/p2", 300)])
        self.zip2 = base / "bk2.zip"  # dup 更新版(ua=400 > 本机300)
        _build_backup_zip(self.zip2, [("dup", "openai", "重复对话(更新)", "/p2", 400)])

        self.patchers = [
            mock.patch.object(backup_import, "codex_state_db", return_value=self.local_db),
            mock.patch.object(backup_import, "codex_home", return_value=self.home),
            mock.patch.object(backup_import, "current_codex_provider", return_value="custom"),
            mock.patch.object(backup_import, "create_disaster_backup", return_value={"created": False}),
            mock.patch.object(backup_import, "_backup_state_db", return_value={"dir": "x", "files": []}),
            mock.patch.object(codex_channels, "codex_running", return_value=False),
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

    def _provider(self, tid):
        con = sqlite3.connect(self.local_db)
        try:
            row = con.execute("SELECT model_provider, rollout_path FROM threads WHERE id=?", (tid,)).fetchone()
            return row
        finally:
            con.close()

    def test_list_marks_present_and_body(self) -> None:
        res = backup_import.list_backup_conversations(self.zip1)
        self.assertTrue(res["success"])
        by = {c["id"]: c for c in res["conversations"]}
        self.assertFalse(by["newconv"]["present"])
        self.assertTrue(by["newconv"]["has_body"])
        self.assertTrue(by["dup"]["present"])
        self.assertFalse(by["dup"]["newer"])  # 同 updated_at

    def test_import_new_conversation(self) -> None:
        res = backup_import.import_conversations(self.cfg, self.zip1, ["newconv"], target_provider="custom")
        self.assertTrue(res["success"])
        self.assertEqual(res["imported"], 1)
        prov, rollout_path = self._provider("newconv")
        self.assertEqual(prov, "custom")  # 接入目标渠道
        rp = Path(rollout_path)
        self.assertTrue(rp.exists())  # rollout 落到本机
        first = json.loads(rp.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["payload"]["model_provider"], "custom")  # rollout 也改成目标渠道
        self.assertEqual(first["payload"]["id"], "newconv")

    def test_import_skips_present_same(self) -> None:
        res = backup_import.import_conversations(self.cfg, self.zip1, ["dup"], target_provider="custom")
        self.assertEqual(res["imported"], 0)
        self.assertTrue(any(s["reason"] == "already_present" for s in res["skipped"]))

    def test_import_newer_makes_copy(self) -> None:
        res = backup_import.import_conversations(self.cfg, self.zip2, ["dup"], target_provider="custom")
        self.assertEqual(res["imported"], 1)
        item = res["items"][0]
        self.assertTrue(item["copy"])
        self.assertNotEqual(item["new_id"], "dup")
        con = sqlite3.connect(self.local_db)
        try:
            ids = {r[0] for r in con.execute("SELECT id FROM threads")}
        finally:
            con.close()
        self.assertIn("dup", ids)  # 本机原版保留
        self.assertIn(item["new_id"], ids)  # 新副本也在

    def test_import_requires_target_when_unknown(self) -> None:
        with mock.patch.object(backup_import, "current_codex_provider", return_value=None):
            res = backup_import.import_conversations(self.cfg, self.zip1, ["newconv"], target_provider=None)
        self.assertFalse(res["success"])

    def test_list_importable_backups_includes_wsl_archives(self) -> None:
        wsl_dir = Path(os.environ["CODEX_SYNC_HOME"]) / "wsl" / "Ubuntu-24.04" / "full-backups"
        wsl_dir.mkdir(parents=True, exist_ok=True)
        archive = wsl_dir / "wsl-test.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "id": "wsl-test",
                        "device_id": "wsl:Ubuntu-24.04",
                        "created_at": "2026-06-07T00:00:00+00:00",
                        "source": "wsl",
                    }
                ),
            )
        with mock.patch.object(backup_import, "list_full_backups", return_value={"success": True, "full_backups": []}):
            res = backup_import.list_importable_backups(self.cfg)
        self.assertTrue(res["success"])
        item = next(b for b in res["backups"] if b["backup_id"] == "wsl-test")
        self.assertEqual(item["source"], "wsl")
        self.assertEqual(item["device_id"], "wsl:Ubuntu-24.04")
        self.assertEqual(item["archive"], str(archive))

        with mock.patch.object(backup_import, "download_full_backup") as download:
            found, err = backup_import._ensure_local_archive(self.cfg, "wsl-test")
        self.assertIsNone(err)
        self.assertEqual(found, archive)
        download.assert_not_called()

    def test_import_conversations_to_wsl_writes_state_and_rollout(self) -> None:
        base = Path(self.tmp.name)
        target_db = base / "wsl-target.sqlite"
        con = sqlite3.connect(target_db)
        con.executescript(THREADS_DDL)
        con.commit()
        con.close()
        writes: list[tuple[str, bytes | None]] = []

        def fake_wsl_sh(_distro, script, input_bytes=None, timeout=120):
            writes.append((script, input_bytes))
            return 0, b"", ""

        with (
            mock.patch.object(wsl, "_ensure_wsl_codex_closed", return_value=None),
            mock.patch.object(wsl, "_wsl_state_db_rel", return_value="state_5.sqlite"),
            mock.patch.object(wsl, "_read_wsl_file", return_value=(target_db.read_bytes(), None)),
            mock.patch.object(wsl, "create_wsl_full_backup", return_value={"success": True, "backup_id": "preflight"}),
            mock.patch.object(wsl, "_wsl_codex_home", return_value="/home/user/.codex"),
            mock.patch.object(wsl, "_wsl_sh", side_effect=fake_wsl_sh),
        ):
            res = wsl.import_conversations_to_wsl("Ubuntu-24.04", self.zip1, ["newconv"], target_provider="custom")

        self.assertTrue(res["success"])
        self.assertEqual(res["imported"], 1)
        rollout_write = next(item for item in writes if item[1] and b"session_meta" in item[1])
        first = json.loads(rollout_write[1].decode("utf-8").splitlines()[0])
        self.assertEqual(first["payload"]["id"], "newconv")
        self.assertEqual(first["payload"]["model_provider"], "custom")

        state_write = writes[-1][1]
        self.assertIsNotNone(state_write)
        imported_db = base / "wsl-imported.sqlite"
        imported_db.write_bytes(state_write)
        con = sqlite3.connect(imported_db)
        try:
            row = con.execute("SELECT id, model_provider, rollout_path FROM threads WHERE id='newconv'").fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "custom")
        self.assertTrue(row[2].startswith("/home/user/.codex/sessions/"))


if __name__ == "__main__":
    unittest.main()
