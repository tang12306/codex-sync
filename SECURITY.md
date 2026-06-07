# Security Policy

Codex Sync handles local Codex state, conversation archives, project snapshots,
server deployment credentials, and WSL files. Treat generated backups and local
configuration as sensitive.

## Supported Versions

This repository is currently alpha software. Security fixes target the latest
`main` branch until the project starts publishing versioned releases.

## Reporting a Vulnerability

Open a private security advisory on GitHub when available. If advisories are
not enabled yet, contact the repository owner privately before filing a public
issue.

## Security Notes

- Full conversation backups contain real conversation content. Cloud upload is
  blocked by default unless plaintext upload is explicitly enabled.
- The local web desktop binds to `127.0.0.1` and uses a per-process session
  token for API requests. Do not expose it through a proxy.
- `deploy.json`, API tokens, SSH passwords, private keys, `.env` files, and
  generated backup archives must not be committed.
- The sync server uses bearer-token authentication. Put it behind HTTPS or a
  trusted private network for production use.
