#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EAgent-Bridge 的双击入口。

无参数双击：启动桥接服务 + 用 Edge 独立窗口打开控制面板（图形界面里换站点、登录、自检）。
带参数：透传给 server.py 的 argparse，例如
    EAgentBridge.exe --selftest
    EAgentBridge.exe --serve-only          # 只起服务，不开窗口
    EAgentBridge.exe --site doubao
    EAgentBridge.exe --start-browser
    EAgentBridge.exe --stop-browser

上游：https://github.com/YaoGaoChaoLe/EAgent (GPL-3.0)，本项目为衍生作品，同样 GPL-3.0。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from pathlib import Path

import server


def find_edge() -> str | None:
    candidates = [
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    tails = [
        r"\Microsoft\Edge\Application\msedge.exe",
        r"\Google\Chrome\Application\chrome.exe",
    ]
    for base in candidates:
        for tail in tails:
            p = Path(base + tail) if base else None
            if p and p.exists():
                return str(p)
    return None


def open_panel(url: str) -> bool:
    """用 --app 模式开一个无地址栏的独立窗口，看起来就是原生程序界面。"""
    exe = find_edge()
    if exe:
        profile = Path(tempfile.gettempdir()) / "eabridge_panel_profile"
        try:
            subprocess.Popen([
                exe,
                f"--app={url}",
                "--window-size=1000,840",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile}",
            ], close_fds=True)
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def run_gui() -> None:
    srv = server.make_server()
    server.serve_forever_background(srv)
    time.sleep(0.4)

    host = server.CONFIG.get("listen_host", "127.0.0.1")
    port = int(server.CONFIG.get("listen_port", 8300))
    url = f"http://{host}:{port}/"
    ok = open_panel(url)
    server.emit("控制面板：" + url + ("（已打开独立窗口）" if ok else "（请手动在浏览器打开）"))
    server.emit("首次使用请点「打开网页 AI 并登录」，登录后在 Harness 里发任务。")
    if not ok:
        try:
            (server.DATA_DIR / "open-panel.txt").write_text(
                f"无法自动打开窗口，请手动访问：{url}\n", encoding="utf-8")
        except OSError:
            pass

    try:
        server.SHUTDOWN_EVENT.wait()
    except KeyboardInterrupt:
        server.emit("已收到停止信号")
    finally:
        server.stop_server(srv)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        server.main()
    else:
        try:
            run_gui()
        except Exception:
            # GUI 打包后没有控制台，出错必须落盘才查得到
            try:
                (server.DATA_DIR / "gui-error.log").write_text(
                    traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
            raise
