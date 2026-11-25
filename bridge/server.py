#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EAgent-Bridge：把 EAgent 的 CDP 网页驱动包装成 OpenAI 兼容 endpoint，
并提供一个本地控制面板（GET /）用于在图形界面里切换站点、启停浏览器、看日志。

    Harness  --(OpenAI /v1/chat/completions)-->  本服务
             <-- (SSE / JSON, tool_calls) ------
    本服务  --(PowerShell + CDP)-->  Edge/Chrome 里已登录的网页 AI
    控制面板 --(浏览器窗口)--------->  http://127.0.0.1:8300/

只依赖 Python 标准库。网页收发逻辑完全来自 EAgent 上游的 2_send.ps1。

上游：https://github.com/YaoGaoChaoLe/EAgent (GPL-3.0)，本项目为衍生作品，同样 GPL-3.0。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import protocol


def _setup_console() -> None:
    """Windows 下 stdout 被重定向到管道时会退化成 cp950/cp936，中文 print 会崩；
    --windowed 打包时 sys.stdout/stderr 为 None，必须跳过。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


_setup_console()


def _resource_root() -> Path:
    """打包后：exe 旁备齐 config.json / sites.json / scripts 时优先用外置资源，
    否则用 PyInstaller 解出的内置资源，保证单文件也能独立跑。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if all((exe_dir / x).exists() for x in ("config.json", "sites.json", "scripts")):
            return exe_dir
        return Path(getattr(sys, "_MEIPASS", str(exe_dir)))
    return Path(__file__).resolve().parent


ROOT = _resource_root()
# 日志与用户配置永远落在 exe 旁，避免写进 onefile 临时目录后随退出丢失
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT


def config_path() -> Path:
    """配置逐文件回退：exe 旁有 config.json 就用它（面板改站点写这里），否则用内置那份。"""
    if getattr(sys, "frozen", False):
        ext = Path(sys.executable).resolve().parent / "config.json"
        if ext.exists():
            return ext
    return ROOT / "config.json"


CONFIG = json.loads(config_path().read_text(encoding="utf-8"))
SITES = json.loads((ROOT / "sites.json").read_text(encoding="utf-8"))

SHUTDOWN_EVENT = threading.Event()
EVENTS: deque = deque(maxlen=200)


class ConversationTracker:
    """网页端是有状态会话，Harness 是无状态 API（每轮重发全量 messages）。
    这里记住上轮已发送的消息前缀，后续只发新增部分，避免整包重复注入输入框。"""

    def __init__(self):
        self.prefixes: dict[str, list[str]] = {}

    @staticmethod
    def _norm(messages: list[dict]) -> list[str]:
        out = []
        for m in messages or []:
            role = m.get("role") or "user"
            c = m.get("content")
            s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True, default=str)
            tc = json.dumps(m.get("tool_calls") or [], ensure_ascii=False, sort_keys=True, default=str)
            key = f"{role}\x00{s}\x00{tc}\x00{m.get('name', '')}"
            out.append(hashlib.sha1(key.encode("utf-8", "replace")).hexdigest())
        return out

    def delta(self, site: str, messages: list[dict]) -> tuple[bool, list[dict]]:
        cur = self._norm(messages)
        prev = self.prefixes.get(site, [])
        k = 0
        while k < min(len(cur), len(prev)) and cur[k] == prev[k]:
            k += 1
        self.prefixes[site] = cur
        return (k == 0), messages[k:]

    def reset(self, site: str | None = None) -> None:
        if site:
            self.prefixes.pop(site, None)
        else:
            self.prefixes.clear()


TRACKER = ConversationTracker()
# 每站点本会话累计注入网页的字符数，用于面板提示何时该开新对话
SESSION_CHARS: dict[str, int] = {}


def emit(msg: str) -> None:
    EVENTS.append({"t": time.strftime("%H:%M:%S"), "msg": str(msg)})
    try:
        print(msg, flush=True)
    except Exception:
        pass


class BridgeError(Exception):
    pass


# 网页端额度耗尽 / 拒绝服务时立即报错停下，绝不继续转发让 Harness 重试烧轮次
QUOTA_MARKERS = ("monthly token quota", "has been used up", "额度已用完", "配额不足", "quota exceeded")

# 模型偶尔编造工具名（如 create_file）。能映射到 Harness 真实工具的就改名，
# 映射不了的就丢弃、把原文当普通回答交回，避免 unknown tool 报错→重试→烧额度死循环。
TOOL_ALIASES = {
    "create_file": "write", "write_file": "write", "create": "write",
    "new_file": "write", "save_file": "write", "writefile": "write",
    "edit_file": "edit", "modify_file": "edit", "update_file": "edit",
    "read_file": "read", "open_file": "read", "cat": "read",
    "shell": "pwsh", "powershell": "pwsh", "run": "pwsh", "execute": "pwsh",
    "run_command": "pwsh", "bash": "pwsh", "terminal": "pwsh",
    "list_files": "glob", "find_files": "glob", "search_files": "grep",
}


def _reconcile_tool_names(tool_calls: list[dict], payload: dict) -> tuple[list[dict], bool]:
    """按 payload.tools 的真实工具名校验/别名映射；返回(修正后的调用, 是否丢弃过)。"""
    allowed = {(t.get("function") or {}).get("name")
               for t in (payload.get("tools") or []) if t.get("type") == "function"}
    allowed.discard(None)
    if not allowed:
        return tool_calls, False
    kept, dropped = [], False
    for c in tool_calls:
        fn = c.get("function") or {}
        name = fn.get("name")
        if name in allowed:
            kept.append(c)
            continue
        alias = TOOL_ALIASES.get(name)
        if alias and alias in allowed:
            fn["name"] = alias
            kept.append(c)
            emit(f"工具名 {name} → {alias}（别名映射）")
        else:
            dropped = True
            emit(f"丢弃未知工具 {name}（不在本轮工具清单，避免死循环）")
    return kept, dropped


def current_site() -> dict:
    site_id = CONFIG["site"]
    try:
        return SITES["sites"][site_id]
    except KeyError:
        raise BridgeError(f"未知站点 {site_id}，可选：{', '.join(SITES['sites'])}")


def append_log(entry: dict) -> None:
    """每轮完整落盘，网页自动化最难 debug，日志必须留全。"""
    try:
        d = DATA_DIR / CONFIG.get("log_dir", "logs")
        d.mkdir(exist_ok=True)
        with (d / "requests.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def powershell_cmd(script: str, extra: list[str]) -> list[str]:
    return [
        CONFIG.get("powershell_exe") or "powershell.exe",
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(script),
    ] + extra


def _run_powershell(cmd: list[str], timeout: int):
    """GUI 模式下父进程没有控制台，PowerShell 子进程必须禁止另弹黑框。"""
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout, **kw,
    )


def invoke(payload: dict) -> dict:
    """调用 EAgent 的 2_send.ps1 完成一轮网页问答，返回解析后的结果。"""
    site = current_site()
    script = ROOT / site["scripts"]["2"]
    if not script.exists():
        raise BridgeError("脚本不存在，请先运行 python extract_eagent.py")

    messages = payload.get("messages") or []
    max_chars = int(CONFIG.get("max_prompt_chars", 24000))
    max_tool = int(CONFIG.get("max_tool_result_chars", protocol.MAX_TOOL_RESULT_CHARS))
    mode = CONFIG.get("context_mode", "incremental")
    first_bg = int(CONFIG.get("first_turn_background_chars", 700))
    if mode == "incremental":
        is_new, delta_msgs = TRACKER.delta(CONFIG["site"], messages)
        if is_new:
            prompt = protocol.flatten_messages(messages, payload.get("tools"),
                                               max_chars, background_chars=first_bg,
                                               max_tool_result_chars=max_tool)
        else:
            prompt = protocol.flatten_delta(delta_msgs, max_chars, max_tool_result_chars=max_tool)
            if not prompt.strip():
                # 增量为空多半是超时后 Harness 原样重试：网页端已收到上文，
                # 只发一句短续答即可，绝不重发整份协议+工具+历史（那是双倍消耗）。
                prompt = ("（请继续完成上一条【用户】消息的任务。需要工具时按规则输出 "
                          "tool_call 代码块，不需要就直接回答。）")
    else:
        prompt = protocol.flatten_messages(messages, payload.get("tools"), max_chars,
                                           max_tool_result_chars=max_tool)
    SESSION_CHARS[CONFIG["site"]] = SESSION_CHARS.get(CONFIG["site"], 0) + len(prompt)
    emit(f"提示词 {len(prompt)} 字 · {('增量' if mode == 'incremental' else '全量')}模式")

    tmp = Path(tempfile.mkdtemp(prefix="eabridge-"))
    msg_file = tmp / "msg.b64"
    out_file = tmp / "reply.txt"
    msg_file.write_text(
        base64.b64encode(prompt.encode("utf-8")).decode("ascii"), encoding="ascii",
    )

    timeout = int(CONFIG.get("timeout_sec", 180))
    cmd = powershell_cmd(str(script), [
        "-MessageB64", str(msg_file),
        "-OutputFile", str(out_file),
        "-Encoding", "utf8", "-Quiet",
        "-Timeout", str(timeout),
        "-FastFailSec", str(int(CONFIG.get("fast_fail_sec", 150))),
        "-Port", str(CONFIG.get("cdp_port", 9222)),
    ])

    started = time.time()
    stdout = stderr = reply = ""
    returncode = -1
    error = None
    try:
        proc = _run_powershell(cmd, timeout + 30)
        returncode = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""

        if out_file.exists():
            reply = out_file.read_text(encoding="utf-8", errors="replace")
        elif "----- reply.txt -----" in stdout:
            reply = stdout.split("----- reply.txt -----", 1)[1].strip()

        markers = ("ERROR:", "TIMEOUT", "FAIL_FAST", "CDP_BROKEN")
        if returncode != 0 or any(m in stdout for m in markers):
            reason = next((m for m in markers if m in stdout), f"exit={returncode}")
            error = f"{reason} | {stdout.strip()[-400:]} | {stderr.strip()[-200:]}"
        elif not reply.strip():
            error = "网页未返回内容（可能未登录、页面未打开或选择器已失效）"
        elif any(k in reply.lower() for k in QUOTA_MARKERS):
            error = "网页 AI 额度已耗尽或拒绝服务，桥已停止本轮；请换模型/站点或等额度恢复，勿重试"
    except subprocess.TimeoutExpired:
        error = f"PowerShell 脚本超过 {timeout + 30}s 未完成"
    except OSError as exc:
        error = f"无法启动 PowerShell：{exc}"
    finally:
        elapsed = round(time.time() - started, 2)

    append_log({
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "site": CONFIG["site"], "elapsed_sec": elapsed, "returncode": returncode,
        "prompt": prompt, "reply": reply,
        "stdout": stdout[-2000:], "stderr": stderr[-1000:], "error": error,
    })
    emit(f"一轮问答 {current_site()['label']} · {elapsed}s · "
         + (f"失败：{error[:80]}" if error else f"成功，回答 {len(reply)} 字"))
    if error:
        if "未返回内容" in error or "ERROR:" in error:
            error += "\n请先点控制面板的「打开网页 AI 并登录」，确认页面已登录。"
        raise BridgeError(error)

    content, tool_calls = protocol.parse_reply(reply)
    tool_calls, dropped = _reconcile_tool_names(tool_calls, payload)
    if dropped and not tool_calls and not content.strip():
        # 全被丢弃且没有正文：把网页原文当普通回答交回，避免空 assistant 触发 Harness 重试
        content = reply.strip()
    return {"content": content, "tool_calls": tool_calls, "elapsed": elapsed}


class Job:
    def __init__(self, payload: dict):
        self.payload = payload
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: Exception | None = None


class Worker(threading.Thread):
    """网页账号同一时刻只能有一个会话，所有请求在这里严格串行。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.q: queue.Queue[Job] = queue.Queue()

    def run(self) -> None:
        while True:
            job = self.q.get()
            try:
                job.result = invoke(job.payload)
            except Exception as exc:  # noqa: BLE001
                job.error = exc
            finally:
                job.done.set()

    def submit(self, payload: dict) -> dict:
        job = Job(payload)
        self.q.put(job)
        job.done.wait()
        if job.error:
            raise job.error
        return job.result or {}


def chat_completion_object(model: str, result: dict) -> dict:
    calls = result.get("tool_calls") or []
    message: dict = {"role": "assistant", "content": result.get("content") or None}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _is_title_request(payload: dict) -> bool:
    """Harness 每开新会话都会单独发一次「生成会话标题」的内部请求。"""
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str) and (
            "Generate the session title from this JSON array" in c
            or "Create a concise title for an AI coding-assistant session" in c
        ):
            return True
    return False


def _local_title(payload: dict) -> dict:
    """标题请求本地直接产出，不转发到网页 AI：省额度、不污染会话缓存。"""
    raw = ""
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str) and "Generate the session title" in c:
            raw = c
            break
    match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    text = ""
    if match:
        try:
            text = json.loads('"' + match.group(1) + '"')
        except ValueError:
            text = match.group(1)
    text = " ".join(text.split()) or "新会话"
    title = (text[:14] if re.search(r"[\u4e00-\u9fff]", text)
             else " ".join(text.split()[:6]))
    emit(f"会话标题请求已在本地处理，未占用网页额度：{title}")
    return chat_completion_object(
        payload.get("model") or CONFIG.get("model_alias", "web-ai"),
        {"content": title, "tool_calls": []},
    )


def sse_lines(model: str, result: dict):
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    calls = result.get("tool_calls") or []

    def chunk(delta: dict, finish=None) -> str:
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }, ensure_ascii=False) + "\n\n"

    yield chunk({"role": "assistant", "content": ""})
    if calls:
        for i, c in enumerate(calls):
            yield chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                        "function": c["function"]}]})
    else:
        text = result.get("content") or ""
        for i in range(0, len(text), 200):
            yield chunk({"content": text[i:i + 200]})
    yield chunk({}, "tool_calls" if calls else "stop")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------- 控制面板 API

def config_info() -> dict:
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8300))
    return {
        "site": CONFIG["site"],
        "sites": [{"id": k, "label": v["label"], "url": v["url"]}
                  for k, v in SITES["sites"].items()],
        "base_url": f"http://{host}:{port}/v1",
        "panel_url": f"http://{host}:{port}/",
        "model_alias": CONFIG.get("model_alias", "web-ai"),
        "cdp_port": CONFIG.get("cdp_port", 9222),
        "listen_port": port,
        "timeout_sec": CONFIG.get("timeout_sec", 180),
        "max_prompt_chars": CONFIG.get("max_prompt_chars", 24000),
        "context_mode": CONFIG.get("context_mode", "incremental"),
        "config_file": str(DATA_DIR / "config.json"),
    }


def set_site(site_id: str) -> dict:
    """图形界面里换站点：立即改内存 + 写回 exe 旁 config.json，下次启动仍生效。"""
    if site_id not in SITES["sites"]:
        raise BridgeError(f"未知站点 {site_id}")
    CONFIG["site"] = site_id
    TRACKER.reset(site_id)  # 换站点后网页会话是全新的，需要重新注入首轮背景
    SESSION_CHARS.pop(site_id, None)
    path = DATA_DIR / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(CONFIG)
        data["site"] = site_id
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        saved = True
    except (OSError, ValueError) as exc:
        saved = False
        emit(f"配置写入失败：{exc}")
    label = SITES["sites"][site_id]["label"]
    emit(f"站点切换为 {label}" + ("（已保存到 config.json）" if saved else "（仅本次有效）"))
    return {"site": site_id, "label": label, "saved": saved, **config_info()}


def panel_html() -> str:
    p = ROOT / "panel.html"
    if not p.exists() and getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", str(ROOT))) / "panel.html"
    if not p.exists():
        return "<h1>控制面板 panel.html 缺失，请重新解压完整分发包</h1>"
    return p.read_text(encoding="utf-8")


def browser_ctl(action: str) -> str:
    site = current_site()
    key = "1" if action == "start" else "3"
    script = ROOT / site["scripts"][key]
    cmd = powershell_cmd(str(script), ["-Port", str(CONFIG.get("cdp_port", 9222))])
    if action == "start" and CONFIG.get("user_data_dir"):
        cmd += ["-UserDataDir", CONFIG["user_data_dir"]]
    lines = [f"执行：{' '.join(cmd)}"]
    try:
        proc = _run_powershell(cmd, 60)
        out = (proc.stdout or "").strip()
        if out:
            lines.append(out)
        if (proc.stderr or "").strip():
            lines.append("[stderr] " + proc.stderr.strip()[-500:])
        if action == "start":
            lines.append(f"请在弹出的窗口里登录 {site['label']}。网页只用来看，发消息在 Harness 里进行。")
            lines.append(f"调试端口 {CONFIG.get('cdp_port', 9222)} 仅绑定本机。")
        else:
            lines.append("已发送关闭指令。切换站点前建议先关掉浏览器。")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"执行失败：{exc}")
    text = "\n".join(lines)
    for line in lines:
        emit(line)
    return text


def selftest() -> None:
    print(f"上游版本 : {SITES.get('generated_from')}")
    print(f"当前站点 : {CONFIG['site']} ({current_site()['label']})")
    for site_id, meta in SITES["sites"].items():
        for key, rel in meta["scripts"].items():
            p = ROOT / rel
            print(f"  [{'OK' if p.exists() else 'MISSING'}] {site_id}/{key}: {rel}")
            if not p.exists():
                raise SystemExit(1)
    print("protocol selftest:")
    protocol.selftest()
    print("\n自检通过。未连接浏览器、未发送任何网页请求。")


def selftest_capture() -> str:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            selftest()
        return buf.getvalue()
    except SystemExit as exc:
        return buf.getvalue() + f"\n自检失败（exit {exc.code}）"
    except Exception as exc:  # noqa: BLE001
        return buf.getvalue() + f"\n自检异常：{exc}"


class Handler(BaseHTTPRequestHandler):
    worker: Worker

    def log_message(self, fmt: str, *args) -> None:
        pass  # GUI 模式下 stderr 可能为 None；日志由 /api/logs 提供

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 502) -> None:
        self._json({"error": {"message": message, "type": "bridge_error", "code": status}}, status)

    def _authorized(self) -> bool:
        key = CONFIG.get("api_key", "")
        return True if not key else self.headers.get("Authorization", "") == f"Bearer {key}"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._html(panel_html())
        elif path in ("/v1/models", "/models"):
            self._json({"object": "list", "data": [{
                "id": CONFIG.get("model_alias", "web-ai"), "object": "model",
                "created": int(time.time()), "owned_by": "eagent-bridge",
            }]})
        elif path == "/health":
            self._json({"status": "ok", "site": CONFIG["site"],
                        "site_label": current_site()["label"],
                        "cdp_port": CONFIG.get("cdp_port", 9222),
                        "queued": self.worker.q.qsize()})
        elif path == "/api/config":
            self._json(config_info())
        elif path == "/api/status":
            self._json({"running": True, "site": CONFIG["site"],
                        "site_label": current_site()["label"],
                        "queued": self.worker.q.qsize(),
                        "session_chars": SESSION_CHARS.get(CONFIG["site"], 0),
                        "cdp_port": CONFIG.get("cdp_port", 9222)})
        elif path == "/api/logs":
            self._json({"lines": list(EVENTS)})
        else:
            self._error("not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError) as exc:
            self._error(f"bad request body: {exc}", 400)
            return

        if path.startswith("/api/"):
            self._handle_api(path, payload)
            return
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error("not found", 404)
            return
        if _is_title_request(payload):
            title_obj = _local_title(payload)
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self._cors()
                self.end_headers()
                model = payload.get("model") or CONFIG.get("model_alias", "web-ai")
                result = {"content": title_obj["choices"][0]["message"]["content"],
                          "tool_calls": []}
                for line in sse_lines(model, result):
                    self.wfile.write(line.encode("utf-8"))
                self.close_connection = True
            else:
                self._json(title_obj)
            return
        if not self._authorized():
            self._error("invalid api key", 401)
            return

        try:
            result = self.worker.submit(payload)
        except BridgeError as exc:
            self._error(str(exc), 502)
            return
        except Exception as exc:  # noqa: BLE001
            self._error(f"unexpected: {exc}", 500)
            return

        model = payload.get("model") or CONFIG.get("model_alias", "web-ai")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            try:
                for line in sse_lines(model, result):
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
        else:
            self._json(chat_completion_object(model, result))

    def _handle_api(self, path: str, payload: dict) -> None:
        try:
            if path == "/api/config":
                self._json(set_site(str(payload.get("site", ""))))
            elif path == "/api/browser":
                action = str(payload.get("action", "start"))
                if action not in ("start", "stop"):
                    self._error("action 只能是 start / stop", 400)
                    return
                if action == "start":
                    # 重新打开/登录网页通常意味着网页端是一个新会话
                    TRACKER.reset(CONFIG["site"])
                    SESSION_CHARS.pop(CONFIG["site"], None)
                    emit("已重置对话缓存，下一轮按新会话发送精简背景")
                self._json({"output": browser_ctl(action)})
            elif path == "/api/session/reset":
                site = payload.get("site")
                TRACKER.reset(site)
                if site:
                    SESSION_CHARS.pop(site, None)
                else:
                    SESSION_CHARS.clear()
                emit("对话缓存已重置，下一轮会重新发送精简背景")
                self._json({"ok": True})
            elif path == "/api/selftest":
                self._json({"output": selftest_capture()})
            elif path == "/api/shutdown":
                emit("收到退出指令，服务即将关闭")
                threading.Timer(0.4, SHUTDOWN_EVENT.set).start()
                self._json({"ok": True})
            else:
                self._error("not found", 404)
        except BridgeError as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            self._error(f"unexpected: {exc}", 500)


def make_server() -> ThreadingHTTPServer:
    worker = Worker()
    worker.start()
    Handler.worker = worker
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8300))
    return ThreadingHTTPServer((host, port), Handler)


def serve_forever_background(srv: ThreadingHTTPServer) -> None:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    emit(f"EAgent-Bridge 服务已启动：http://{CONFIG.get('listen_host', '127.0.0.1')}:{CONFIG.get('listen_port', 8300)}/")
    emit(f"当前站点：{current_site()['label']} · 调试端口 {CONFIG.get('cdp_port', 9222)}")
    emit(f"日志文件：{DATA_DIR / CONFIG.get('log_dir', 'logs') / 'requests.jsonl'}")


def stop_server(srv: ThreadingHTTPServer) -> None:
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="EAgent -> OpenAI 兼容桥")
    ap.add_argument("--site", choices=list(SITES["sites"]), help="临时覆盖 config.json 里的站点")
    ap.add_argument("--start-browser", action="store_true", help="启动调试浏览器（需人工登录）")
    ap.add_argument("--stop-browser", action="store_true", help="关闭调试浏览器")
    ap.add_argument("--selftest", action="store_true", help="只做静态自检")
    ap.add_argument("--serve-only", action="store_true", help="只启动服务，不打开控制面板窗口")
    args = ap.parse_args()

    if args.site:
        CONFIG["site"] = args.site
    if args.selftest:
        selftest()
        return
    if args.start_browser:
        browser_ctl("start")
        return
    if args.stop_browser:
        browser_ctl("stop")
        return

    srv = make_server()
    serve_forever_background(srv)
    print(f"  模型名    : {CONFIG.get('model_alias', 'web-ai')}")
    print("  提示：网页 AI 单会话，所有请求串行执行；请勿高频调用。")
    try:
        SHUTDOWN_EVENT.wait()
    except KeyboardInterrupt:
        emit("已收到停止信号")
    finally:
        stop_server(srv)


if __name__ == "__main__":
    main()
