# Codex Sync

[English](README.en.md)

Codex Sync 是一个面向 Windows 的桌面应用，用来在多台电脑之间接续 Codex 工作现场：同步项目进度、备份项目快照、迁移/合并 Codex 对话，并把 Windows 与 WSL 的 Codex 环境放在同一个界面里管理。

它的目标不是再提供一堆命令行脚本，而是让你打开一个 EXE 控制台就能看到当前机器、WSL、项目和远端服务器的状态，并完成备份、恢复、导入和接力。

## 你可以用它做什么

- **跨设备接续 Codex 对话**：把一台机器上的完整 Codex 对话备份到服务器，在另一台机器上选择目标环境和渠道后导入继续。
- **同步项目进度**：手动选择项目目录，Git 项目上传补丁快照，非 Git 项目按安全规则打包文件。
- **统一管理 Windows 与 WSL**：Windows Codex Home 和不同 WSL 发行版都作为一等环境显示，可分别备份、恢复和导入对话。
- **自建服务器备份**：项目和对话备份上传到你自己的同步服务器，不依赖 GitHub 备份分支。
- **冲突保护**：完整备份采用 append-only 分支模型，远端状态变化时自动保留分支，避免覆盖旧对话链路。
- **灾难恢复**：执行导入、恢复、渠道合并等敏感写入前，会先创建本地灾难备份。

## 桌面端体验

桌面端是主要入口，适合日常使用：

- 首页展示本机、远端和其它设备的同步状态。
- “对话与备份”负责完整对话备份、云端备份列表、备份包导入、对话浏览和渠道合并。
- “项目备份”可以选择任意项目目录上传快照，也可以从云端项目库选择项目和版本恢复到本机目录。
- “系统设置”负责服务器连接、一键部署、备份策略、自动任务和高级诊断。

本地 Web 桌面端只绑定 `127.0.0.1`，每次启动都会生成随机会话 token。不要把它通过反向代理暴露到网络。

## 快速开始

如果你下载的是发布包，双击运行：

```text
CodexSync.exe
```

默认会打开独立的 Codex Sync 桌面窗口。首次点击关闭按钮时，会询问是最小化到托盘继续运行，还是彻底退出并关闭本地服务，并可勾选以后不再提示。重复双击不会启动第二个实例，会尝试切回已有窗口。

发布包可以直接解压运行，也可以在“系统设置 -> 定时任务与 Hooks -> 桌面应用安装”中安装到 `%LOCALAPPDATA%\CodexSync\CodexSync.exe`。安装后会创建桌面/开始菜单快捷方式，并可开启开机自启，避免自启路径绑定到临时解压目录。

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
3. 在“对话与备份”里创建完整对话备份。
4. 在“项目备份”里选择当前项目并上传项目快照。
5. 到另一台机器打开 Codex Sync，列出云端备份，选择目标 Windows/WSL 环境和渠道后导入。

轻量快照只用于记录 cwd、Git 状态、配置摘要和最近事件，是诊断与兜底接力状态；真正的跨设备对话接续以完整对话备份和选择性导入为主。

## 自建同步服务器

Codex Sync 使用一个轻量同步服务器保存快照、项目备份和完整对话备份。你可以在 Linux 服务器上一键安装：

```powershell
python -m codex_sync deploy-config --ssh-target user@your-server
python -m codex_sync deploy-server --install --yes
```

默认部署位置：

- 程序：`/opt/codex-sync-server`
- 数据：`/var/lib/codex-sync`
- Token：`/etc/codex-sync/server-token`
- 监听：`0.0.0.0:8888`

部署成功后，本机客户端会自动写入 `server_url` 和 API token。网页端“系统设置 -> 服务器部署”也可以查看状态、更新部署和填写临时 SSH 密码。

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

当前版本还没有客户端加密。完整对话备份包含真实上下文，所以默认只生成本地包；只有你显式开启 `--allow-plaintext-upload` 或在设置中允许明文上传时，才会上传完整对话包。

项目备份会跳过 `.env`、密钥、证书、`node_modules`、`dist`、`build`、缓存目录和超出大小限制的文件。

## 自动同步与 Hook

安装 Codex hooks 后，本工具会在 Codex 会话开始、用户提交、压缩上下文和停止等事件发生时标记本机状态变化。即使 hook 没有运行，定时扫描也会通过内容 digest 发现对话变化。

Windows 定时任务可用于后台同步轻量状态和生成本地完整备份：

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
