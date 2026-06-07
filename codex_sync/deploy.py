"""服务器端一键部署 / 全新安装（开源普适，纯标准库）。

把本地 sync_server.py 通过 SSH 部署到远程服务器：更新已有部署，或在干净服务器上
全新安装（systemd 服务 + token + 可选 nginx 反代）。所有连接信息由用户在
~/.codex-sync/deploy.json 配置，代码零硬编码主机信息（开源友好）。

高危远程写操作：update/install 需显式确认（CLI --yes / 网页确认框），支持 --dry-run 预览。
依赖系统 OpenSSH（ssh/scp）+ 已配置的免密 key（BatchMode，失败即报错不挂起）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import load_config, save_config
from .paths import app_dir, ensure_app_dirs
from .util import _CREATE_NO_WINDOW, decode_process_output


@dataclass
class DeployConfig:
    # 连接信息必填、零默认（开源不含任何个人/主机信息）
    ssh_target: str = ""           # user@host 或 SSH config 别名
    ssh_port: int = 22
    # 以下为通用 FHS 路径 / 标准端口，对任意部署都合理
    remote_dir: str = "/opt/codex-sync-server"
    service_name: str = "codex-sync-server"
    python: str = "python3"
    bind_host: str = "0.0.0.0"
    bind_port: int = 8888
    data_dir: str = "/var/lib/codex-sync"
    token_file: str = "/etc/codex-sync/server-token"
    max_body_bytes: int = 536870912  # 512 MiB
    nginx_enabled: bool = False
    nginx_server_name: str = ""      # 启用 nginx 时必填（域名或公网 IP）
    client_max_body_size: str = "512m"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeployConfig":
        base = cls()
        return cls(**{key: data.get(key, getattr(base, key)) for key in asdict(base).keys()})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def deploy_config_path() -> Path:
    ensure_app_dirs()
    return app_dir() / "deploy.json"


def load_deploy_config() -> DeployConfig:
    path = deploy_config_path()
    if not path.exists():
        return DeployConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DeployConfig()
    return DeployConfig.from_dict(data if isinstance(data, dict) else {})


def save_deploy_config(cfg: DeployConfig) -> Path:
    path = deploy_config_path()
    path.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def local_sync_server_path() -> Path:
    """Packaged server entry used for one-click deployment."""
    candidates = [Path(__file__).resolve().parent / "sync_server.py"]
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "codex_sync" / "sync_server.py")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


# ---------- SSH / scp 封装 ----------

def _run(args: list[str], timeout: int, input_text: str | None = None, ssh_password: str | None = None) -> tuple[int, str, str]:
    tmpdir: tempfile.TemporaryDirectory[str] | None = None
    env = None
    if ssh_password:
        tmpdir = tempfile.TemporaryDirectory()
        if os.name == "nt":
            helper = Path(tmpdir.name) / "askpass.cmd"
            helper.write_text("@echo off\r\n<nul set /p=%CODEX_SYNC_SSH_PASSWORD%\r\n", encoding="utf-8")
        else:
            helper = Path(tmpdir.name) / "askpass.sh"
            helper.write_text("#!/bin/sh\nprintf '%s' \"$CODEX_SYNC_SSH_PASSWORD\"\n", encoding="utf-8")
            helper.chmod(0o700)
        env = os.environ.copy()
        env["SSH_ASKPASS"] = str(helper)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["CODEX_SYNC_SSH_PASSWORD"] = ssh_password
        env.setdefault("DISPLAY", "codex-sync")
    try:
        proc = subprocess.run(
            args,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
        )
        return proc.returncode, decode_process_output(proc.stdout).strip(), decode_process_output(proc.stderr).strip()
    except FileNotFoundError as exc:
        return 127, "", f"命令未找到（请确认已安装 OpenSSH）：{exc}"
    except subprocess.TimeoutExpired as exc:
        return 124, decode_process_output(exc.stdout).strip(), decode_process_output(exc.stderr).strip() or f"操作超时（{timeout}s）"
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


def _ssh_options(cfg: DeployConfig, ssh_password: str | None = None) -> list[str]:
    options = ["-p", str(int(cfg.ssh_port or 22)), "-o", "ConnectTimeout=15"]
    if ssh_password:
        options += ["-o", "StrictHostKeyChecking=accept-new", "-o", "NumberOfPasswordPrompts=1"]
    else:
        options += ["-o", "BatchMode=yes"]
    return options


def _ssh(cfg: DeployConfig, remote_cmd: str, timeout: int = 60, input_text: str | None = None, ssh_password: str | None = None) -> tuple[int, str, str]:
    args = ["ssh", *_ssh_options(cfg, ssh_password), cfg.ssh_target, remote_cmd]
    return _run(args, timeout, input_text=input_text, ssh_password=ssh_password)


def _scp(cfg: DeployConfig, local: str | Path, remote: str, timeout: int = 120, ssh_password: str | None = None) -> tuple[int, str, str]:
    scp_options = ["-P", str(int(cfg.ssh_port or 22)), "-o", "ConnectTimeout=15"]
    if ssh_password:
        scp_options += ["-o", "StrictHostKeyChecking=accept-new", "-o", "NumberOfPasswordPrompts=1"]
    else:
        scp_options += ["-o", "BatchMode=yes"]
    args = ["scp", *scp_options, str(local), f"{cfg.ssh_target}:{remote}"]
    return _run(args, timeout, ssh_password=ssh_password)


def require_target(cfg: DeployConfig) -> dict[str, Any] | None:
    if not (cfg.ssh_target or "").strip():
        return {"success": False, "error": "未配置 ssh_target。请先运行：python -m codex_sync deploy-config --ssh-target user@host"}
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_file(cfg: DeployConfig) -> str:
    return f"{cfg.remote_dir.rstrip('/')}/sync_server.py"


def _remote_probe_url(cfg: DeployConfig) -> str:
    probe_host = "127.0.0.1" if cfg.bind_host in ("0.0.0.0", "::") else cfg.bind_host
    return f"http://{probe_host}:{cfg.bind_port}/api/devices"


# ---------- 配置文件模板（占位全部来自 cfg，无硬编码主机）----------

def render_systemd_unit(cfg: DeployConfig) -> str:
    token_dir = str(Path(cfg.token_file).parent).replace("\\", "/")
    db_path = f"{cfg.data_dir.rstrip('/')}/snapshots.sqlite3"
    return (
        "[Unit]\n"
        "Description=Codex Sync Snapshot Server\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={cfg.remote_dir}\n"
        f"Environment=CODEX_SYNC_SERVER_HOST={cfg.bind_host}\n"
        f"Environment=CODEX_SYNC_SERVER_PORT={cfg.bind_port}\n"
        f"Environment=CODEX_SYNC_DATA_DIR={cfg.data_dir}\n"
        f"Environment=CODEX_SYNC_DB_PATH={db_path}\n"
        f"Environment=CODEX_SYNC_FULL_BACKUP_MAX_BODY_BYTES={cfg.max_body_bytes}\n"
        f"Environment=CODEX_SYNC_SERVER_TOKEN_FILE={cfg.token_file}\n"
        f"ExecStart={cfg.python} {_remote_file(cfg)}\n"
        "Restart=always\n"
        "RestartSec=3\n"
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "ProtectSystem=full\n"
        f"ReadWritePaths={cfg.data_dir} {token_dir}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_nginx_conf(cfg: DeployConfig) -> str:
    return (
        "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        f"    server_name {cfg.nginx_server_name};\n"
        "\n"
        f"    client_max_body_size {cfg.client_max_body_size};\n"
        "\n"
        "    location /api/ {\n"
        f"        proxy_pass http://{cfg.bind_host}:{cfg.bind_port}/api/;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_set_header Authorization $http_authorization;\n"
        "        proxy_connect_timeout 10s;\n"
        "        proxy_send_timeout 60s;\n"
        "        proxy_read_timeout 60s;\n"
        "    }\n"
        "}\n"
    )


# ---------- 状态（只读）----------

def deploy_status(cfg: DeployConfig, ssh_password: str | None = None) -> dict[str, Any]:
    guard = require_target(cfg)
    if guard:
        return guard
    remote_file = _remote_file(cfg)
    probe = _remote_probe_url(cfg)
    cmd = (
        f"systemctl is-active {shlex.quote(cfg.service_name)}.service 2>/dev/null; echo '<SEP>'; "
        f"sha256sum {shlex.quote(remote_file)} 2>/dev/null | cut -d' ' -f1; echo '<SEP>'; "
        f"curl -s -o /dev/null -w '%{{http_code}}' {shlex.quote(probe)} 2>/dev/null"
    )
    code, out, err = _ssh(cfg, cmd, ssh_password=ssh_password)
    if code != 0 and not out:
        return {"success": False, "error": f"SSH 失败：{err or code}", "ssh_target": cfg.ssh_target}
    parts = [p.strip() for p in out.split("<SEP>")]
    active = parts[0] if len(parts) > 0 else ""
    remote_sha = parts[1] if len(parts) > 1 else ""
    http_code = parts[2] if len(parts) > 2 else ""
    local = local_sync_server_path()
    local_sha = _sha256_file(local) if local.exists() else ""
    return {
        "success": True,
        "ssh_target": cfg.ssh_target,
        "service": f"{cfg.service_name}.service",
        "active": active,
        "remote_sha256": remote_sha,
        "local_sha256": local_sha,
        "in_sync": bool(remote_sha and remote_sha == local_sha),
        "endpoint": probe,
        "endpoint_http": http_code,
        "endpoint_ok": http_code in ("200", "401"),
    }


# ---------- 更新已有部署 ----------

def update_server(
    cfg: DeployConfig,
    dry_run: bool = False,
    assume_yes: bool = False,
    ssh_password: str | None = None,
) -> dict[str, Any]:
    guard = require_target(cfg)
    if guard:
        return guard
    local = local_sync_server_path()
    if not local.exists():
        return {"success": False, "error": f"本地未找到 sync_server.py：{local}"}
    if not dry_run and not assume_yes:
        return {"success": False, "error": "更新会重启远程服务。请加 --yes 确认，或用 --dry-run 预览要执行的命令。"}

    remote_file = _remote_file(cfg)
    qf = shlex.quote(remote_file)
    svc = shlex.quote(cfg.service_name)
    py = shlex.quote(cfg.python)
    probe = _remote_probe_url(cfg)
    backup_cmd = f"cp -a {qf} {qf}.bak-$(date +%Y%m%d-%H%M%S)"
    compile_cmd = f"{py} -m py_compile {qf}"
    restart_cmd = f"systemctl restart {svc}.service"
    probe_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' {shlex.quote(probe)}"

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "commands": [
                f"ssh {cfg.ssh_target} {backup_cmd!r}",
                f"scp {local} {cfg.ssh_target}:{remote_file}",
                f"ssh {cfg.ssh_target} {compile_cmd!r}",
                f"ssh {cfg.ssh_target} {restart_cmd!r}",
                f"ssh {cfg.ssh_target} {probe_cmd!r}",
            ],
        }

    steps: list[dict[str, Any]] = []
    code, out, err = _ssh(cfg, backup_cmd, ssh_password=ssh_password)
    steps.append({"step": "backup", "code": code, "err": err})
    if code != 0:
        return {"success": False, "error": f"备份远程文件失败：{err or code}", "steps": steps}

    code, out, err = _scp(cfg, local, remote_file, ssh_password=ssh_password)
    steps.append({"step": "upload", "code": code, "err": err})
    if code != 0:
        return {"success": False, "error": f"上传 sync_server.py 失败：{err or code}", "steps": steps}

    code, out, err = _ssh(cfg, compile_cmd, ssh_password=ssh_password)
    steps.append({"step": "py_compile", "code": code, "err": err})
    if code != 0:
        return {"success": False, "error": f"远程语法校验失败（未重启，可 deploy-server --rollback --yes 还原）：{err or out}", "steps": steps}

    code, out, err = _ssh(cfg, restart_cmd, ssh_password=ssh_password)
    steps.append({"step": "restart", "code": code, "err": err})
    if code != 0:
        return {"success": False, "error": f"重启服务失败：{err or code}", "steps": steps}

    code, out, err = _ssh(cfg, probe_cmd, ssh_password=ssh_password)
    http_code = out.strip()
    steps.append({"step": "probe", "code": code, "http": http_code})
    return {
        "success": True,
        "updated": True,
        "endpoint_http": http_code,
        "endpoint_ok": http_code in ("200", "401"),
        "steps": steps,
    }


# ---------- 全新安装 ----------

def install_server(
    cfg: DeployConfig,
    dry_run: bool = False,
    assume_yes: bool = False,
    regenerate_token: bool = False,
    backfill_local: bool = True,
    ssh_password: str | None = None,
) -> dict[str, Any]:
    guard = require_target(cfg)
    if guard:
        return guard
    local = local_sync_server_path()
    if not local.exists():
        return {"success": False, "error": f"本地未找到 sync_server.py：{local}"}
    if cfg.nginx_enabled and not cfg.nginx_server_name.strip():
        return {"success": False, "error": "启用 nginx 时必须配置 nginx_server_name（域名或公网 IP）。"}
    if not dry_run and not assume_yes:
        return {"success": False, "error": "全新安装会在服务器写入 systemd 服务、token、（可选）nginx 配置。请加 --yes 确认，或用 --dry-run 预览。"}

    remote_file = _remote_file(cfg)
    token_dir = str(Path(cfg.token_file).parent).replace("\\", "/")
    svc_path = f"/etc/systemd/system/{cfg.service_name}.service"
    nginx_path = "/etc/nginx/conf.d/codex-sync.conf"
    unit_text = render_systemd_unit(cfg)
    nginx_text = render_nginx_conf(cfg)
    py = shlex.quote(cfg.python)
    qtoken = shlex.quote(cfg.token_file)
    svc = shlex.quote(cfg.service_name)

    mkdir_cmd = (
        f"mkdir -p {shlex.quote(cfg.remote_dir)} {shlex.quote(cfg.data_dir)} {shlex.quote(token_dir)} "
        f"&& chmod 700 {shlex.quote(token_dir)}"
    )
    if regenerate_token:
        token_cmd = f"{py} -c 'import secrets;print(secrets.token_urlsafe(16))' > {qtoken} && chmod 600 {qtoken}"
    else:
        token_cmd = f"test -s {qtoken} || {py} -c 'import secrets;print(secrets.token_urlsafe(16))' > {qtoken}; chmod 600 {qtoken}"
    enable_cmd = f"systemctl daemon-reload && systemctl enable {svc}.service && systemctl restart {svc}.service"
    probe = _remote_probe_url(cfg)
    probe_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' {shlex.quote(probe)}"

    if dry_run:
        commands = [
            f"ssh {cfg.ssh_target} {mkdir_cmd!r}",
            f"scp {local} {cfg.ssh_target}:{remote_file}",
            f"ssh {cfg.ssh_target} '<生成 token 到 {cfg.token_file}（不存在时）>'",
            f"ssh {cfg.ssh_target} 'cat > {svc_path}'   # systemd unit",
            f"ssh {cfg.ssh_target} {enable_cmd!r}",
        ]
        if cfg.nginx_enabled:
            commands += [
                f"ssh {cfg.ssh_target} 'cat > {nginx_path}'   # nginx conf",
                f"ssh {cfg.ssh_target} 'nginx -t && systemctl reload nginx'",
            ]
        commands.append(f"ssh {cfg.ssh_target} {probe_cmd!r}")
        return {
            "success": True,
            "dry_run": True,
            "commands": commands,
            "unit_preview": unit_text,
            "nginx_preview": nginx_text if cfg.nginx_enabled else None,
        }

    steps: list[dict[str, Any]] = []

    def record(step: str, code: int, err: str) -> bool:
        steps.append({"step": step, "code": code, "err": err})
        return code == 0

    code, out, err = _ssh(cfg, mkdir_cmd, ssh_password=ssh_password)
    if not record("mkdir", code, err):
        return {"success": False, "error": f"创建远程目录失败：{err or code}", "steps": steps}

    code, out, err = _scp(cfg, local, remote_file, ssh_password=ssh_password)
    if not record("upload", code, err):
        return {"success": False, "error": f"上传 sync_server.py 失败：{err or code}", "steps": steps}

    code, out, err = _ssh(cfg, token_cmd, ssh_password=ssh_password)
    if not record("token", code, err):
        return {"success": False, "error": f"生成/设置 token 失败：{err or code}", "steps": steps}

    code, out, err = _ssh(cfg, f"cat > {shlex.quote(svc_path)}", input_text=unit_text, ssh_password=ssh_password)
    if not record("write_unit", code, err):
        return {"success": False, "error": f"写入 systemd unit 失败：{err or code}", "steps": steps}

    code, out, err = _ssh(cfg, enable_cmd, ssh_password=ssh_password)
    if not record("enable_start", code, err):
        return {"success": False, "error": f"启用/启动服务失败：{err or code}", "steps": steps}

    if cfg.nginx_enabled:
        code, out, err = _ssh(cfg, f"cat > {shlex.quote(nginx_path)}", input_text=nginx_text, ssh_password=ssh_password)
        if not record("write_nginx", code, err):
            return {"success": False, "error": f"写入 nginx 配置失败：{err or code}", "steps": steps}
        code, out, err = _ssh(cfg, "nginx -t && systemctl reload nginx", ssh_password=ssh_password)
        if not record("reload_nginx", code, err):
            return {"success": False, "error": f"nginx 测试/重载失败：{err or out}", "steps": steps}

    _, token_val, _ = _ssh(cfg, f"cat {qtoken}", ssh_password=ssh_password)
    token_val = token_val.strip()

    code, out, err = _ssh(cfg, probe_cmd, ssh_password=ssh_password)
    http_code = out.strip()
    steps.append({"step": "probe", "code": code, "http": http_code})

    backfilled = None
    if backfill_local and token_val:
        if cfg.nginx_enabled and cfg.nginx_server_name.strip():
            host = cfg.nginx_server_name.strip()
            server_url = f"http://{host}"
        else:
            host = cfg.ssh_target.split("@")[-1]
            server_url = f"http://{host}:{int(cfg.bind_port or 8888)}"
        app_cfg = load_config()
        app_cfg.server_url = server_url
        app_cfg.api_token = token_val
        save_config(app_cfg)
        backfilled = {"server_url": server_url}

    return {
        "success": True,
        "installed": True,
        "endpoint_http": http_code,
        "endpoint_ok": http_code in ("200", "401"),
        "nginx": cfg.nginx_enabled,
        "local_config_backfilled": backfilled,
        "steps": steps,
    }


# ---------- 回滚 ----------

def rollback_server(cfg: DeployConfig, assume_yes: bool = False, ssh_password: str | None = None) -> dict[str, Any]:
    guard = require_target(cfg)
    if guard:
        return guard
    if not assume_yes:
        return {"success": False, "error": "回滚会用最近备份覆盖远程并重启。请加 --yes 确认。"}
    remote_file = _remote_file(cfg)
    qf = shlex.quote(remote_file)
    svc = shlex.quote(cfg.service_name)
    _, out, _ = _ssh(cfg, f"ls -1t {qf}.bak-* 2>/dev/null | head -1", ssh_password=ssh_password)
    latest = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not latest:
        return {"success": False, "error": "未找到任何 .bak- 备份可回滚。"}
    code, out, err = _ssh(cfg, f"cp -a {shlex.quote(latest)} {qf} && systemctl restart {svc}.service", ssh_password=ssh_password)
    if code != 0:
        return {"success": False, "error": f"回滚失败：{err or code}", "restored_from": latest}
    return {"success": True, "rolled_back": True, "restored_from": latest}
