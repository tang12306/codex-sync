import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_sync import deploy
from codex_sync.deploy import DeployConfig


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CODEX_SYNC_HOME")
        os.environ["CODEX_SYNC_HOME"] = str(Path(self.tmp.name) / "app")  # 隔离 deploy.json / config.json

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("CODEX_SYNC_HOME", None)
        else:
            os.environ["CODEX_SYNC_HOME"] = self._old
        self.tmp.cleanup()

    def test_defaults_are_generic(self) -> None:
        c = DeployConfig()
        self.assertEqual(c.ssh_target, "")  # 必填、零个人信息
        self.assertEqual(c.ssh_port, 22)
        self.assertEqual(c.remote_dir, "/opt/codex-sync-server")
        self.assertEqual(c.bind_host, "0.0.0.0")
        self.assertEqual(c.bind_port, 8888)
        self.assertEqual(c.token_file, "/etc/codex-sync/server-token")
        self.assertFalse(c.nginx_enabled)

    def test_local_server_path_is_packaged(self) -> None:
        path = deploy.local_sync_server_path()
        self.assertEqual(path.name, "sync_server.py")
        self.assertEqual(path.parent.name, "codex_sync")
        self.assertTrue(path.exists())

    def test_config_roundtrip(self) -> None:
        c = DeployConfig(ssh_target="user@host", ssh_port=2222, bind_port=9999, nginx_enabled=True, nginx_server_name="example.com")
        path = deploy.save_deploy_config(c)
        self.assertTrue(Path(path).exists())
        loaded = deploy.load_deploy_config()
        self.assertEqual(loaded.ssh_target, "user@host")
        self.assertEqual(loaded.ssh_port, 2222)
        self.assertEqual(loaded.bind_port, 9999)
        self.assertTrue(loaded.nginx_enabled)
        self.assertEqual(loaded.nginx_server_name, "example.com")

    def test_render_systemd_unit(self) -> None:
        c = DeployConfig(remote_dir="/srv/cs", python="/usr/bin/python3", data_dir="/data/cs", token_file="/etc/cs/tok", bind_port=7777)
        unit = deploy.render_systemd_unit(c)
        self.assertIn("ExecStart=/usr/bin/python3 /srv/cs/sync_server.py", unit)
        self.assertIn("Environment=CODEX_SYNC_SERVER_TOKEN_FILE=/etc/cs/tok", unit)
        self.assertIn("Environment=CODEX_SYNC_SERVER_PORT=7777", unit)
        self.assertIn("ReadWritePaths=/data/cs /etc/cs", unit)
        self.assertNotIn("118.190", unit)  # 无硬编码主机

    def test_render_nginx_conf(self) -> None:
        c = DeployConfig(bind_host="127.0.0.1", bind_port=8888, nginx_server_name="sync.example.com", client_max_body_size="256m")
        conf = deploy.render_nginx_conf(c)
        self.assertIn("server_name sync.example.com;", conf)
        self.assertIn("proxy_pass http://127.0.0.1:8888/api/;", conf)
        self.assertIn("client_max_body_size 256m;", conf)

    def test_require_target_blocks_all_ops(self) -> None:
        c = DeployConfig(ssh_target="")
        for res in (deploy.deploy_status(c), deploy.update_server(c), deploy.install_server(c), deploy.rollback_server(c)):
            self.assertFalse(res["success"])
            self.assertIn("ssh_target", res["error"])

    def test_update_requires_yes(self) -> None:
        c = DeployConfig(ssh_target="user@host")
        with mock.patch("codex_sync.deploy.subprocess.run") as run:
            res = deploy.update_server(c, dry_run=False, assume_yes=False)
        self.assertFalse(res["success"])
        self.assertIn("--yes", res["error"])
        run.assert_not_called()  # 未确认绝不触发任何远程操作

    def test_update_dry_run_no_exec(self) -> None:
        c = DeployConfig(ssh_target="user@host")
        with mock.patch("codex_sync.deploy.subprocess.run") as run:
            res = deploy.update_server(c, dry_run=True)
        self.assertTrue(res["success"])
        self.assertTrue(res["dry_run"])
        self.assertTrue(any("scp" in cmd for cmd in res["commands"]))
        self.assertTrue(any("http://127.0.0.1:8888/api/devices" in cmd for cmd in res["commands"]))
        self.assertFalse(any("http://0.0.0.0:8888/api/devices" in cmd for cmd in res["commands"]))
        run.assert_not_called()

    def test_install_requires_nginx_server_name(self) -> None:
        c = DeployConfig(ssh_target="user@host", nginx_enabled=True, nginx_server_name="")
        res = deploy.install_server(c, assume_yes=True)
        self.assertFalse(res["success"])
        self.assertIn("nginx_server_name", res["error"])

    def test_ssh_command_construction(self) -> None:
        c = DeployConfig(ssh_target="user@host")
        fake = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
        with mock.patch("codex_sync.deploy.subprocess.run", return_value=fake) as run:
            code, out, _ = deploy._ssh(c, "echo hi")
        self.assertEqual((code, out), (0, "ok"))
        args = run.call_args.args[0]
        self.assertEqual(args[0], "ssh")
        self.assertIn("-p", args)
        self.assertIn("22", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("user@host", args)
        self.assertIn("echo hi", args)

    def test_scp_command_construction(self) -> None:
        c = DeployConfig(ssh_target="user@host")
        fake = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch("codex_sync.deploy.subprocess.run", return_value=fake) as run:
            deploy._scp(c, "/local/x.py", "/remote/x.py")
        args = run.call_args.args[0]
        self.assertEqual(args[0], "scp")
        self.assertIn("-P", args)
        self.assertIn("22", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("user@host:/remote/x.py", args)

    def test_ssh_password_uses_askpass_without_command_leak(self) -> None:
        c = DeployConfig(ssh_target="user@host", ssh_port=2222)
        fake = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
        with mock.patch("codex_sync.deploy.subprocess.run", return_value=fake) as run:
            code, out, _ = deploy._ssh(c, "echo hi", ssh_password="secret-pass")
        self.assertEqual((code, out), (0, "ok"))
        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(args[0], "ssh")
        self.assertIn("2222", args)
        self.assertNotIn("BatchMode=yes", args)
        self.assertNotIn("secret-pass", " ".join(args))
        self.assertEqual(kwargs["env"]["CODEX_SYNC_SSH_PASSWORD"], "secret-pass")
        self.assertIn("SSH_ASKPASS", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
