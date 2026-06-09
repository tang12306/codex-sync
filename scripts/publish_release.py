"""一次性发布脚本：用本地 GitHub 凭证调 REST API 创建/更新 Release 并上传安装包。

token 不写在脚本里，仅从环境变量读取（CODEX_GH_TOKEN / GH_TOKEN / GITHUB_TOKEN）。
用法（见同目录调用）：
    CODEX_GH_TOKEN=<token> python scripts/publish_release.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "tang12306"
REPO = "codex-sync"
TAG = "v0.4.0"
NAME = "Codex Sync v0.4.0"
ROOT = Path(__file__).resolve().parent.parent
ASSET = ROOT / "dist" / "CodexSyncSetup-v0.4.0-windows-x64.exe"
SHA = ROOT / "dist" / "CodexSyncSetup-v0.4.0-windows-x64.exe.sha256"

NOTES = """# Codex Sync v0.4.0

本次主题是「精简瘦身」：移除已无意义的「云端轻量接力快照」，并大幅简化界面，让常用操作一眼看懂、好用。

## 🧹 彻底移除云端轻量接力快照
- 前后端整体移除「轻量快照」（记录 cwd / git / 配置元数据的诊断快照）及其上传管线、服务器存储、还原/接续抽屉。
- **完整对话备份、项目备份完全不受影响**，仍可正常备份/恢复。
- 同步服务器 API 升级到 v5（features 不再含 `snapshots`）。

## ✨ 界面精简
- 删除四个页面底部冗余的「输出详情 / 原始详情」大方框，操作成功/失败统一由右上角提示反馈。
- 「备份与恢复 → 云端」标签只保留云端完整备份列表；移除快照表与详情/接续/还原抽屉。
- 修复「恢复目标目录」单行输入框被错误撑成大方框的样式问题。
- 移除项目页低密度的「备份内容 / 安全边界」说明卡，清理大量死代码（约 -2500 行）。

## 🔧 其它（自上个版本累积）
- 项目自动备份统一为完整快照、支持「一键恢复最新完整版」、项目实时备份。
- 「立即同步」语义更新为：扫描并上传完整对话备份 + 处理项目自动备份队列。

## 📦 安装
下载 `CodexSyncSetup-v0.4.0-windows-x64.exe`，双击按向导安装即可。无需管理员权限（安装到当前用户目录）。

## ⚠️ 升级提示
服务器端需重新部署到 v5（在「系统设置 → 服务器与部署 → 一键更新部署」即可推送）。SHA256 见随附的 `.sha256` 文件。
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
