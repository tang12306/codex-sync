import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from codex_sync.web_desktop import DesktopRuntime, make_handler


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
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                if old_sync is None:
                    os.environ.pop("CODEX_SYNC_HOME", None)
                else:
                    os.environ["CODEX_SYNC_HOME"] = old_sync


if __name__ == "__main__":
    unittest.main()
