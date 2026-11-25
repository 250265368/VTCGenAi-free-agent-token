#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证「增量发送」：第一轮带精简背景，第二轮起只把新增消息发给网页 AI。

用法：python verify_incremental.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol  # noqa: E402
import server  # noqa: E402


def main() -> None:
    print("protocol selftest:")
    protocol.selftest()
    print()
    print("=== 两轮增量模拟 ===")

    tools = [{
        "type": "function",
        "function": {
            "name": "shell",
            "description": "run a command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                           "required": ["command"]},
        },
    }]

    turn1 = [
        {"role": "system", "content": "You are an AI agent powered by DeepSeek Harness. " * 30},
        {"role": "user", "content": "hi"},
    ]
    turn2 = turn1 + [
        {"role": "assistant", "content": "hello there"},
        {"role": "tool", "name": "shell", "content": "desktop-path-ok"},
    ]

    new1, delta1 = server.TRACKER.delta("vtc", turn1)
    p1 = protocol.flatten_messages(turn1, tools, 24000, background_chars=700)
    new2, delta2 = server.TRACKER.delta("vtc", turn2)
    p2 = protocol.flatten_delta(delta2, 24000)

    print(f"第 1 轮: 新会话={new1} | 发送 {len(p1)} 字（含精简背景）")
    print(f"第 2 轮: 新会话={new2} | 新增 {len(delta2)} 条消息 | 只发送 {len(p2)} 字")
    print(f"  内容: {p2!r}")

    assert new1 is True, "第一轮应识别为新会话"
    assert new2 is False, "第二轮应命中已发送前缀"
    assert len(p1) < 6000, f"首轮背景过长: {len(p1)}"
    assert len(p2) < 100 and "desktop-path-ok" in p2, "第二轮应只发新增内容"
    assert "DeepSeek Harness" not in p2, "第二轮不应重发 system 背景"

    server.TRACKER.reset("vtc")
    print("\nPASS: 第二轮不再重发全量背景")

    print("\n=== 首轮体积 / 标题短路 / VTC 选择器 ===")
    big_tools = [{
        "type": "function",
        "function": {
            "name": f"tool_{i}",
            "description": "Search and fetch data from the web. " * 8,
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"},
                                          "limit": {"type": "integer"}},
                           "required": ["query"]},
        },
    } for i in range(50)]
    noisy = [
        {"role": "system", "content": "You are an AI agent powered by DeepSeek Harness. " * 100},
        {"role": "user", "content": "<system-reminder> A skill is a reusable set of instructions. " * 30},
        {"role": "user", "content": "hi"},
    ]
    first = protocol.flatten_messages(noisy, big_tools, 8000, background_chars=0)
    print(f"50 工具 + 长 system 的首轮: {len(first)} 字")
    assert len(first) < 5000, len(first)
    assert "DeepSeek Harness" not in first and "system-reminder" not in first
    print("  [PASS] Harness 内部背景与 skill 说明已被剔除")

    title_payload = {
        "model": "web-ai",
        "messages": [
            {"role": "system", "content": "Create a concise title for an AI coding-assistant session."},
            {"role": "user", "content":
                "Generate the session title from this JSON array of human messages: "
                '[{"seq":9,"text":"做一个鹈鹕骑自行车，需要可以调节车灯"}]'},
        ],
    }
    assert server._is_title_request(title_payload)
    title = server._local_title(title_payload)["choices"][0]["message"]["content"]
    print(f"  [PASS] 标题请求本地短路: {title!r}")

    probe_file = Path(__file__).with_name("dom_probe.json")
    if probe_file.exists():
        probe = json.loads(probe_file.read_text(encoding="utf-8"))
        hits = [c for c in probe["candidates"]
                if "prose" in c["cls"] and "text-base" in c["cls"]
                and "whitespace-pre-wrap" not in c["cls"]]
        print("  VTC 助手选择器命中:", [(h["len"], h["head"][:24]) for h in hits])
        assert any("鹈鹕" in h["head"] for h in hits), "校准后的选择器没抓到助手回答"
        print("  [PASS] 助手选择器能定位真实回答，而非用户消息")


if __name__ == "__main__":
    main()
