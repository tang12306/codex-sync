import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from codex_sync import __version__
from codex_sync import web_desktop
from codex_sync.config import AppConfig
from codex_sync.web_desktop import DesktopRuntime, SingleInstance, create_local_server, make_handler


class WebDesktopTests(unittest.TestCase):
    def test_local_api_requires_session_token(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            runtime = DesktopRuntime()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_port}"
                html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
                match = re.search(r'name="codex-sync-desktop-token" content="([^"]+)"', html)
                self.assertIsNotNone(match)
                token = match.group(1)
                self.assertEqual(token, runtime.session_token)

                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(base + "/api/status", timeout=5)
                self.assertEqual(denied.exception.code, 403)

                request = urllib.request.Request(base + "/api/status", headers={"X-Codex-Sync-Desktop-Token": token})
                payload = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
                self.assertIn("config", payload)
                self.assertIn("desktop_close_behavior", payload["config"])
                self.assertIn("app_install", payload)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_static_assets_are_not_cached_across_versions(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            runtime = DesktopRuntime()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_port}"
                index_response = urllib.request.urlopen(base + "/", timeout=5)
                html = index_response.read().decode("utf-8")
                self.assertIn("no-store", index_response.headers["Cache-Control"])
                self.assertIn(f'/js/main.js?v={__version__}', html)
                self.assertIn(f'Codex Sync · v{__version__}', html)

                js_response = urllib.request.urlopen(base + f"/js/main.js?v={__version__}", timeout=5)
                js = js_response.read().decode("utf-8")
                self.assertIn("no-store", js_response.headers["Cache-Control"])
                self.assertIn(f'from "./store.js?v={__version__}"', js)
                self.assertIn(f'./views/${{route}}.js?v={__version__}', js)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_local_server_can_start_and_stop_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            local = None
            try:
                local = create_local_server(port=None)
                local.start()
                response = urllib.request.urlopen(local.url, timeout=5)
                self.assertEqual(response.status, 200)
                self.assertIn("codex-sync-desktop-token", response.read().decode("utf-8"))
            finally:
                if local is not None:
                    local.stop()
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_restore_directory_picker_uses_restore_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                runtime = DesktopRuntime()
                with patch("codex_sync.web_desktop._choose_project_directory", return_value={"success": True, "path": "C:\\restore-target"}) as choose:
                    result = runtime.run_action("choose-project-dir", {"project_path": "C:\\base", "purpose": "restore"})
                self.assertTrue(result["success"])
                self.assertEqual(result["path"], "C:\\restore-target")
                choose.assert_called_once_with("C:\\base", purpose="restore")
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_sync_health_action_reuses_short_lived_cache(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                runtime = DesktopRuntime()
                with (
                    patch("codex_sync.web_desktop.summarize_sync_health", return_value={"success": True, "local": {}, "devices": [], "pending_devices": []}) as summarize,
                    patch("codex_sync.web_desktop._collect_sync_health_locals", return_value={}),
                    patch("codex_sync.web_desktop._decorate_sync_health", side_effect=[
                        {"success": True, "value": 1},
                        {"success": True, "value": 2},
                    ]) as decorate,
                ):
                    first = runtime.run_action("sync-health")
                    second = runtime.run_action("sync-health")
                    forced = runtime.run_action("sync-health", {"force": True})
                self.assertFalse(first["cached"])
                self.assertTrue(second["cached"])
                self.assertEqual(second["value"], 1)
                self.assertFalse(forced["cached"])
                self.assertEqual(forced["value"], 2)
                self.assertEqual(summarize.call_count, 2)
                self.assertEqual(decorate.call_count, 2)
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_conversation_workspace_reuses_single_scan_for_related_actions(self) -> None:
        with tempfile.TemporaryDirectory() as sync_home:
            old_sync = os.environ.get("CODEX_SYNC_HOME")
            os.environ["CODEX_SYNC_HOME"] = sync_home
            try:
                runtime = DesktopRuntime()
                snapshot_one = {
                    "success": True,
                    "homes_result": {"success": True, "homes": [{"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True}]},
                    "homes": [{"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True}],
                    "usable_homes": [{"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True}],
                    "per_home": {
                        "windows": {
                            "home": {"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True},
                            "conversations": {
                                "success": True,
                                "source_home": "windows",
                                "conversations": [{"id": "t1", "updated_at": 2, "model_provider": "openai", "cwd": "C:\\project"}],
                                "count": 1,
                                "cwds": ["C:\\project"],
                                "providers": ["openai"],
                            },
                            "channels": {
                                "success": True,
                                "source_home": "windows",
                                "home_id": "windows",
                                "home_label": "Windows",
                                "channels": [{"provider": "openai", "threads": 1}],
                            },
                        }
                    },
                    "scan_ms": 12,
                }
                snapshot_two = {
                    **snapshot_one,
                    "per_home": {
                        "windows": {
                            **snapshot_one["per_home"]["windows"],
                            "conversations": {
                                **snapshot_one["per_home"]["windows"]["conversations"],
                                "conversations": [{"id": "t2", "updated_at": 3, "model_provider": "openai", "cwd": "C:\\project"}],
                            },
                        }
                    },
                    "scan_ms": 13,
                }
                with patch("codex_sync.web_desktop._scan_conversation_workspace", side_effect=[snapshot_one, snapshot_two]) as scan:
                    workspace = runtime.run_action("conversation-workspace", {"source_home": "all"})
                    conversations = runtime.run_action("list-conversations", {"source_home": "windows"})
                    channels = runtime.run_action("list-channels", {"source_home": "windows"})
                    forced = runtime.run_action("conversation-workspace", {"source_home": "windows", "force": True})

                self.assertFalse(workspace["cached"])
                self.assertTrue(conversations["success"])
                self.assertEqual(conversations["conversations"][0]["id"], "t1")
                self.assertTrue(channels["success"])
                self.assertEqual(channels["channels"][0]["provider"], "openai")
                self.assertFalse(forced["cached"])
                self.assertEqual(forced["conversations"]["conversations"][0]["id"], "t2")
                self.assertEqual(scan.call_count, 2)
            finally:
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync

    def test_wsl_without_codex_is_neutral_in_sync_health(self) -> None:
        health = {"success": True, "local": {}, "devices": [], "pending_devices": []}
        with (
            patch("codex_sync.web_desktop.windows_task_status", return_value={"installed": True}),
            patch("codex_sync.web_desktop.outbox_count", return_value=0),
            patch("codex_sync.web_desktop.hook_status", return_value={"events": ["Stop"]}),
            patch("codex_sync.web_desktop._project_state", return_value={"exists": True, "is_dir": True, "is_repo": True, "dirty": False, "root": "C:\\project"}),
            patch("codex_sync.web_desktop.project_auto_backup_status", return_value={"queue_count": 0, "enabled_for_git_commit": True}),
            patch(
                "codex_sync.web_desktop._overview_codex_homes",
                return_value={
                    "success": True,
                    "homes": [
                        {"id": "windows", "kind": "windows", "label": "Windows", "ok": True, "has_codex": True},
                        {"id": "wsl:Ubuntu", "kind": "wsl", "distro": "Ubuntu", "label": "WSL · Ubuntu", "ok": True, "has_codex": False},
                    ],
                },
            ),
        ):
            result = web_desktop._decorate_sync_health(AppConfig(server_url="http://server", device_id="device"), health)
        wsl_item = next(item for item in result["status_items"] if item["id"] == "wsl-Ubuntu")
        self.assertEqual(wsl_item["status"], "未参与")
        self.assertEqual(wsl_item["tone"], "")
        self.assertNotIn("Codex", result["summary"]["text"])

    @unittest.skipUnless(os.name == "nt", "Windows mutex behavior")
    def test_single_instance_rejects_second_owner(self) -> None:
        name = f"Local\\CodexSyncTest-{uuid.uuid4()}"
        first = SingleInstance(name)
        second = SingleInstance(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            second.release()
            first.release()

    def test_warmup_loop_refreshes_caches_then_stops_cleanly(self) -> None:
        runtime = DesktopRuntime()
        refreshed = threading.Event()
        with (
            patch.object(runtime, "cached_sync_health") as ch,
            patch.object(runtime, "cached_conversation_workspace", side_effect=lambda **kw: refreshed.set()),
            patch("codex_sync.web_desktop.load_config", return_value=AppConfig(device_id="warmup")),
            patch("codex_sync.web_desktop.WARMUP_INITIAL_DELAY_SECONDS", 0),
            patch("codex_sync.web_desktop.WARMUP_INTERVAL_SECONDS", 30),
        ):
            runtime.start_warmup()
            self.assertTrue(refreshed.wait(timeout=5))  # 预热线程主动刷新了缓存
            runtime.stop_warmup()
        self.assertTrue(ch.called)
        self.assertIsNotNone(runtime.warmup_thread)
        self.assertFalse(runtime.warmup_thread.is_alive())  # stop_warmup 后线程干净退出


if __name__ == "__main__":
    unittest.main()
