#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过 CDP 只读探测网页 AI 页面的 DOM 结构，用于校准站点适配器的 CSS 选择器。

用法：python probe_dom.py [url关键字，默认 vtc]
前提：目标页面已在 9222 调试浏览器中打开。全过程只读，不点击、不发消息。
完整结果保存到 dom_probe.json。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import websocket

PROBE_JS = r"""
(function(){
  function d(el){
    if(!el) return null;
    var t=(el.innerText||el.textContent||'').trim();
    return {
      tag: el.tagName,
      cls: (el.className||'').toString().slice(0,160),
      id: el.id||'',
      testid: el.getAttribute('data-testid')||'',
      role: el.getAttribute('role')||'',
      aria: el.getAttribute('aria-label')||'',
      ph: el.getAttribute('placeholder')||'',
      type: el.getAttribute('type')||'',
      len: t.length,
      head: t.slice(0,100),
      rect: (function(){var r=el.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]})()
    };
  }
  var out={url:location.href,title:document.title,inputs:[],buttons:[],candidates:[],bigBlocks:[]};
  document.querySelectorAll('textarea,[contenteditable="true"],input[type="text"]').forEach(function(e){out.inputs.push(d(e))});
  document.querySelectorAll('button,[role="button"]').forEach(function(e){out.buttons.push(d(e))});
  var sel='[class*="message"],[class*="chat"],[class*="bubble"],[class*="markdown"],[class*="prose"],[class*="assistant"],[class*="response"],[class*="content"],[data-testid]';
  document.querySelectorAll(sel).forEach(function(e){
    var x=d(e);
    if(x && x.len>0) out.candidates.push(x);
  });
  document.querySelectorAll('div,p,section,article').forEach(function(e){
    var t=(e.innerText||'').trim();
    if(t.length>250){
      out.bigBlocks.push({
        self:d(e),
        parent:d(e.parentElement),
        siblings:(e.parentElement?Array.from(e.parentElement.children):[]).slice(0,6).map(d)
      });
    }
  });
  return JSON.stringify(out);
})()
"""


def main() -> None:
    match = sys.argv[1] if len(sys.argv) > 1 else "vtc"
    pages = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:9222/json", timeout=5).read().decode("utf-8"))
    page = next((p for p in pages
                 if p.get("type") == "page" and match in p.get("url", "")), None)
    if not page:
        print("没有匹配的页面：", [p.get("url") for p in pages])
        return

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)
    ws.send(json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": PROBE_JS, "returnByValue": True},
    }))
    value = None
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            value = msg.get("result", {}).get("result", {}).get("value")
            break
    ws.close()
    data = json.loads(value)

    Path(__file__).with_name("dom_probe.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("URL:", data["url"], "| title:", data["title"])
    print("\n=== INPUTS ===")
    for x in data["inputs"]:
        print(" ", json.dumps(x, ensure_ascii=False))
    print("\n=== BUTTONS (last 12) ===")
    for x in data["buttons"][-12:]:
        print(" ", json.dumps(x, ensure_ascii=False))
    print("\n=== CANDIDATES (last 22) ===")
    for x in data["candidates"][-22:]:
        print(" ", json.dumps(x, ensure_ascii=False))
    print("\n=== BIG BLOCKS (first 4) ===")
    for b in data["bigBlocks"][:4]:
        print(" self  :", json.dumps(b["self"], ensure_ascii=False))
        print(" parent:", json.dumps(b["parent"], ensure_ascii=False))
        for s in b["siblings"]:
            print("   sib :", json.dumps(s, ensure_ascii=False))
    print("\n完整结果已保存 dom_probe.json")


if __name__ == "__main__":
    main()
