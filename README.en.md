# Codex Sync

[中文](README.md)

Codex Sync is a Windows-side Codex conversation sync and disaster recovery tool. It focuses on local full conversation backups, selective restore/import, project snapshot upload to a self-hosted server, dirty-state notifications, branch conflict protection, Windows scheduled sync, WSL support, and a local web desktop console.

Full conversation archives are sensitive. Client-side encryption is not implemented yet, so full backups are stored locally by default and are uploaded only when plaintext upload is explicitly enabled.

## Quick Start

Install from a source checkout:

```powershell
python -m pip install -e .
```

Start the local web desktop:

```powershell
codex-sync desktop
```

You can also run:

```text
Start-CodexSyncDesktop.bat
```

The legacy Tkinter console remains available:

```powershell
codex-sync desktop-legacy
```

## Common Commands

```powershell
codex-sync config --server-url https://sync.example.com --api-token YOUR_TOKEN
codex-sync install-hooks
codex-sync hook-status
codex-sync sync-now
codex-sync daemon
codex-sync install-task --minutes 3
codex-sync task-status
codex-sync uninstall-task
```

Full conversation backups:

```powershell
python -m codex_sync full-backup-status
python -m codex_sync full-backup-now
python -m codex_sync full-backup-now --upload --allow-plaintext-upload
python -m codex_sync list-full-backups
python -m codex_sync download-full-backup BACKUP_ID
python -m codex_sync restore-full-backup ARCHIVE.zip --confirm-backup-id BACKUP_ID
```

WSL and project backup commands:

```powershell
python -m codex_sync wsl-status
python -m codex_sync wsl-pull DISTRO
python -m codex_sync git-snapshot
python -m codex_sync project-backup
python -m codex_sync list-project-backups
```

`git-snapshot` creates a local patch snapshot for the current Git working tree. `project-backup` uploads a project zip to the configured sync server. Git repositories use patch snapshots; non-Git folders are packed as ordinary files with the same safety exclusions. `.env`, keys, certificates, build outputs, dependency folders, and caches are skipped.

## Sync Model

`sync-now` performs three operations:

1. Checks local Codex state and skips unchanged lightweight snapshot uploads when possible.
2. Flushes queued lightweight snapshots from the local outbox.
3. Scans full conversation content digests, sends dirty notifications when content changes, and creates a local full backup after the content has been quiet.

The main cross-machine workflow is:

```text
full conversation backup -> download/import -> choose target Codex home/channel
```

Remote lightweight snapshots are kept as diagnostics and fallback handoff state. They include cwd, Git status, configuration summaries, and recent events, but not full conversation text.

## Server

Start a local test server:

```powershell
codex-sync-server
```

Source checkouts can also use the compatibility wrapper:

```powershell
python sync_server.py
```

Default local address:

```text
http://127.0.0.1:8888
```

Token source order:

- `CODEX_SYNC_SERVER_TOKEN`
- `CODEX_SYNC_SERVER_TOKEN_FILE`
- an auto-generated token file

## One-Click Deployment

`deploy-server` deploys the packaged server entry to your own Linux server over SSH. Connection settings live in `~/.codex-sync/deploy.json`; `deploy.example.json` is only a template.

```powershell
python -m codex_sync deploy-config --ssh-target user@your-server
python -m codex_sync deploy-server --install --yes
python -m codex_sync deploy-server --update --yes
python -m codex_sync deploy-server --status
python -m codex_sync deploy-server --rollback --yes
```

The default install uses `/opt/codex-sync-server` for code, `/var/lib/codex-sync` for data, `/etc/codex-sync/server-token` for the API token, and binds the server to `0.0.0.0:8888`. The generated `server_url` and token are written back to the local client config.

## Security Notes

- Full conversation backups contain real conversation content.
- Plaintext cloud upload is disabled by default.
- The local web desktop binds to `127.0.0.1` and uses a per-process session token. Do not expose it through a proxy.
- Do not commit `deploy.json`, `.env` files, SSH keys, API tokens, generated archives, or local database files.
- The sync server uses bearer-token authentication. Use HTTPS or a trusted private network for production.

## License

MIT. See `LICENSE`.
