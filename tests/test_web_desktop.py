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


if __name__ == "__main__":
    unittest.main()
