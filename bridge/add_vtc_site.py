#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 VTC AI (https://genai.vtc.edu.hk/) 生成站点适配器，并同步注册到各份 sites.json。

VTC 是自建聊天界面，DOM 与 DeepSeek / 豆包 / 元宝 完全不同，
所以这里基于通用候选选择器生成一份可校准的 2_send.ps1：
  - 输入框兼容 textarea / input / contenteditable
  - 助手消息用一组常见 chat UI 类名候选
  - 发送沿用 Enter 键（多数聊天框支持）
选择器集中写在脚本顶部的 $Selectors，实测后可只改这个脚本、重启 exe 即生效，
不需要重新打包（exe 会优先读取同目录的外置 scripts/）。

用法：python add_vtc_site.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WS = ROOT.parent
SRC = ROOT / "scripts" / "deepseek"
DST = ROOT / "scripts" / "vtc"

SITE_ID = "vtc"
SITE = {
    "label": "VTC AI",
    "url": "https://genai.vtc.edu.hk/",
    "url_match": r"genai\.vtc\.edu\.hk",
    "scripts": {
        "1": "scripts/vtc/1_start.ps1",
        "2": "scripts/vtc/2_send.ps1",
        "3": "scripts/vtc/3_close.ps1",
    },
}


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def write_ps1(path: Path, text: str) -> None:
    # PowerShell 5.1 读含中文的脚本需要 UTF-8 BOM；保持 CRLF
    data = text.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + data)


# 已通过 CDP 探测真实 DOM 校准（VTC GenAI 是 Next.js + Tailwind）：
# 用户与助手消息内层都带 prose + text-base，但助手内层没有 whitespace-pre-wrap。
NEW_SELECTORS = """$Selectors = @{
    assistant = @('div[class*="prose"][class*="text-base"]:not([class*="whitespace-pre-wrap"])')
    user      = @('div[class*="prose"][class*="whitespace-pre-wrap"]')
    input     = @('textarea[placeholder="Type a message..."]')
    sendBtn   = @('button[type="submit"]', '[aria-label*="Send"]')
}"""

OLD_SELECTORS = """$Selectors = @{
    assistant = @('.ds-assistant-message-main-content', '[class*="ds-assistant-message-main-content"]')
    user      = @('[class*="fbb737a4"]')
    input     = @('textarea')
    sendBtn   = @('div[role="button"].ds-button--primary.ds-button--circle', '[role="button"][aria-label*="发送"]', '[role="button"][aria-label*="Send"]')
}"""

NEW_INJECT = """$injectJs = $SelJs + @"
(function(){
    var msg = $msgJson;
    var el = pick('input')[0];
    if(!el) return JSON.stringify({ok:false, err:'no input'});
    el.focus();
    var tag = (el.tagName||'').toUpperCase();
    if(tag === 'TEXTAREA' || tag === 'INPUT'){
        var proto = tag === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto,'value').set.call(el, msg);
        el.dispatchEvent(new Event('input',{bubbles:true}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        return JSON.stringify({ok:true, len:(el.value||'').length});
    }
    if(el.isContentEditable){
        el.innerText = msg;
        el.dispatchEvent(new InputEvent('input',{bubbles:true, inputType:'insertText', data:msg}));
        return JSON.stringify({ok:true, len:(el.innerText||'').length});
    }
    return JSON.stringify({ok:false, err:'unsupported input'});
})()
"@"""

OLD_INJECT = """$injectJs = $SelJs + @"
(function(){
    var msg = $msgJson;
    var ta = pick('input')[0];
    if(!ta) return JSON.stringify({ok:false, err:'no textarea'});
    ta.focus();
    var s = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    s.call(ta, msg);
    ta.dispatchEvent(new Event('input',{bubbles:true}));
    ta.dispatchEvent(new Event('change',{bubbles:true}));
    return JSON.stringify({ok:true, len:ta.value.length});
})()
"@"""


def main() -> None:
    if not (SRC / "2_send.ps1").exists():
        raise SystemExit("缺少 deepseek 适配器，请先运行 python extract_eagent.py")
    DST.mkdir(parents=True, exist_ok=True)

    start = read_text(SRC / "1_start.ps1")
    start = start.replace('$Url = "https://chat.deepseek.com"',
                          '$Url = "https://genai.vtc.edu.hk/"')
    start = start.replace("deepseek_debug_profile", "vtc_debug_profile")
    write_ps1(DST / "1_start.ps1", start)

    write_ps1(DST / "3_close.ps1", read_text(SRC / "3_close.ps1"))

    send = read_text(SRC / "2_send.ps1")
    send = send.replace(r"chat\.deepseek\.com", r"genai\.vtc\.edu\.hk")
    send = send.replace("$FastFailSec = 15", "$FastFailSec = 150")
    for old, new, label in (
        (OLD_SELECTORS, NEW_SELECTORS, "selectors"),
        (OLD_INJECT, NEW_INJECT, "inject script"),
    ):
        if old not in send:
            raise SystemExit(f"上游脚本结构与预期不符，无法替换 {label}；请人工校对")
        send = send.replace(old, new)
    write_ps1(DST / "2_send.ps1", send)

    targets = [ROOT / "sites.json", WS / "sites.json", ROOT / "dist" / "sites.json"]
    for path in targets:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("sites", {})[SITE_ID] = SITE
        data["note"] = (
            "四个站点共用同一个 CDP 端口，同一时间只能运行一个站点；"
            "切换站点需先关闭浏览器。VTC AI 使用独立的 vtc_debug_profile 登录态。"
        )
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("registered:", path)

    for path in (ROOT / "config.json", WS / "config.json", ROOT / "dist" / "config.json"):
        if not path.exists():
            continue
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["_comment_site"] = "可选 deepseek / doubao / yuanbao / vtc，共用 CDP 端口，同一时间只能启用一个"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for name in ("1_start.ps1", "2_send.ps1", "3_close.ps1"):
        p = DST / name
        print(f"  generated {p.relative_to(WS)} ({p.stat().st_size} bytes)")
    print("\nVTC AI 已注册。改选择器只需编辑 scripts/vtc/2_send.ps1 顶部的 $Selectors 后重启 exe。")


if __name__ == "__main__":
    main()
