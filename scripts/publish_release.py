"""一次性发布脚本：用本地 GitHub 凭证调 REST API 创建/更新 Release 并上传安装包。

token 不写在脚本里，仅从环境变量读取（CODEX_GH_TOKEN / GH_TOKEN / GITHUB_TOKEN）。
用法（见同目录调用）：
    CODEX_GH_TOKEN=<token> python scripts/publish_release.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "tang12306"
REPO = "codex-sync"
ROOT = Path(__file__).resolve().parent.parent
# 版本号跟随 codex_sync/__init__.py，发布前先改版本、跑 build_windows_release.ps1
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "codex_sync" / "__init__.py").read_text(encoding="utf-8")).group(1)
TAG = f"v{VERSION}"
NAME = f"Codex Sync v{VERSION}"
ASSET = ROOT / "dist" / f"CodexSyncSetup-v{VERSION}-windows-x64.exe"
SHA = ROOT / "dist" / f"CodexSyncSetup-v{VERSION}-windows-x64.exe.sha256"

NOTES = rf"""# Codex Sync v{VERSION}

本次主题是「正规安装程序 + 界面全面改版」：安装、升级、卸载都和常见的 Windows 软件一样了，几个主要页面也重新排版。

## 📦 全新安装程序
- 换成标准的 Windows 安装向导：欢迎页 → 许可协议 → 选择安装目录 → 附加任务（桌面快捷方式 / 开机自启）→ 安装 → 完成。
- 默认按当前用户安装到 `%LOCALAPPDATA%\Programs\CodexSync`，无需管理员权限。
- 在「设置 → 应用 → 已安装的应用」里可以看到 Codex Sync 并正常卸载。卸载会撤掉指向本程序的 Codex hooks 和定时任务，`~/.codex-sync` 里的配置和备份记录会保留。
- 安装 / 卸载前会检测正在运行的 Codex Sync 并提示关闭。
- 程序文件带版本信息（右键 → 属性 → 详细信息）。

## ✨ 界面改版
- 项目备份、对话、备份与恢复、系统设置页面重新排版；页内二级页签，刷新后停留在原来的页签。
- 系统设置底部固定保存栏，支持 Ctrl+S；有未保存的修改时离开页面会先确认。
- 统一的确认对话框，浅色 / 深色主题可切换。
- 修复界面上出现「nullnull」等显示问题。

## 🔄 应用内更新
- 「系统设置 → 安装与自动化」下载新版后，可以直接点「立即安装」原地升级。

## 🔧 修复
- 修复 Windows 定时任务每次运行都会失败的问题；打包版的定时任务改为直接调用程序本身。
- 对话很多时会提示列表已截断；日志里的敏感字段会脱敏。

## ⬆️ 从 0.4.0 升级
下载 `CodexSyncSetup-v{VERSION}-windows-x64.exe` 双击安装即可。安装程序会默认装回原来的目录并接管旧安装（旧的「卸载 Codex Sync」快捷方式会被清理）；如果旧版正在运行，会先提示关闭。
服务器端无需更新（同步服务器 API 仍为 v5）。

## ⚠️ 提示
安装包暂未做代码签名，Windows 可能提示「Windows 已保护你的电脑」，点「更多信息 → 仍要运行」即可；开启了「智能应用控制」的电脑可能会直接拦截。SHA256 见随附的 `.sha256` 文件。
"""

TOKEN = os.environ.get("CODEX_GH_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com"


def req(url: str, data: bytes | None = None, method: str | None = None, ctype: str = "application/json"):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "codex-sync-release",
    }
    if ctype and data is not None:
        headers["Content-Type"] = ctype
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return exc.code, parsed


def main() -> int:
    if not TOKEN:
        print("NO_TOKEN: 环境变量未提供 GitHub token")
        return 1
    if not ASSET.exists():
        print(f"ASSET_MISSING: {ASSET}")
        return 3

    # 1) 找已有 release，否则创建
    status, rel = req(f"{API}/repos/{OWNER}/{REPO}/releases/tags/{TAG}")
    if status == 200 and rel.get("id"):
        print(f"release exists: {rel.get('html_url')}")
    else:
        status, rel = req(
            f"{API}/repos/{OWNER}/{REPO}/releases",
            data=json.dumps({"tag_name": TAG, "name": NAME, "body": NOTES, "draft": False, "prerelease": False}).encode("utf-8"),
            method="POST",
        )
        if status not in (200, 201) or not rel.get("id"):
            print(f"CREATE_FAILED {status}: {rel}")
            return 2
        print(f"release created: {rel.get('html_url')}")

    rel_id = rel["id"]
    upload_base = rel["upload_url"].split("{")[0]

    # 2) 删除同名旧 asset（支持重跑）
    for asset in rel.get("assets", []) or []:
        if asset.get("name") in (ASSET.name, SHA.name):
            req(f"{API}/repos/{OWNER}/{REPO}/releases/assets/{asset['id']}", method="DELETE", ctype="")
            print(f"removed old asset: {asset.get('name')}")

    # 3) 上传 exe
    status, asset = req(f"{upload_base}?name={ASSET.name}", data=ASSET.read_bytes(), method="POST", ctype="application/octet-stream")
    if status not in (200, 201):
        print(f"UPLOAD_FAILED {status}: {asset}")
        return 4
    print(f"asset uploaded: {asset.get('browser_download_url')}  ({asset.get('size')} bytes)")

    # 4) 上传 sha256（可选）
    if SHA.exists():
        status, sha_asset = req(f"{upload_base}?name={SHA.name}", data=SHA.read_bytes(), method="POST", ctype="text/plain")
        if status in (200, 201):
            print(f"sha uploaded: {sha_asset.get('browser_download_url')}")
        else:
            print(f"sha upload skipped ({status})")

    print(f"DONE: {rel.get('html_url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
