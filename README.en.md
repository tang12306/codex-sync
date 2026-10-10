# Codex Sync

[中文](README.md)

Codex Sync is a Windows desktop app for carrying Codex work across devices. It syncs project progress, uploads project snapshots, backs up and imports Codex conversations, and manages Windows and WSL Codex environments from one local console.

The primary experience is the EXE desktop app. The CLI remains available for automation and troubleshooting, but daily use should happen through the desktop console.

## What It Does

- **Resume Codex conversations across devices**: back up full conversations from one machine, then import selected conversations into a target Windows or WSL Codex environment on another machine.
- **Back up project code**: upload a full source snapshot of any project directory (excluding `.git`, dependencies, build output, caches, and secrets), restore any version into any directory, and optionally back up automatically after Git commits, in real time, or when Codex stops.
- **Treat Windows and WSL as first-class homes**: each WSL distro can be backed up, restored, and used as an import target.
- **Use your own server**: backups go to your self-hosted sync server instead of a GitHub backup branch.
- **Protect conflicting histories**: full backups use an append-only branch model so remote changes do not overwrite older conversation chains.
- **Create disaster backups before risky writes**: restore, import, and channel merge operations create local protection backups first.

## Desktop Experience

The desktop app is the main entry point:

- Overview shows local, server, and other-device sync state plus anything that needs attention.
- Conversations & Channels lets you browse, search, and export local conversations and organize them across channels (model providers).
- Project Backup uploads snapshots of any project directory, manages automatic backups, and previews/restores any server version into any directory.
- Conversation Backups handles Windows/WSL full backups, the cloud backup list, selective import from a backup, and server retention cleanup.
- Settings is split into Server & Deployment, Backup Policy (including the encryption passphrase), Install & Automation (hooks, scheduled task, startup, app updates), and Service & Diagnostics.
- The log panel in the top-right corner shows the detailed result of every action.

The local web desktop binds only to `127.0.0.1` and generates a per-process session token. Do not expose it through a reverse proxy.

## Quick Start

If you downloaded a release build, run the Windows installer:

```text
CodexSyncSetup-vX.Y.Z-windows-x64.exe
```

It is a standard setup wizard: choose the install folder (default `%LOCALAPPDATA%\Programs\CodexSync`, per-user, no admin rights needed) and whether to create a desktop shortcut and start on login. Upgrading from 0.4.0 or earlier installs into the previous folder and takes over the old installation. If Codex Sync is running, the installer asks to close it first. Launching the app again focuses the existing window instead of starting a second instance.

To uninstall, use Windows `Settings -> Apps -> Installed apps`. Uninstalling also removes the Codex hooks and scheduled task that point to this app; your config and backup history in `~/.codex-sync` are kept.

Closing the window shows a native Windows confirmation: Yes minimizes to tray and keeps the local service running, No exits and stops the service, and Cancel returns to the app. Start on login can be chosen during setup, or toggled later from `Settings -> Install & Automation -> Install & Startup`.

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
3. Set an encryption passphrase in `Settings -> Backup Policy` (use the same passphrase on your other machines).
4. Create and upload a full conversation backup from Conversation Backups.
5. Select your current project and upload a project snapshot from Project Backup.
6. On another machine, open `Conversation Backups -> Import from Backup`, choose a backup, a target Windows/WSL home, and a channel, then import. Restore projects from `Project Backup -> Server Project Library`.

## Self-Hosted Sync Server

Codex Sync uses a small sync server to store project backups and full conversation backups. Install it on a Linux server:

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

Full conversation archives contain real context. Codex Sync encrypts full backup archives on the client before upload by default. The encryption key is taken from `CODEX_SYNC_FULL_BACKUP_PASSPHRASE`, the saved full-backup passphrase, or an auto-generated local key file under `~/.codex-sync/`. Use the same passphrase on other devices to restore encrypted cloud backups. Plaintext upload remains blocked unless `--allow-plaintext-upload` or the matching setting is explicitly enabled.

Project backups skip `.env`, keys, certificates, `node_modules`, `dist`, `build`, cache folders, and files exceeding the configured size limit.

## Automatic Sync and Hooks

Codex hooks mark local state as changed when sessions start, prompts are submitted, context is compacted, or a session stops. Periodic scans also detect conversation changes by content digest.

Install a Windows scheduled task to scan for conversation changes and generate local full backups even when the desktop app is closed:

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
