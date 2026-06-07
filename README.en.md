# Codex Sync

[中文](README.md)

Codex Sync is a Windows desktop app for carrying Codex work across devices. It syncs project progress, uploads project snapshots, backs up and imports Codex conversations, and manages Windows and WSL Codex environments from one local console.

The primary experience is the EXE desktop app. The CLI remains available for automation and troubleshooting, but daily use should happen through the desktop console.

## What It Does

- **Resume Codex conversations across devices**: back up full conversations from one machine, then import selected conversations into a target Windows or WSL Codex environment on another machine.
- **Sync project progress**: choose any project directory; Git projects are uploaded as patch snapshots, while non-Git projects are packed with safety exclusions.
- **Treat Windows and WSL as first-class homes**: each WSL distro can be backed up, restored, and used as an import target.
- **Use your own server**: backups go to your self-hosted sync server instead of a GitHub backup branch.
- **Protect conflicting histories**: full backups use an append-only branch model so remote changes do not overwrite older conversation chains.
- **Create disaster backups before risky writes**: restore, import, and channel merge operations create local protection backups first.

## Desktop Experience

The desktop app is the main entry point:

- Overview shows local, remote, and other-device sync state.
- Conversations & Backups handles full backups, cloud backup lists, import, browsing, and channel merging.
- Project Backup lets you choose any project directory and upload a project snapshot.
- Settings manages server connection, one-click deployment, backup policy, scheduled tasks, and diagnostics.

The local web desktop binds only to `127.0.0.1` and generates a per-process session token. Do not expose it through a reverse proxy.

## Quick Start

If you downloaded a release package, run:

```text
CodexSync.exe
```

By default it opens the standalone Codex Sync desktop window. Closing the window also stops the local service.

From source:

```powershell
python -m pip install -e .
codex-sync desktop
```

The repository also includes a Windows launcher:

```text
Start-CodexSyncDesktop.bat
```

## Recommended Workflow

1. Open Codex Sync on your main machine.
2. Configure or one-click deploy your sync server from Settings.
3. Create a full conversation backup from Conversations & Backups.
4. Select your current project and upload a project snapshot from Project Backup.
5. On another machine, open Codex Sync, list cloud backups, choose a target Windows/WSL home and channel, then import.

Lightweight snapshots are diagnostic handoff state: cwd, Git status, configuration summaries, and recent events. The main cross-device conversation workflow is full backup plus selective import.

## Self-Hosted Sync Server

Codex Sync uses a small sync server to store lightweight snapshots, project backups, and full conversation backups. Install it on a Linux server:

```powershell
python -m codex_sync deploy-config --ssh-target user@your-server
python -m codex_sync deploy-server --install --yes
```

Default deployment paths:

- Code: `/opt/codex-sync-server`
- Data: `/var/lib/codex-sync`
- Token: `/etc/codex-sync/server-token`
- Bind address: `0.0.0.0:8888`

After installation, the local client config is backfilled with `server_url` and the generated API token. The desktop Settings page can also check status, update deployment, and use a temporary SSH password.

Local test server:

```powershell
codex-sync-server
```

## Backup Contents and Security

Full conversation backups include Codex conversation text, thread indexes, and required state databases. They exclude by default:

- `auth.json`, `cap_sid`
- `.env`
- SSH keys, certificates, and key files
- `config.toml`, `hooks.json`
- browser state, caches, temp folders, and runtime process folders

Client-side encryption is not implemented yet. Full conversation archives contain real context, so cloud upload is disabled by default and requires explicit plaintext-upload approval.

Project backups skip `.env`, keys, certificates, `node_modules`, `dist`, `build`, cache folders, and files exceeding the configured size limit.

## Automatic Sync and Hooks

Codex hooks mark local state as changed when sessions start, prompts are submitted, context is compacted, or a session stops. Periodic scans also detect conversation changes by content digest.

Install a Windows scheduled task for background lightweight sync and local full-backup generation:

```powershell
codex-sync install-task --minutes 3
```

Plaintext full conversation archives are not uploaded automatically by default.

## Developer and Automation Commands

The desktop app is the main UI. These commands are useful for development, automation, and troubleshooting:

```powershell
codex-sync desktop
codex-sync desktop --browser
codex-sync desktop --no-open --port 8765
codex-sync config --server-url https://sync.example.com --api-token YOUR_TOKEN
codex-sync sync-now
codex-sync full-backup-now
codex-sync full-backup-now --upload --allow-plaintext-upload
codex-sync list-full-backups
codex-sync project-backup
codex-sync list-project-backups
codex-sync wsl-status
codex-sync deploy-server --status
```

## Project Status

Codex Sync is alpha software. The current focus is the Windows desktop app, WSL workflows, self-hosted server backups, and practical cross-device Codex conversation migration.

## License

MIT. See `LICENSE`.
