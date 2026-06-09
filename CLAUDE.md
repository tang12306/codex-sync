# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Codex Sync 是一个 Windows 桌面应用，用于在多台电脑之间同步 Codex (Claude Code) 工作现场。核心能力：跨设备备份/恢复 Codex 对话、项目代码快照、渠道合并/迁移、Windows 与 WSL 环境统一管理。

## 常用命令

```powershell
# 开发运行桌面端
python -m codex_sync desktop

# 在浏览器中打开（不启动桌面窗口）
python -m codex_sync desktop --browser

# 指定端口、不自动打开
python -m codex_sync desktop --no-open --port 8765

# 启动本地同步服务器（开发/测试用）
python -m codex_sync.sync_server

# CLI 各种操作
python -m codex_sync config                          # 查看/修改配置
python -m codex_sync sync-now                        # 立即同步一次
python -m codex_sync full-backup-now                 # 创建完整对话备份
python -m codex_sync full-backup-now --upload --allow-plaintext-upload
python -m codex_sync list-full-backups               # 列出云端完整备份
python -m codex_sync project-backup                  # 备份当前项目
python -m codex_sync wsl-status                      # 检测 WSL 环境
python -m codex_sync install-task --minutes 3         # 安装 Windows 定时任务
python -m codex_sync install-hooks                   # 安装 Codex hooks
python -m codex_sync conversations --search <关键词>  # 搜索本机对话
python -m codex_sync conversation-show <id>           # 查看对话内容
python -m codex_sync conversation-export <id>         # 导出对话为 Markdown
```

## 运行测试

```powershell
# 运行所有测试
python -m pytest tests/ -v

# 运行单个测试文件
python -m pytest tests/test_config.py -v

# 运行单个测试函数
python -m pytest tests/test_config.py::test_load_config -v
```

## 构建发布

```powershell
# Windows EXE 打包（PyInstaller）
powershell -File scripts/build_windows_release.ps1 -Version 0.2.0
```

打包入口是 `app_entry.py`，产物为 `dist/CodexSyncSetup-vX.Y.Z-windows-x64.exe`。

## 架构概览

### 入口层次

```
app_entry.py          ← PyInstaller EXE 入口，双击自动安装到 %LOCALAPPDATA%\CodexSync
codex_sync/cli.py     ← CLI 入口，argparse 子命令 → 各模块函数
codex_sync/sync_server.py  ← 独立同步服务器（SQLite + HTTP），部署到 Linux
codex_sync/__init__.py     ← 版本号 __version__ = "0.2.0"
```

### 桌面端架构 (web_desktop.py)

桌面端是一个本地 HTTP 服务 + WebView 窗口的组合：

- **`DesktopRuntime`**: 单例运行时，管理 session token（随机生成）、日志缓冲、后台 daemon 线程、所有 action 的路由分发（`run_action` 方法，约 60+ 个 action）。
- **`LocalDesktopServer`**: 包装 `ThreadingHTTPServer`，只绑定 `127.0.0.1`，每次启动随机 session token（通过 `X-Codex-Sync-Desktop-Token` header 和 HTML meta 注入双重校验）。
- **API 路由**: `GET /api/status`（状态查询）、`POST /api/config`（配置保存）、`POST /api/action`（执行命名 action，payload 中包含 `name` 和参数）。
- **前端 SPA**: `codex_sync/web/` 目录，纯 vanilla JS，无框架依赖。
  - `js/main.js` — 入口，初始化 store、注册路由、启动轮询
  - `js/store.js` — 轻量响应式 store（`getState`/`setState`/`subscribe`/`select`）
  - `js/router.js` — hash 路由
  - `js/api.js` — HTTP API 封装
  - `js/views/` — 各页面视图（overview、conversations、project、backups、settings、logs、drawer）
- **系统托盘**: `WindowsTrayIcon` 类，原生 Win32 API 实现（`Shell_NotifyIconW`），支持双击还原、右键菜单（显示/退出）。
- **单实例**: `SingleInstance` 类，Windows Named Mutex，防止重复启动。
- **关闭行为**: `desktop_close_behavior` 配置（ask/minimize_to_tray/exit），首次关闭弹出原生 MessageBox 询问。

### 核心模块职责

| 模块 | 职责 |
|------|------|
| `config.py` | `AppConfig` dataclass，JSON 持久化到 `~/.codex-sync/config.json` |
| `server.py` | 客户端 HTTP 通信层：完整/项目备份的服务器通信、服务器兼容性检查与留存策略调用。期望 `SERVER_API_VERSION = 5` |
| `collector.py` | Codex hook 事件捕获（`capture_event`）与 Codex 配置状态收集（`collect_codex_state`） |
| `full_backup.py` | 完整对话备份：扫描 `~/.codex/` 变更、打包 ZIP、上传/下载/恢复。有安全排除列表（auth.json、.env、密钥等） |
| `git_backup.py` | Git 项目补丁快照和备份 |
| `codex_channels.py` | Codex 对话渠道合并/还原（model_provider 级别），操作 `~/.codex/` 下的 SQLite 数据库 |
| `conversations.py` | 本机 Codex 对话列表/读取/导出（Markdown/JSON） |
| `backup_import.py` | 从备份包导入对话到本机渠道 |
| `wsl.py` | WSL 发行版检测、WSL 内 Codex 对话备份/恢复/导入、渠道查询 |
| `deploy.py` | SSH 远程部署同步服务器（systemd + nginx 可选） |
| `hooks.py` | 全局 Codex hooks 安装/状态查询 |
| `daemon.py` | 前台同步循环（定时 full-backup 扫描上传 + 项目自动备份队列处理） |
| `windows_task.py` | Windows Task Scheduler 定时任务管理 |
| `disaster_backup.py` | 敏感写入前的本地灾难备份 |
| `paths.py` | 路径常量：`~/.codex-sync/`（本应用数据）、`~/.codex/`（Codex 数据） |
| `project_auto_backup.py` | 项目自动备份队列、Git post-commit hook 集成 |
| `app_install.py` | EXE 安装到 `%LOCALAPPDATA%\CodexSync`、快捷方式、开机自启 |
| `app_update.py` | 应用更新检查/下载 |

### 数据流

```
Codex hooks 事件 → capture_event() → notify_codex_changed() → full_backup 扫描/上传
                                                            ↘ (Stop 事件) 项目自动备份入队
daemon / sync-now → scan_full_backup_changes(upload) + process_project_auto_backup_queue
```

### 配置键位

所有配置通过 `AppConfig` dataclass 管理，关键字段：
- `server_url` / `api_token` — 同步服务器连接
- `device_id` — 自动生成 `{hostname}-{uuid[:8]}`
- `full_backup_enabled` — 是否启用完整对话备份
- `full_backup_allow_plaintext_upload` — 是否允许明文上传完整对话包（默认关闭）
- `desktop_close_behavior` — 关闭行为：`ask` | `minimize_to_tray` | `exit`
- `project_auto_backup_on_codex_stop` — Codex 停止时自动备份项目
- 配置路径可通过 `CODEX_SYNC_HOME` 环境变量覆盖；Codex 数据路径可通过 `CODEX_HOME` 覆盖

### 前端状态管理约定

- store 是唯一的全局状态容器
- `setState` 浅合并顶层 key，未变化的片段必须保持引用稳定
- `select(selector, fn)` 按 `Object.is` 去重，避免无关刷新
- 视图通过 `mount(viewRoot, store)` 挂载，返回可选的 `unmount` 清理函数
- 所有后端交互通过 `runAction(name, payload)` 统一调用 `/api/action`

### 版本管理

- 版本号定义在 `codex_sync/__init__.py` 的 `__version__`
- `pyproject.toml` 的 `version` 字段需同步
- 构建脚本 `scripts/build_windows_release.ps1` 接受 `-Version` 参数
