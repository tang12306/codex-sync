from __future__ import annotations

import argparse
import json
import os
import sys

from .collector import capture_event, create_resume_prompt
from .config import AppConfig, load_config, save_config
from .daemon import run_daemon
from .disaster_backup import create_disaster_backup
from .git_backup import backup_project_to_server, create_patch_snapshot, list_project_backups
from .hooks import hook_status, install_hooks
from .full_backup import (
    download_full_backup,
    full_backup_now,
    get_remote_device_state,
    list_full_backups,
    notify_codex_changed,
    restore_full_backup,
    scan_full_backup_changes,
)
from .server import (
    flush_outbox,
    outbox_count,
    get_server_retention,
    sync_once,
    list_remote_snapshots,
    get_remote_snapshot_detail,
    generate_remote_resume_context,
    prune_server_retention,
    preview_restore_snapshot,
    restore_snapshot_locally,
    check_server_compatibility,
)
from .windows_task import install_windows_task, uninstall_windows_task, windows_task_status
from .wsl import pull_wsl_config, restore_latest_wsl_full_backup, restore_wsl_full_backup, wsl_full_backup_now, wsl_status
from .backup_import import _ensure_local_archive, import_conversations, list_backup_conversations, list_importable_backups
from .codex_channels import list_channels, merge_channels, merge_threads, restore_channels, restore_threads
from .conversations import export_conversation, list_conversations, read_conversation
from .deploy import (
    deploy_config_path,
    deploy_status,
    install_server,
    load_deploy_config,
    rollback_server,
    save_deploy_config,
    update_server,
)


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def cmd_config(args: argparse.Namespace) -> None:
    cfg = load_config()
    changed = False
    for field in (
        "server_url",
        "api_token",
        "device_id",
        "sync_interval_seconds",
        "max_untracked_copy_bytes",
        "full_backup_enabled",
        "full_backup_include_config",
        "full_backup_include_memories",
        "full_backup_allow_plaintext_upload",
        "full_backup_quiet_seconds",
        "full_backup_retention_count",
        "full_backup_retention_max_bytes",
    ):
        value = getattr(args, field, None)
        if value is not None:
            setattr(cfg, field, value)
            changed = True
    if changed:
        path = save_config(cfg)
        print_json({"saved": str(path), "config": cfg.to_public_dict()})
    else:
        print_json(cfg.to_public_dict())


def cmd_capture(args: argparse.Namespace) -> None:
    cfg = load_config()
    raw = sys.stdin.read()
    item = capture_event(args.event, raw, cfg, cwd=os.getcwd())
    result = {
        "captured": item,
        "change": notify_codex_changed(
            cfg,
            event_id=item["id"],
            event=args.event,
            cwd=os.getcwd(),
            changed_at=item["created_at"],
        ),
    }
    if args.sync:
        result["sync"] = sync_once(cfg, cwd=os.getcwd())
    print_json(result)


def cmd_sync_now(args: argparse.Namespace) -> None:
    cfg = load_config()
    result: dict[str, object] = {"sync": sync_once(cfg, cwd=os.getcwd(), skip_unchanged=bool(args.skip_unchanged_snapshot))}
    result["flush_outbox"] = flush_outbox(cfg)
    if cfg.full_backup_enabled:
        result["full_backup_scan"] = scan_full_backup_changes(cfg, create_package=True, notify_dirty=True, check_remote=True)
        result["device_state"] = get_remote_device_state(cfg)
    print_json(result)


def cmd_desktop(args: argparse.Namespace) -> None:
    from .web_desktop import main as desktop_main

    desktop_main(open_browser=bool(args.browser), port=args.port, window=not args.browser and not args.no_open)


def cmd_desktop_legacy(_: argparse.Namespace) -> None:
    from .desktop import main as desktop_main

    desktop_main()


def cmd_daemon(_: argparse.Namespace) -> None:
    cfg = load_config()
    print_json({"preflight_backup": create_disaster_backup(cfg, reason="before_daemon")})
    run_daemon(cfg, log=print)


def cmd_channels(_: argparse.Namespace) -> None:
    print_json(list_channels())


def cmd_merge_channel(args: argparse.Namespace) -> None:
    print_json(merge_channels(load_config(), sources=args.source, target=args.to, all_others=args.all, close_running=args.close_codex))


def cmd_restore_channels(args: argparse.Namespace) -> None:
    print_json(restore_channels(load_config(), sources=args.source, all_merged=args.all, close_running=args.close_codex))


_DEPLOY_STR_FIELDS = (
    "ssh_target",
    "remote_dir",
    "service_name",
    "python",
    "bind_host",
    "data_dir",
    "token_file",
    "nginx_server_name",
    "client_max_body_size",
)


def _apply_deploy_overrides(cfg, args: argparse.Namespace) -> bool:
    changed = False
    for field in _DEPLOY_STR_FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            setattr(cfg, field, value)
            changed = True
    if getattr(args, "bind_port", None) is not None:
        cfg.bind_port = args.bind_port
        changed = True
    if getattr(args, "ssh_port", None) is not None:
        cfg.ssh_port = args.ssh_port
        changed = True
    if getattr(args, "max_body_bytes", None) is not None:
        cfg.max_body_bytes = args.max_body_bytes
        changed = True
    if getattr(args, "nginx", None) is not None:
        cfg.nginx_enabled = args.nginx
        changed = True
    return changed


def cmd_deploy_server(args: argparse.Namespace) -> None:
    cfg = load_deploy_config()
    _apply_deploy_overrides(cfg, args)  # 本次临时覆盖，不写入 deploy.json
    if args.install:
        print_json(install_server(cfg, dry_run=args.dry_run, assume_yes=args.yes, regenerate_token=args.regenerate_token))
    elif args.status:
        print_json(deploy_status(cfg))
    elif args.rollback:
        print_json(rollback_server(cfg, assume_yes=args.yes))
    else:
        print_json(update_server(cfg, dry_run=args.dry_run, assume_yes=args.yes))


def cmd_deploy_config(args: argparse.Namespace) -> None:
    cfg = load_deploy_config()
    if _apply_deploy_overrides(cfg, args):
        path = save_deploy_config(cfg)
        print_json({"saved": str(path), "config": cfg.to_dict()})
    else:
        print_json({"path": str(deploy_config_path()), "config": cfg.to_dict()})


def cmd_conversations(args: argparse.Namespace) -> None:
    print_json(list_conversations(search=args.search or "", cwd=args.cwd or "", provider=args.provider or "", include_archived=args.all))


def cmd_conversation_show(args: argparse.Namespace) -> None:
    print_json(read_conversation(args.id, include_tools=args.tools, include_reasoning=args.reasoning, include_developer=args.system))


def cmd_conversation_export(args: argparse.Namespace) -> None:
    print_json(export_conversation(args.id, fmt=("json" if args.json else "markdown"), include_tools=args.tools))


def cmd_merge_conversation(args: argparse.Namespace) -> None:
    print_json(merge_threads(load_config(), thread_ids=args.id, target=args.to, close_running=args.close_codex))


def cmd_restore_conversation(args: argparse.Namespace) -> None:
    print_json(restore_threads(load_config(), thread_ids=args.id, close_running=args.close_codex))


def cmd_backup_conversations(args: argparse.Namespace) -> None:
    if args.backup_id:
        archive, err = _ensure_local_archive(load_config(), args.backup_id)
        print_json({"success": False, "error": err} if err else list_backup_conversations(archive))
    else:
        print_json(list_importable_backups(load_config()))


def cmd_import_conversation(args: argparse.Namespace) -> None:
    cfg = load_config()
    archive, err = _ensure_local_archive(cfg, args.from_backup)
    if err:
        print_json({"success": False, "error": err})
        return
    print_json(import_conversations(cfg, archive, thread_ids=args.id, target_provider=args.to, close_running=args.close_codex))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("desktop", help="Open the Codex Sync desktop window.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="Open the local web UI in the default browser instead of the desktop window.")
    mode.add_argument("--no-open", action="store_true", help="Start the local web UI service without opening a window or browser.")
    p.add_argument("--port", type=int, help="Bind the local web UI to a specific port.")
    p.set_defaults(func=cmd_desktop)

    p = sub.add_parser("desktop-legacy", help="Open the legacy Tkinter desktop test console.")
    p.set_defaults(func=cmd_desktop_legacy)

    p = sub.add_parser("config", help="Show or update local configuration.")
    p.add_argument("--server-url")
    p.add_argument("--api-token")
    p.add_argument("--device-id")
    p.add_argument("--sync-interval-seconds", type=int)
    p.add_argument("--max-untracked-copy-bytes", type=int)
    p.add_argument("--full-backup-enabled", action=argparse.BooleanOptionalAction)
    p.add_argument("--full-backup-include-config", action=argparse.BooleanOptionalAction)
    p.add_argument("--full-backup-include-memories", action=argparse.BooleanOptionalAction)
    p.add_argument("--full-backup-allow-plaintext-upload", action=argparse.BooleanOptionalAction)
    p.add_argument("--full-backup-quiet-seconds", type=int)
    p.add_argument("--full-backup-retention-count", type=int)
    p.add_argument("--full-backup-retention-max-bytes", type=int)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("install-hooks", help="Install global Codex hooks for sync capture.")
    p.set_defaults(func=lambda _args: print_json(install_hooks()))

    p = sub.add_parser("hook-status", help="Show Codex hook installation status.")
    p.set_defaults(func=lambda _args: print_json(hook_status()))

    p = sub.add_parser("preflight-backup", help="Create a local Codex disaster backup.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=lambda args: print_json(create_disaster_backup(load_config(), reason=args.reason, force=args.force)))

    p = sub.add_parser("capture", help="Capture a Codex hook event from stdin.")
    p.add_argument("--event", required=True)
    p.add_argument("--sync", action="store_true")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("sync-now", help="Collect and upload one snapshot, then scan full conversation backup changes.")
    p.add_argument("--skip-unchanged-snapshot", action="store_true", help="Skip lightweight snapshot upload when stable snapshot content has not changed.")
    p.set_defaults(func=cmd_sync_now)

    p = sub.add_parser("flush-outbox", help="Send queued snapshots.")
    p.set_defaults(func=lambda _args: print_json(flush_outbox(load_config())))

    p = sub.add_parser("outbox", help="Show queued snapshot count.")
    p.set_defaults(func=lambda _args: print_json({"count": outbox_count()}))

    p = sub.add_parser("daemon", help="Run foreground sync loop.")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("resume", help="Generate a resume prompt from local state.")
    p.set_defaults(func=lambda _args: print_json({"path": str(create_resume_prompt(load_config(), cwd=os.getcwd()))}))

    p = sub.add_parser("git-snapshot", help="Create a local patch snapshot for the current repo.")
    p.set_defaults(func=lambda _args: print_json(create_patch_snapshot(os.getcwd(), load_config())))

    p = sub.add_parser("project-backup", help="Create a project backup package and upload it to the sync server.")
    p.set_defaults(func=lambda _args: print_json(backup_project_to_server(os.getcwd(), load_config())))

    p = sub.add_parser("list-project-backups", help="List project backups on the sync server.")
    p.set_defaults(func=lambda _args: print_json(list_project_backups(load_config())))

    p = sub.add_parser("full-backup-now", help="Create a full Codex conversation backup package, optionally uploading it.")
    p.add_argument("--force", action="store_true", help="Create a package even when the content digest has not changed.")
    p.add_argument("--upload", action="store_true", help="Upload the package to the sync server.")
    p.add_argument("--allow-plaintext-upload", action="store_true", help="Allow uploading an unencrypted full conversation backup.")
    p.set_defaults(func=lambda args: print_json(full_backup_now(load_config(), force=args.force, upload=args.upload, allow_plaintext_upload=args.allow_plaintext_upload)))

    p = sub.add_parser("full-backup-status", help="Scan local full backup digest and show remote device sync state.")
    p.set_defaults(func=lambda _args: print_json({"local": scan_full_backup_changes(load_config(), create_package=False), "remote": get_remote_device_state(load_config())}))

    p = sub.add_parser("notify-change", help="Notify the sync server that local Codex conversations changed.")
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=lambda args: print_json(notify_codex_changed(load_config(), reason=args.reason, include_digest=True)))

    p = sub.add_parser("list-full-backups", help="List full conversation backups on the sync server.")
    p.set_defaults(func=lambda _args: print_json(list_full_backups(load_config())))

    p = sub.add_parser("download-full-backup", help="Download a full conversation backup package from the sync server.")
    p.add_argument("backup_id")
    p.add_argument("--output")
    p.set_defaults(func=lambda args: print_json(download_full_backup(load_config(), args.backup_id, output=args.output)))

    p = sub.add_parser("restore-full-backup", help="Restore a downloaded full conversation backup package locally.")
    p.add_argument("archive")
    p.add_argument("--confirm-backup-id", required=True)
    p.add_argument("--restore-config", action="store_true")
    p.set_defaults(func=lambda args: print_json(restore_full_backup(load_config(), args.archive, confirm_backup_id=args.confirm_backup_id, restore_config=args.restore_config)))

    p = sub.add_parser("wsl-status", help="Detect WSL distros and Codex config.")
    p.set_defaults(func=lambda _args: print_json(wsl_status()))

    p = sub.add_parser("wsl-pull", help="Pull selected WSL Codex config files into local backup.")
    p.add_argument("distro")
    p.set_defaults(func=lambda args: print_json(pull_wsl_config(args.distro)))

    p = sub.add_parser("wsl-full-backup", help="Create a full Codex backup from a WSL distro.")
    p.add_argument("distro")
    p.add_argument("--no-config", action="store_true", help="Exclude config files such as config.toml and AGENTS.md.")
    p.add_argument("--no-memories", action="store_true", help="Exclude memories directory.")
    p.add_argument("--upload", action="store_true", help="Upload the WSL full backup package to the sync server.")
    p.add_argument("--allow-plaintext-upload", action="store_true", help="Allow uploading an unencrypted WSL full conversation backup.")
    p.set_defaults(
        func=lambda args: print_json(
            wsl_full_backup_now(
                load_config(),
                args.distro,
                include_config=not args.no_config,
                include_memories=not args.no_memories,
                upload=args.upload,
                allow_plaintext_upload=args.allow_plaintext_upload,
            )
        )
    )

    p = sub.add_parser("wsl-restore-full-backup", help="Restore a full Codex backup into a WSL distro.")
    p.add_argument("distro")
    p.add_argument("archive")
    p.add_argument("--confirm-backup-id", required=True)
    p.add_argument("--restore-config", action="store_true")
    p.set_defaults(func=lambda args: print_json(restore_wsl_full_backup(args.distro, args.archive, confirm_backup_id=args.confirm_backup_id, restore_config=args.restore_config)))

    p = sub.add_parser("wsl-restore-latest", help="Restore the latest local WSL full backup into a WSL distro.")
    p.add_argument("distro")
    p.add_argument("--restore-config", action="store_true")
    p.set_defaults(func=lambda args: print_json(restore_latest_wsl_full_backup(args.distro, restore_config=args.restore_config)))

    p = sub.add_parser("list-snapshots", help="List remote snapshots on the sync server.")
    p.set_defaults(func=lambda _args: print_json(list_remote_snapshots(load_config())))

    p = sub.add_parser("server-retention", help="Preview or apply sync server retention cleanup.")
    p.add_argument("--prune", action="store_true", help="Delete backups/snapshots selected by the server retention policy.")
    p.add_argument("--dry-run", action="store_true", help="Preview cleanup without deleting anything.")
    p.set_defaults(
        func=lambda args: print_json(
            prune_server_retention(load_config(), dry_run=True)
            if args.dry_run
            else prune_server_retention(load_config(), dry_run=False)
            if args.prune
            else get_server_retention(load_config())
        )
    )

    p = sub.add_parser(
        "server-version",
        aliases=["server-compatibility"],
        help="Check whether the remote sync server API is compatible with this client.",
    )
    p.set_defaults(func=lambda _args: print_json(check_server_compatibility(load_config())))

    p = sub.add_parser("snapshot-detail", help="Fetch one remote snapshot by id.")
    p.add_argument("snapshot_id")
    p.set_defaults(func=lambda args: print_json(get_remote_snapshot_detail(load_config(), args.snapshot_id)))

    p = sub.add_parser("remote-resume", help="Generate a resume prompt from a remote snapshot.")
    p.add_argument("snapshot_id")
    p.set_defaults(func=lambda args: print_json(generate_remote_resume_context(load_config(), args.snapshot_id)))

    p = sub.add_parser("restore-snapshot", help="Restore configuration files from a remote snapshot.")
    p.add_argument("snapshot_id")
    p.add_argument("--confirm-snapshot-id", required=True)
    p.add_argument("--restore-hooks", action="store_true")
    p.set_defaults(
        func=lambda args: print_json(
            restore_snapshot_locally(
                load_config(),
                args.snapshot_id,
                confirm_snapshot_id=args.confirm_snapshot_id,
                restore_hooks=args.restore_hooks,
            )
        )
    )

    p = sub.add_parser("preview-restore", help="Preview local config changes before restoring a remote snapshot.")
    p.add_argument("snapshot_id")
    p.add_argument("--restore-hooks", action="store_true")
    p.set_defaults(func=lambda args: print_json(preview_restore_snapshot(load_config(), args.snapshot_id, restore_hooks=args.restore_hooks)))

    p = sub.add_parser("install-task", help="Install a Windows scheduled task for periodic sync-now.")
    p.add_argument("--minutes", type=int, default=3)
    p.set_defaults(func=lambda args: print_json(install_windows_task(minutes=args.minutes)))

    p = sub.add_parser("uninstall-task", help="Uninstall the Windows scheduled sync task.")
    p.set_defaults(func=lambda _args: print_json(uninstall_windows_task()))

    p = sub.add_parser("task-status", help="Show Windows scheduled sync task status.")
    p.set_defaults(func=lambda _args: print_json(windows_task_status()))

    p = sub.add_parser("channels", help="List Codex conversation channels (model_providers) and merge status.")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("merge-channel", help="Merge conversations from given channel(s) into the current channel list.")
    p.add_argument("--from", dest="source", action="append", help="Source provider to merge (repeatable).")
    p.add_argument("--to", dest="to", help="Target provider (default: current Codex model_provider).")
    p.add_argument("--all", action="store_true", help="Merge all other channels into the target.")
    p.add_argument("--close-codex", action="store_true", help="检测到 Codex 在运行时自动关闭其后台进程（会自动重启，不影响编辑器）。")
    p.set_defaults(func=cmd_merge_channel)

    p = sub.add_parser("restore-channels", help="Restore previously merged conversations to their original channels.")
    p.add_argument("--from", dest="source", action="append", help="Original provider to restore (repeatable).")
    p.add_argument("--all", action="store_true", help="Restore all merged conversations.")
    p.add_argument("--close-codex", action="store_true", help="检测到 Codex 在运行时自动关闭其后台进程（会自动重启，不影响编辑器）。")
    p.set_defaults(func=cmd_restore_channels)

    def _add_conn_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--ssh-target", help="user@host 或 SSH config 别名。")
        sp.add_argument("--ssh-port", type=int, help="SSH 端口，默认 22。")
        sp.add_argument("--remote-dir")
        sp.add_argument("--service-name")
        sp.add_argument("--python")
        sp.add_argument("--bind-host")
        sp.add_argument("--bind-port", type=int)
        sp.add_argument("--data-dir")
        sp.add_argument("--token-file")
        sp.add_argument("--max-body-bytes", type=int)
        sp.add_argument("--nginx", action=argparse.BooleanOptionalAction, help="启用/禁用 nginx 反代。")
        sp.add_argument("--nginx-server-name", help="nginx server_name（域名或公网 IP）。")
        sp.add_argument("--client-max-body-size")

    p = sub.add_parser("deploy-server", help="通过 SSH 部署/更新服务器端 sync_server.py。")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="干净服务器全新安装（systemd + token + 可选 nginx）。")
    mode.add_argument("--update", action="store_true", help="更新已部署的 sync_server.py 并重启（默认）。")
    mode.add_argument("--status", action="store_true", help="查看远程服务状态（只读）。")
    mode.add_argument("--rollback", action="store_true", help="用最近备份回滚并重启。")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不实际执行。")
    p.add_argument("--yes", action="store_true", help="确认执行高危写操作（install/update/rollback 必需）。")
    p.add_argument("--regenerate-token", action="store_true", help="install 时强制重新生成 token。")
    _add_conn_args(p)
    p.set_defaults(func=cmd_deploy_server)

    p = sub.add_parser("deploy-config", help="查看或设置部署配置（~/.codex-sync/deploy.json）；带参数即设置，否则查看。")
    _add_conn_args(p)
    p.set_defaults(func=cmd_deploy_config)

    p = sub.add_parser("conversations", help="列出本机 Codex 对话（可搜索 / 按项目或渠道筛选）。")
    p.add_argument("--search")
    p.add_argument("--cwd")
    p.add_argument("--provider")
    p.add_argument("--all", action="store_true", help="包含已归档对话。")
    p.set_defaults(func=cmd_conversations)

    p = sub.add_parser("conversation-show", help="查看一个对话的正文（默认只显示用户/助手消息）。")
    p.add_argument("id")
    p.add_argument("--tools", action="store_true", help="包含工具调用/输出。")
    p.add_argument("--reasoning", action="store_true", help="包含思考过程。")
    p.add_argument("--system", action="store_true", help="包含系统注入（developer 消息）。")
    p.set_defaults(func=cmd_conversation_show)

    p = sub.add_parser("conversation-export", help="导出对话为 Markdown（默认）或 JSON 到 ~/.codex-sync/exports。")
    p.add_argument("id")
    p.add_argument("--json", action="store_true", help="导出为 JSON 而非 Markdown。")
    p.add_argument("--tools", action="store_true", help="导出包含工具调用/输出。")
    p.set_defaults(func=cmd_conversation_export)

    p = sub.add_parser("merge-conversation", help="把指定对话并入当前（或目标）渠道——按对话粒度。")
    p.add_argument("--id", action="append", required=True, help="对话 ID（可重复）。")
    p.add_argument("--to", help="目标渠道（默认当前渠道）。")
    p.add_argument("--close-codex", action="store_true", help="检测到 Codex 在运行时自动关闭其后台进程（会自动重启）。")
    p.set_defaults(func=cmd_merge_conversation)

    p = sub.add_parser("restore-conversation", help="把指定对话还原到其原始渠道——按对话粒度。")
    p.add_argument("--id", action="append", required=True, help="对话 ID（可重复）。")
    p.add_argument("--close-codex", action="store_true", help="检测到 Codex 在运行时自动关闭其后台进程（会自动重启）。")
    p.set_defaults(func=cmd_restore_conversation)

    p = sub.add_parser("backup-conversations", help="列出可导入的备份包；带 --backup-id 则列出该包内的对话。")
    p.add_argument("--backup-id", help="备份包 ID（不带则列出所有可导入备份包）。")
    p.set_defaults(func=cmd_backup_conversations)

    p = sub.add_parser("import-conversation", help="从备份包导入对话到本机并接入指定渠道（写库需 Codex 关闭）。")
    p.add_argument("--id", action="append", required=True, help="要导入的对话 ID（可重复）。")
    p.add_argument("--from-backup", required=True, help="来源备份包 ID。")
    p.add_argument("--to", help="目标渠道（默认当前渠道）。")
    p.add_argument("--close-codex", action="store_true", help="检测到 Codex 在运行时自动关闭其后台进程（会自动重启）。")
    p.set_defaults(func=cmd_import_conversation)

    return parser


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
