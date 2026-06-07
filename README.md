# Codex Sync

Codex Sync 是一个 Windows 侧的 Codex 对话同步与灾难保护工具。当前版本重点解决：完整对话本地备份与导入、项目补丁快照上传服务器、服务器 dirty 状态通知、冲突分支保护、Windows 定时同步和桌面控制台。

当前项目还没有接入客户端加密，所以完整对话包默认只生成在本地，不会自动上传云端。只有显式使用 `--allow-plaintext-upload` 或在配置里允许明文上传时，才会把完整对话包传到服务器。

## 快速启动

从源码目录安装为可编辑包：

```powershell
python -m pip install -e .
```

启动现代化本地 Web 桌面端：

```powershell
codex-sync desktop
```

也可以双击：

```text
Start-CodexSyncDesktop.bat
```

旧版 Tkinter 控制台仍保留：

```powershell
codex-sync desktop-legacy
```

## 常用命令

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

完整对话备份：

```powershell
python -m codex_sync full-backup-status
python -m codex_sync full-backup-now
python -m codex_sync full-backup-now --upload --allow-plaintext-upload
python -m codex_sync list-full-backups
python -m codex_sync download-full-backup BACKUP_ID
python -m codex_sync restore-full-backup ARCHIVE.zip --confirm-backup-id BACKUP_ID
```

远端轻量快照（诊断 / 兜底）：

```powershell
python -m codex_sync list-snapshots
python -m codex_sync snapshot-detail SNAPSHOT_ID
python -m codex_sync remote-resume SNAPSHOT_ID
python -m codex_sync preview-restore SNAPSHOT_ID
python -m codex_sync restore-snapshot SNAPSHOT_ID --confirm-snapshot-id SNAPSHOT_ID
```

WSL 和项目备份能力：

```powershell
python -m codex_sync wsl-status
python -m codex_sync wsl-pull DISTRO
python -m codex_sync git-snapshot
python -m codex_sync project-backup
python -m codex_sync list-project-backups
```

`git-snapshot` 只在本机生成当前 Git 工作区的补丁快照。`project-backup` 会上传项目 zip 到自建同步服务器：当前目录是 Git 仓库时使用补丁快照；非 Git 目录会按安全排除规则打包普通文件。`.env`、密钥、构建产物和缓存目录会被跳过，不再使用 GitHub 备份分支。

## 当前同步模型

`sync-now` 会执行三件事：

1. 检查本机状态；定时任务会在轻量快照内容无变化时跳过上传。
2. 尝试发送本地 outbox 中之前失败的轻量快照。
3. 扫描完整对话内容 digest，发现变化时补发 dirty 通知，并在内容安静一段时间后生成本地完整对话包。

跨机器继续对话的主流程是「完整对话备份 -> 下载/导入 -> 选择本机渠道」。远端轻量快照只保留为诊断和兜底接力：它记录 cwd、Git 状态、配置摘要和最近事件，不包含完整对话正文。

Codex hooks 会在 `SessionStart`、`UserPromptSubmit`、`PreCompact`、`PostCompact`、`Stop` 等事件发生时立即调用本工具，把本机状态标记为 dirty。即使 hook 因异常没有执行，下一次周期扫描也会通过内容 digest 识别本地对话已经变化，并补发 dirty。

默认的完整备份安静期是 60 秒，避免 Codex 正在写入会话文件时每 3 分钟生成一个大 zip。可以调整：

```powershell
python -m codex_sync config --full-backup-quiet-seconds 60
```

本地完整备份包默认最多保留 20 个、总量最多 2GB：

```powershell
python -m codex_sync config --full-backup-retention-count 20
python -m codex_sync config --full-backup-retention-max-bytes 2147483648
```

## 冲突分支保护

完整对话备份是 append-only 模型，不会用新备份直接覆盖旧备份。

客户端上传完整备份前会读取服务器上的当前设备 head：

- 如果本地内容 digest 已经等于远端 head digest，本机会直接采纳远端 head，不重复上传。
- 如果远端 head 和本地 `parent_backup_id` 一致，上传会继续当前分支。
- 如果远端 head 已经变化，而本地仍基于旧 parent，客户端会自动切到新分支，例如 `device:branch-20260606T040000Z-abcd1234`。
- 服务器会把这种情况标记为 `sync_state=diverged`，保留两个分支，避免异常同步时把旧对话链路覆盖掉。

## 完整对话备份内容

默认包含：

- `sessions/`
- `threads/`
- `history.jsonl`
- `session_index.jsonl`
- `.codex-global-state.json`
- `goals_*.sqlite*`
- `logs_*.sqlite*`
- `state_*.sqlite*`

默认排除：

- `auth.json`
- `cap_sid`
- `.env`
- SSH 私钥、证书、密钥文件
- `browser/`
- `cache/`
- `tmp/`
- `computer-use/`
- `node_repl/`
- `process_manager/`
- `config.toml`、`hooks.json` 等配置文件

如果确实需要把配置或 memories 放进完整备份包，可以显式开启：

```powershell
python -m codex_sync config --full-backup-include-config
python -m codex_sync config --full-backup-include-memories
```

## 灾难保护

敏感操作前会先创建本地灾难备份，位置：

```text
%USERPROFILE%\.codex-sync\disaster-backups
```

自动触发点包括：

- 打开桌面端时
- 启动 daemon 前
- 安装 Codex hooks 前
- 执行 `python -m codex_sync daemon` 前
- 还原远端快照或完整备份前

默认 24 小时内只自动创建一次，避免频繁复制大文件。需要强制创建：

```powershell
python -m codex_sync preflight-backup --force --reason manual
```

## Windows 定时任务

安装 3 分钟一次的自动同步：

```powershell
python -m codex_sync install-task --minutes 3
```

查看或移除：

```powershell
python -m codex_sync task-status
python -m codex_sync uninstall-task
```

当前 3 分钟任务默认不会自动上传完整对话明文包，只会上传轻量快照、补发 dirty 状态，并在本地生成完整对话包。等客户端加密完成后，再适合把完整包纳入自动云端上传。

## 服务器

本地测试服务器：

```powershell
codex-sync-server
```

源码目录也可以继续运行兼容入口：

```powershell
python sync_server.py
```

本地测试默认监听：

```text
http://127.0.0.1:8888
```

服务器接口：

- `POST /api/snapshots`
- `GET /api/snapshots`
- `GET /api/snapshots/{id}`
- `POST /api/changes`
- `GET /api/device-state?device_id=...`
- `GET /api/devices`
- `POST /api/full-backups`
- `GET /api/full-backups`
- `GET /api/full-backups/{id}`
- `GET /api/full-backups/{id}/download`
- `POST /api/project-backups`
- `GET /api/project-backups`
- `GET /api/project-backups/{id}`
- `GET /api/project-backups/{id}/download`

Token 来源：

- 优先读取环境变量 `CODEX_SYNC_SERVER_TOKEN`
- 否则读取 `CODEX_SYNC_SERVER_TOKEN_FILE`
- 都不存在时自动生成 token 文件

### 一键部署 / 全新安装

无需手动 SSH，`deploy-server` 会把已打包的服务器入口部署到你自己的服务器。连接信息存在 `~/.codex-sync/deploy.json`（仓库里的 `deploy.example.json` 是占位示例，复制后改成你的值，或直接用 CLI 配置）：

```powershell
# 1) 配置连接（user@host 或 SSH config 别名）
python -m codex_sync deploy-config --ssh-target user@your-server

# 2) 干净服务器首次全新安装（systemd 服务 + token）
python -m codex_sync deploy-server --install --yes

# 3) 之后改了 sync_server.py，一键更新并重启
python -m codex_sync deploy-server --update --yes
python -m codex_sync deploy-server --update --dry-run   # 仅预览将执行的命令，不实际执行

# 查看远程状态 / 回滚到最近一次备份
python -m codex_sync deploy-server --status
python -m codex_sync deploy-server --rollback --yes
```

- 全新安装按通用 FHS 路径部署：程序 `/opt/codex-sync-server`、数据 `/var/lib/codex-sync`、token `/etc/codex-sync/server-token`（均可在 `deploy.json` 改），默认绑定 `0.0.0.0:8888`，并自动把 `server_url` 与生成的 token 回填到本机客户端配置。
- 更新会先备份远程旧版（`sync_server.py.bak-<时间戳>`）、远程 `py_compile` 校验语法通过后才重启，失败可 `--rollback --yes`。
- 依赖系统 OpenSSH；可使用 SSH key 或在网页控制台临时输入 SSH 密码。目标机需 root、已装 `python3`（启用 nginx 时还需 `nginx`）。
- 网页控制台「设置 → 服务器部署」也提供「查看状态 / 更新部署」按钮（全新安装仅限 CLI）。
- `deploy.json` 含你的服务器连接信息，**不要提交到版本库**（它在用户目录 `~/.codex-sync/`，默认不在仓库内）。

## 本地目录

工具状态目录：

```text
%USERPROFILE%\.codex-sync
```

Codex hooks 文件：

```text
%USERPROFILE%\.codex\hooks.json
```

执行 `install-hooks` 后，需要在 Codex 里打开 `/hooks` 并信任新 hook 定义。

## 安全边界

轻量快照会做基础脱敏，默认不会上传原始 `config.toml`、`AGENTS.md`、provider token、MCP 配置等敏感内容。

完整对话备份包含真实对话上下文，属于敏感数据。当前版本没有客户端加密，所以不要在未接受风险的情况下开启明文完整备份上传。

本地 Web 桌面端只绑定 `127.0.0.1`，并为每次启动生成随机会话 token。不要把本地桌面端通过反代暴露到网络。

## License

MIT. See `LICENSE`.
