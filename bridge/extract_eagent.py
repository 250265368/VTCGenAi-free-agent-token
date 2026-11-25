#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 EAgent 上游仓库抽取 CDP PowerShell 脚本层，变成可独立运行的 .ps1。

上游：https://github.com/YaoGaoChaoLe/EAgent  (GPL-3.0)
上游源码以只读方式保存在 ../upstream/EAgent，本脚本不修改它。
本文件属于 EAgent 的衍生作品，同样遵循 GPL-3.0。

用法：python extract_eagent.py
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM_RES = ROOT.parent / "upstream" / "EAgent" / "Code" / "resource"
UPSTREAM_REPO = ROOT.parent / "upstream" / "EAgent"
OUT_DIR = ROOT / "scripts"
SITES_FILE = ROOT / "sites.json"

# upstream 资源名前缀 -> 本项目站点 id
SITES = {
    "deepseek": {
        "prefix": "Ds",
        "label": "DeepSeek 网页版",
        "url": "https://chat.deepseek.com",
        "url_match": r"chat\.deepseek\.com",
    },
    "doubao": {
        "prefix": "豆包",
        "label": "豆包网页版",
        "url": "https://www.doubao.com/chat/",
        "url_match": r"doubao\.com",
    },
    "yuanbao": {
        "prefix": "元宝",
        "label": "腾讯元宝网页版",
        "url": "https://yuanbao.tencent.com/chat/naQivTmsDa",
        "url_match": r"yuanbao\.tencent\.com",
    },
}

# upstream 资源后缀 -> 本项目脚本名
PARTS = {
    "ps1": "1_start.ps1",   # 启动带调试端口的浏览器
    "ps2": "2_send.ps1",    # 注入消息 + 轮询等待回答 + 写出 reply.txt
    "ps3": "3_close.ps1",   # 关闭浏览器
}


def decode_source(raw: bytes) -> str:
    """EAgent 资源是 UTF-8（部分带 BOM）明文 PowerShell，少数常量是 GBK。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def upstream_revision() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(UPSTREAM_REPO), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=10,
        )
        return out.decode("ascii", "replace").strip()
    except Exception:
        return "unknown"


def main() -> None:
    if not UPSTREAM_RES.is_dir():
        raise SystemExit(
            f"找不到上游资源目录：{UPSTREAM_RES}\n"
            "请先执行：git clone --depth 1 https://github.com/YaoGaoChaoLe/EAgent.git ../upstream/EAgent"
        )

    # 保留手工适配的站点（如 vtc），重新抽取上游脚本时不会被覆盖丢失
    sites_cfg: dict = {}
    if SITES_FILE.exists():
        try:
            prev = json.loads(SITES_FILE.read_text(encoding="utf-8")).get("sites", {})
            sites_cfg.update({k: v for k, v in prev.items() if k not in SITES})
        except (ValueError, OSError):
            pass
    for site_id, meta in SITES.items():
        site_dir = OUT_DIR / site_id
        site_dir.mkdir(parents=True, exist_ok=True)
        scripts: dict = {}
        for suffix, filename in PARTS.items():
            src = UPSTREAM_RES / f"{meta['prefix']}_{suffix}.image.dat"
            if not src.exists():
                raise SystemExit(f"缺少上游资源：{src}")
            text = decode_source(src.read_bytes())
            # 统一成 UTF-8 BOM：Windows PowerShell 5.1 读取含中文脚本时的安全做法
            dst = site_dir / filename
            dst.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
            scripts[filename.split("_")[0]] = f"scripts/{site_id}/{filename}"
            print(f"  {src.name:<28} -> {dst.relative_to(ROOT)}  ({len(text)} chars)")
        sites_cfg[site_id] = {
            "label": meta["label"],
            "url": meta["url"],
            "url_match": meta["url_match"],
            "scripts": scripts,
        }

    payload = {
        "generated_from": f"YaoGaoChaoLe/EAgent @ {upstream_revision()}",
        "license": "GPL-3.0",
        "note": "三个站点共用同一个 CDP 端口与 user-data-dir，同一时间只能运行一个站点；切换站点需先关闭浏览器。",
        "cdp_port": 9222,
        "sites": sites_cfg,
    }
    SITES_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已生成 {SITES_FILE.relative_to(ROOT)}，共 {len(sites_cfg)} 个站点、{len(sites_cfg) * len(PARTS)} 个脚本。")
    print("提示：脚本内的 CSS 选择器来自上游，网页改版后需同步更新上游并重新抽取。")


if __name__ == "__main__":
    main()
