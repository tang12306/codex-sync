# Codex Sync

[English](README.en.md)

Codex Sync 是一个面向 Windows 的桌面应用，用来在多台电脑之间接续 Codex 工作现场：同步项目进度、备份项目快照、迁移/合并 Codex 对话，并把 Windows 与 WSL 的 Codex 环境放在同一个界面里管理。

它的目标不是再提供一堆命令行脚本，而是让你打开一个 EXE 控制台就能看到当前机器、WSL、项目和远端服务器的状态，并完成备份、恢复、导入和接力。

## 你可以用它做什么

- **跨设备接续 Codex 对话**：把一台机器上的完整 Codex 对话备份到服务器，在另一台机器上选择目标环境和渠道后导入继续。
- **备份项目代码**：选择任意项目目录上传完整源码快照（自动排除 `.git`、依赖、构建产物、缓存和密钥），可以按版本恢复到任意目录；也可以开启 Git 提交后、实时或 Codex 关闭时自动备份。
- **统一管理 Windows 与 WSL**：Windows Codex Home 和不同 WSL 发行版都作为一等环境显示，可分别备份、恢复和导入对话。
- **自建服务器备份**：项目和对话备份上传到你自己的同步服务器，不依赖 GitHub 备份分支。
- **冲突保护**：完整备份采用 append-only 分支模型，远端状态变化时自动保留分支，避免覆盖旧对话链路。
- **灾难恢复**：执行导入、恢复、渠道合并等敏感写入前，会先创建本地灾难备份。

## 桌面端体验

桌面端是主要入口，适合日常使用：

- “首页”展示本机、服务器和其它设备的同步状态，以及需要处理的事项。
- “对话与渠道”用来浏览、搜索、导出本机对话，并整理不同渠道（model provider）下的对话记录。
- “项目备份”可以选择任意项目目录上传快照、开启自动备份，并从服务器项目库按版本预览和恢复到任意目录。
- “对话备份与恢复”负责 Windows / WSL 完整对话备份、云端备份列表、从备份选择性导入对话，以及服务器留存清理。
- “系统设置”分为服务器与部署、备份策略（含加密口令）、安装与自动化（Hooks、定时任务、开机自启、应用更新）、后台与诊断四个分区。
- 右上角可以打开日志面板，查看每次操作的详细结果。

本地 Web 桌面端只绑定 `127.0.0.1`，每次启动都会生成随机会话 token。不要把它通过反向代理暴露到网络。

## 快速开始

如果你下载的是发布版，下载 Windows 安装包并双击运行：

```text
CodexSyncSetup-vX.Y.Z-windows-x64.exe
```

这是一个标准的安装向导：可以选择安装目录（默认 `%LOCALAPPDATA%\Programs\CodexSync`，按当前用户安装，不需要管理员权限），勾选是否创建桌面快捷方式、是否开机自启。从 0.4.0 及更早版本升级时会默认装回原来的目录并接管旧安装。如果 Codex Sync 正在运行，安装程序会先提示关闭它。重复双击桌面快捷方式不会启动第二个实例，会尝试切回已有窗口。

卸载：在 Windows“设置 → 应用 → 已安装的应用”里找到 Codex Sync。卸载会一并撤掉指向本程序的 Codex hooks 和定时任务，`~/.codex-sync` 里的配置和备份记录会保留。

点击关闭按钮时会弹出原生 Windows 确认框：选择“是”最小化到托盘并继续运行，选择“否”彻底退出并停止本地服务，选择“取消”返回应用。开机自启可以在安装时勾选，也可以之后在“系统设置 -> 安装与自动化 -> 安装与开机自启”中开关。

源码运行：

```powershell
python -m pip install -e .
codex-sync desktop
```

仓库内也提供一个 Windows 启动脚本：

```text
Start-CodexSyncDesktop.bat
```

## 推荐工作流

1. 在主力机器打开 Codex Sync 桌面端。
2. 在“系统设置”里配置或一键部署你的同步服务器。
3. 在“系统设置 -> 备份策略 -> 完整备份加密”里设置加密口令（另一台电脑填同一个口令）。
4. 在“对话备份与恢复”里生成并上传完整对话备份。
5. 在“项目备份”里选择当前项目并上传项目快照。
6. 到另一台机器打开 Codex Sync，在“对话备份与恢复 -> 从备份导入对话”里选择备份、目标 Windows/WSL 环境和渠道后导入；项目在“项目备份 -> 服务器项目库”里恢复。

## 自建同步服务器

Codex Sync 使用一个轻量同步服务器保存项目备份和完整对话备份。你可以在 Linux 服务器上一键安装：

```powershell
python -m codex_sync deploy-config --ssh-target user@your-server
python -m codex_sync deploy-server --install --yes
```

默认部署位置：

- 程序：`/opt/codex-sync-server`
- 数据：`/var/lib/codex-sync`
- Token：`/etc/codex-sync/server-token`
- 监听：`0.0.0.0:8888`

部署成功后，本机客户端会自动写入 `server_url` 和 API token。桌面端“系统设置 -> 服务器与部署”也可以一键安装、更新部署、查看远程服务状态，SSH 密码只用于当次操作。

本地测试服务器：

```powershell
codex-sync-server
```

## 备份内容与安全边界

完整对话备份默认包含 Codex 的会话正文、线程索引和必要状态数据库，但默认排除：

- `auth.json`、`cap_sid`
- `.env`
- SSH 私钥、证书、密钥文件
- `config.toml`、`hooks.json`
- 浏览器、缓存、临时目录和运行时进程目录

完整对话备份包含真实上下文。Codex Sync 默认会在客户端加密完整备份包后再上传；加密密钥优先来自 `CODEX_SYNC_FULL_BACKUP_PASSPHRASE`，其次来自设置中保存的完整备份口令，最后会使用 `~/.codex-sync/` 下自动生成的本机私有 key 文件。跨设备恢复加密云端备份时，请在其它设备设置相同口令。只有你显式开启 `--allow-plaintext-upload` 或在设置中允许明文上传时，才会上传未加密完整对话包。

项目备份会跳过 `.env`、密钥、证书、`node_modules`、`dist`、`build`、缓存目录和超出大小限制的文件。

## 自动同步与 Hook

安装 Codex hooks 后，本工具会在 Codex 会话开始、用户提交、压缩上下文和停止等事件发生时标记本机状态变化。即使 hook 没有运行，定时扫描也会通过内容 digest 发现对话变化。

Windows 定时任务可以在桌面端没有打开时，也定期在后台扫描对话变化并生成本地完整备份：

```powershell
codex-sync install-task --minutes 3
```

默认不会自动上传完整对话明文包。

## 开发者与自动化命令

桌面端是主要使用方式；下面命令用于开发、自动化和排障：

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

## 当前状态

项目仍处于 alpha 阶段，优先保证 Windows 桌面端、WSL 场景、自建服务器和 Codex 对话迁移的核心流程可用。欢迎在 issue 中反馈真实跨设备工作流里的问题。

## License

MIT. See `LICENSE`.
