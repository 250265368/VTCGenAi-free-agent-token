#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAI chat.completions 协议 <-> 网页 AI 纯文本 的双向转换层。

网页版 AI 没有原生 function calling，Harness 却有严格的 tools / tool_calls 协议，
这一层就是两者之间的翻译器：
  请求侧：把 tools 定义压成紧凑列表写进提示词，要求模型用 tool_call 代码块回一次调用
  响应侧：从回答文本里解析该代码块，还原成 OpenAI tool_calls

请求侧最重要的一条经验：Harness 的 system prompt + 完整工具 JSON schema 动辄 5 万字，
网页端输入框根本吃不下。必须压缩背景设定、紧凑化工具定义，并保证真正的用户任务
永远留在结尾，否则网页 AI 会「看不到任务」而去调 get_goal。

响应侧容错：代码块围栏常被网页 textContent 吃掉、语言标记后粘按钮文字、
PowerShell 里的裸双引号会弄坏 JSON —— 都要能救回来。

上游：https://github.com/YaoGaoChaoLe/EAgent (GPL-3.0)，本文件为衍生作品，同样 GPL-3.0。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

FENCE_RE = re.compile(r"```(?:tool[\s_-]*call|tool|json)[^\n\r]*\r?\n(.*?)```", re.S | re.I)
NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
ARGS_RE = re.compile(r'"arguments"\s*:\s*(\{.*?\})\s*(?:,|\})', re.S)

# DeepSeek 系模型（如 VTC 的 DeepSeek V4 Flash）原生工具调用是 DSML 标记而非 JSON：
#   <｜｜DSML｜｜ invoke name="pwsh"> <｜｜DSML｜｜ parameter name="command" string="true">…</…>
# 竖线可能是全角｜或半角|、个数不定；参数值里含裸 | 与反斜杠，只能按标签切，不能按 | 切。
_DB = r"[｜|]+"
_DP = r"<" + _DB + r"DSML" + _DB + r"\s*"
_DC = r"</" + _DB + r"DSML" + _DB + r"\s*"
DSML_INVOKE_RE = re.compile(_DP + r'invoke\s+name="([^"]+)"[^>]*>(.*?)' + _DC + r"invoke>", re.S)
DSML_PARAM_RE = re.compile(
    _DP + r'parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?[^>]*>(.*?)' + _DC + r"parameter>", re.S)
DSML_CALLS_RE = re.compile(_DP + r"calls>.*?" + _DC + r"calls>", re.S)
UI_NOISE_LINES = {
    "复制", "下载", "复制代码", "复制于稿", "重新生成", "已思考", "编辑", "分享",
    "Copy code", "Copy", "Regenerate", "Thumbs up", "Thumbs down",
}

# Harness 每轮会往 user 消息里塞的运行时快照，对网页 AI 没有意义，且会挤掉真正的任务
CONTEXT_NOISE_MARKERS = (
    "Time sampled while preparing turn",
    "Browser time zone for this request",
    "Elapsed since the preceding model-visible message",
    "<system-reminder>",
    "Current DeepSeek Harness Web GUI URL",
    "Current DeepSeek Harness implementation checkout",
)

MAX_BACKGROUND_CHARS = 2600   # 原始 system prompt 保留上限
TAIL_BUDGET = 400             # 结尾指令预留

TOOL_PROTOCOL = """## 工具调用规则（必须严格遵守）
需要调用工具时，只输出一个代码块，语言标记为 tool_call，内容是一个 JSON 对象。
name 必须是下面「可用工具」清单里真实存在的工具名，arguments 的键必须与该工具声明的参数一致：

```tool_call
{"name":"<真实工具名>","arguments":{"<参数名>":"<参数值>"}}
```

上面这行只是格式示意，**绝对不要原样输出**，也不要带尖括号占位符。

硬性规则：
1. 每轮最多调用一个工具，禁止一次输出多个工具调用或多个代码块。
2. 需要工具时代码块之外不要写解释；不需要工具时正常用自然语言回答，不要输出代码块。
3. arguments 必须是合法 JSON。参数值里出现双引号要写成 \\" 转义，换行写成 \\n，
   反斜杠写成 \\\\。宁可把命令写简单，也要保证 JSON 能被解析。
4. 工具结果会在下一轮以【工具 … 返回】给出，拿到结果后再决定下一步。
5. 信息不足时直接用自然语言提问，不要猜。

可用工具（括号内为参数，带 * 的是必填）：
{tools_list}

## 会话说明
上面【用户】【助手】【工具 … 返回】是按时间顺序排列的对话记录。"""

TAIL_RULE = (
    "【本轮要求】\n"
    "请完成上面最后一条【用户】消息所描述的任务。需要工具时按上面的规则输出 tool_call 代码块；"
    "不需要工具就直接给出最终回答。"
)
BACKGROUND_NOTICE = "【角色与背景设定】（仅供你理解自己的角色，不必复述；后面【用户】开头的内容才是要处理的请求）\n"


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        return "".join(parts)
    return str(content)


def _is_context_noise(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(marker) for marker in CONTEXT_NOISE_MARKERS)


MAX_TOOLS_CHARS = 2400
MAX_TOOL_DESC = 45
# 核心工具必须出现在清单里：按顺序截断曾把 write 截掉，模型看不到就幻觉出 create_file
PRIORITY_TOOLS = ("write", "read", "edit", "pwsh", "glob", "grep",
                  "read_image", "modlens_read_image", "ask_user_question", "get_goal")
MAX_TOOL_RESULT_CHARS = 3000   # 单条工具返回的预算，超出按 head+tail 截断

_PS_NOISE_PREFIXES = ("+ CategoryInfo", "+ FullyQualifiedErrorId")


def _compact_tool_result(text: str, budget: int = MAX_TOOL_RESULT_CHARS) -> str:
    """工具返回常是整份文件 / 题目全文 / PowerShell 报错堆栈，原样转发极耗 token。
    先去掉无信息量的噪音行，再按预算保留头尾（中间省略），保住关键内容。"""
    if not text:
        return ""
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s == "[P empty]":
            continue
        if s.startswith(_PS_NOISE_PREFIXES) or re.fullmatch(r"\+\s*~+", s):
            continue
        lines.append(re.sub(r"^\[P:[^\]]*\]\s*", "", ln))
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if budget > 0 and len(cleaned) > budget:
        head = budget * 2 // 3
        tail = budget - head - 20
        cleaned = cleaned[:head] + "\n…（中间部分省略）…\n" + cleaned[-tail:]
    return cleaned


def _compact_tools(tools: list[dict] | None) -> str:
    """把工具 schema 压成一行一个，并设总字数上限（50 个工具原样要 7800 字）。
    核心工具（write/read/edit/pwsh…）优先排到最前，保证不被字数上限截掉——
    否则模型看不到 write 就会幻觉出 create_file 之类不存在的工具，触发死循环烧额度。"""
    by_name: dict[str, str] = {}
    order: list[str] = []
    for t in tools or []:
        if t.get("type") != "function":
            continue
        f = t.get("function") or {}
        name = f.get("name") or "?"
        desc = " ".join((f.get("description") or "").split())
        if len(desc) > MAX_TOOL_DESC:
            desc = desc[:MAX_TOOL_DESC] + "…"
        params = f.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        args = [f"{k}{'*' if k in required else ''}" for k in props]
        line = f"- {name}({', '.join(args)}) {desc}"
        if name not in by_name:
            order.append(name)
        by_name[name] = line
    ranked = [n for n in PRIORITY_TOOLS if n in by_name]
    ranked += [n for n in order if n not in PRIORITY_TOOLS]
    lines = [by_name[n] for n in ranked]
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > MAX_TOOLS_CHARS:
            out.append(f"…（另有 {len(lines) - len(out)} 个工具未列出）")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "（本轮没有可用工具）"


def render_tools(tools: list[dict] | None) -> list[dict]:
    """保留完整结构，供需要详细工具定义的调用方使用。"""
    out = []
    for t in tools or []:
        if t.get("type") != "function":
            continue
        f = t.get("function") or {}
        out.append({
            "name": f.get("name") or "",
            "description": f.get("description") or "",
            "parameters": f.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _select_recent(body: list[str], budget: int) -> tuple[list[str], int]:
    """按预算从后往前保留对话，且至少保住最近两条，避免只剩一条运行时快照。"""
    kept: list[str] = []
    used = 0
    for chunk in reversed(body):
        piece = chunk + "\n\n"
        if len(kept) >= 2 and used + len(piece) > budget:
            break
        kept.insert(0, piece)
        used += len(piece)
    return kept, len(body) - len(kept)


def flatten_messages(
    messages: list[dict],
    tools: list[dict] | None = None,
    max_chars: int = 24000,
    background_chars: int = 2600,
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
) -> str:
    """把 OpenAI messages[] + tools[] 压成一条网页 AI 吃得下的文本。"""
    background: list[str] = []
    body: list[str] = []

    for m in messages or []:
        role = m.get("role") or "user"
        content = _text_of(m.get("content"))

        if role in ("system", "developer"):
            if content.strip():
                background.append(content)
        elif _is_context_noise(content):
            # 运行时快照丢进背景区，不占「最近对话」的名额
            background.append(content)
        elif role == "user":
            body.append(f"【用户】\n{content}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if calls:
                simplified = []
                for c in calls:
                    fn = (c.get("function") or {})
                    simplified.append({"name": fn.get("name"), "arguments": fn.get("arguments")})
                body.append("【助手的工具调用】\n" + json.dumps(simplified, ensure_ascii=False))
            if content.strip():
                body.append(f"【助手】\n{content}")
        elif role == "tool":
            name = m.get("name") or "tool"
            body.append(f"【工具 {name} 返回】\n{_compact_tool_result(content, max_tool_result_chars)}")
        else:
            body.append(f"【{role}】\n{content}")

    raw_bg = "\n\n".join(background)
    cap = max(0, background_chars)
    if cap == 0:
        # 完全丢弃 Harness 的内部 system / skill 说明：对网页 AI 无用且极易被复述
        raw_bg = ""
    elif len(raw_bg) > cap:
        raw_bg = raw_bg[:cap] + "\n…（背景设定后略）"
    system = (BACKGROUND_NOTICE + raw_bg) if raw_bg else ""
    if tools:
        # 模板里的 JSON 示例含花括号，不能用 str.format
        system += "\n\n" + TOOL_PROTOCOL.replace("{tools_list}", _compact_tools(tools))

    budget = max(1500, max_chars - len(system) - TAIL_BUDGET)
    kept, omitted = _select_recent(body, budget)
    notice = f"【说明】更早的 {omitted} 条对话因长度限制已省略。\n\n" if omitted else ""

    # 结尾指令必须紧跟最后一条消息，放在最前面模型会忽略
    return system + "\n\n" + notice + "".join(kept) + TAIL_RULE


def flatten_delta(messages: list[dict], max_chars: int = 24000,
                  max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """增量模式：网页端自己有会话记忆，只发送本轮新增的消息，不再重发背景与历史。"""
    body: list[str] = []
    for m in messages or []:
        role = m.get("role") or "user"
        content = _text_of(m.get("content"))
        if role in ("system", "developer") or _is_context_noise(content):
            continue
        if role == "user":
            body.append(f"【用户】\n{content}")
        elif role == "tool":
            name = m.get("name") or "tool"
            body.append(f"【工具 {name} 返回】\n{_compact_tool_result(content, max_tool_result_chars)}")
        elif role == "assistant":
            # 助手正文与工具调用都不回传：网页端会话里已有它自己刚生成的回复，
            # 再塞一遍（尤其是 write 整个文件的 arguments）是纯重复消耗。
            continue
    text = "\n\n".join(body).strip()
    if len(text) > max_chars:
        text = "…（过长的上文已省略）\n" + text[-max_chars:]
    return text


def clean_web_text(text: str) -> str:
    """去掉网页版助手消息 textContent 里混入的按钮 / 状态文案。"""
    text = re.sub(r"</?[｜|]*\s*tool_call\s*>", "", text, flags=re.I)
    text = re.sub(
        r"(?i)(tool[\s_-]*call)(?:\s*(?:复制代码|复制于稿|复制|下载|重新生成|编辑|分享|Copy code|Copy|Regenerate))+",
        r"\1", text,
    )
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s in UI_NOISE_LINES:
            continue
        if re.match(r"^已思考[（(]", s) and len(s) < 24:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _fix_invalid_escapes(s: str) -> str:
    """模型常把 PowerShell 子表达式写成 \\( \\) 这类非法 JSON 转义。
    合法转义只有 " \\ / b f n r t u；其余的反斜杠直接丢掉、保留原字符。
    按 escape 感知扫描，避免把合法的 \\\\ 误伤。"""
    valid = set('"\\/bfnrtu')
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in valid:
                out.append(c)
                out.append(nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _repair_unescaped_quotes(s: str) -> str:
    """把 JSON 字符串内部、后面不是结构字符的裸双引号转义成 \\" 。"""
    out: list[str] = []
    i, in_str, n = 0, False, len(s)
    while i < n:
        ch = s[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ",}:]":
                out.append('"')
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _try_json(s: str) -> dict | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _raw_object(s: str) -> dict | None:
    """用 JSON 解析器自己找对象边界。不能用 rfind('}') —— HTML/CSS 参数值里
    充满花括号，会把结尾定位到字符串内部，导致整段 JSON 解析失败、参数全丢。"""
    start = s.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _unescape_lenient(v: str) -> str:
    """把 JSON 字符串值反转义成真实字符；非法转义（如 \\(）保留原字符。"""
    mapping = {'"': '"', '\\': '\\', '/': '/', 'n': '\n', 't': '\t',
               'r': '\r', 'b': '\b', 'f': '\f'}
    out = []
    i, n = 0, len(v)
    while i < n:
        c = v[i]
        if c == "\\" and i + 1 < n:
            nxt = v[i + 1]
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < n:
                try:
                    out.append(chr(int(v[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _salvage_args(s: str) -> dict | None:
    """整体 JSON 因裸引号/非法转义无法解析时，按参数键的边界直接捞字符串值。
    值的结束位置 = 下一个键的开头（或 arguments 收尾），不依赖值内部引号是否转义。"""
    m = re.search(r'"arguments"\s*:\s*\{', s)
    if not m:
        return None
    body = s[m.end():]
    keys = list(re.finditer(r'"(\w+)"\s*:\s*"', body))
    if not keys:
        return None
    args = {}
    for idx, km in enumerate(keys):
        val = body[km.end(): keys[idx + 1].start() if idx + 1 < len(keys) else len(body)]
        if idx + 1 < len(keys):
            val = re.sub(r'",\s*$', '', val)
        else:
            val = re.sub(r'"\s*\}\s*$', '', val)
        args[km.group(1)] = _unescape_lenient(val)
    return args or None


def _loose_json(raw: str) -> dict | None:
    s = raw.strip()
    cleaned = re.sub(r",\s*([}\]])", r"\1", s)
    fixed = _fix_invalid_escapes(s)
    fixed_cleaned = _fix_invalid_escapes(cleaned)
    for candidate in (s, fixed, _repair_unescaped_quotes(s),
                      _repair_unescaped_quotes(fixed),
                      cleaned, fixed_cleaned, _repair_unescaped_quotes(cleaned),
                      _repair_unescaped_quotes(fixed_cleaned)):
        obj = _try_json(candidate) or _raw_object(candidate)
        if obj:
            return obj

    name_m = NAME_RE.search(s)
    if not name_m:
        return None
    salv = _salvage_args(s)
    if salv:
        return {"name": name_m.group(1), "arguments": salv}
    args: dict = {}
    args_m = ARGS_RE.search(s)
    if args_m:
        frag = args_m.group(1)
        for candidate in (frag, _fix_invalid_escapes(frag),
                          _repair_unescaped_quotes(frag),
                          _repair_unescaped_quotes(_fix_invalid_escapes(frag))):
            obj = _try_json(candidate)
            if obj:
                args = obj
                break
    return {"name": name_m.group(1), "arguments": args}


def _clean(text: str) -> str:
    return text.strip()


META_KEYS = {"name", "type", "id", "function", "arguments"}


def _normalize_tool_obj(obj: dict | None) -> dict | None:
    """网页模型常把参数平铺在顶层：{"name":"write","file_path":...,"content":...}。
    缺 arguments 时，把除元数据外的键收拢成 arguments，还原成 OpenAI 的形状。"""
    if not isinstance(obj, dict) or not obj.get("name"):
        return None
    if "arguments" in obj:
        return obj
    args = {k: v for k, v in obj.items() if k not in META_KEYS}
    return {"name": obj["name"], "arguments": args}


PLACEHOLDER_NAMES = {"工具名", "example_tool", "example", "tool_name", "xxx", "?"}


def _is_placeholder(name: str) -> bool:
    """提示词里的格式示例被网页模型照抄时，不能当成真实工具调用交回 Harness，
    否则 Harness 会去执行一个不存在的工具，把报错塞进会话历史。"""
    n = (name or "").strip()
    low = n.lower()
    return (not n or low in PLACEHOLDER_NAMES or n.startswith("<")
            or "工具名" in n or "参数名" in n or "真实工具名" in n)


def _parse_dsml(text: str) -> list[dict]:
    """把 DeepSeek 原生 DSML 工具调用还原成 OpenAI tool_calls。"""
    calls = []
    for m in DSML_INVOKE_RE.finditer(text):
        args: dict = {}
        for pm in DSML_PARAM_RE.finditer(m.group(2)):
            key, is_str, val = pm.group(1), pm.group(2), pm.group(3).strip()
            if is_str == "false":
                try:
                    val = json.loads(val)
                except ValueError:
                    pass
            args[key] = val
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": m.group(1), "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


def parse_reply(text: str) -> tuple[str, list[dict]]:
    """把网页 AI 的回答拆成 (自然语言内容, OpenAI tool_calls)。"""
    if not text or not text.strip():
        return "", []

    text = clean_web_text(text)
    dsml = _parse_dsml(text)
    if dsml:
        return _clean(DSML_CALLS_RE.sub("", text)), dsml
    m = FENCE_RE.search(text)
    obj = _loose_json(m.group(1)) if m else None
    # 围栏被网页 textContent 吃掉是常态；只要像工具调用就从全文捞一次
    if obj is None and ('"name"' in text or re.search(r"tool[\s_-]*call", text, re.I)):
        obj = _loose_json(text)
    obj = _normalize_tool_obj(obj)

    if not obj or _is_placeholder(str(obj.get("name", ""))):
        return _clean(text), []

    name = str(obj["name"])
    args = obj.get("arguments", {})
    if not args and re.search(r'"arguments"\s*:\s*\{\s*[^}\s]', text):
        # 原文有参数内容却解析成空：交回空参数会让 Harness 报 invalid arguments
        # 并无限重试烧额度；宁可当纯文本交回，也不触发死循环。
        return _clean(text), []
    arg_str = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    call = {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": name, "arguments": arg_str},
    }
    content = _clean(text[:m.start()] + "\n" + text[m.end():]) if m else ""
    return content, [call]


def selftest() -> None:
    bad_json = (
        '```tool_call 复制 下载\n'
        '{"name":"pwsh","arguments":{"description":"Check desktop path",'
        '"command":"$d = [Environment]::GetFolderPath(\'Desktop\'); '
        'Write-Output ("Desktop=$d"); Write-Output ("Exists=$(Test-Path $d)")"}}\n'
        '```'
    )
    no_fence = (
        'tool_call复制下载\n'
        '{"name":"pwsh","arguments":{"description":"check","command":"Get-Location"}}\n'
        '复制\n下载'
    )
    flat_args = (
        'tool_call复制下载{"name":"write","file_path":"D:/work/pelican.html",'
        '"content":"<!DOCTYPE html><title>x</title>","justification":"create"}'
    )
    cases = [
        ("纯文本回答", "这是普通回答。", "这是普通回答。", []),
        ("标准工具调用",
         '```tool_call\n{"name":"read_file","arguments":{"path":"a.txt"}}\n```', "", ["read_file"]),
        ("夹解释 + 尾随逗号",
         '好的。\n```tool_call\n{"name":"read_file","arguments":{"path":"a.txt"},}\n```\n完成',
         None, ["read_file"]),
        ("网页 UI 文案 + 裸引号坏 JSON", bad_json, None, ["pwsh"]),
        ("围栏丢失 + 按钮文字粘连", no_fence, None, ["pwsh"]),
        ("参数平铺无 arguments", flat_args, None, ["write"]),
        ("格式示例占位符不当成工具调用",
         '```tool_call\n{"name":"<真实工具名>","arguments":{"<参数名>":"<参数值>"}}\n```',
         None, []),
        ("坏到无法恢复时保留原文", '```tool_call\n{name: broken}\n```', None, []),
    ]
    for label, raw, expect_content, expect_calls in cases:
        content, calls = parse_reply(raw)
        names = [c["function"]["name"] for c in calls]
        ok = names == expect_calls
        if expect_content is not None:
            ok = ok and content == expect_content
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: content={len(content)} chars, calls={names}")
        if not ok:
            raise SystemExit(1)

    _, bad_calls = parse_reply(bad_json)
    arg = json.loads(bad_calls[0]["function"]["arguments"])
    assert 'Write-Output ("Desktop=$d")' in arg["command"], arg["command"]
    print("  [PASS] 坏 JSON 修复后命令中的双引号已还原")

    _, flat_calls = parse_reply(flat_args)
    fa = json.loads(flat_calls[0]["function"]["arguments"])
    assert fa.get("file_path") == "D:/work/pelican.html" and "content" in fa
    print("  [PASS] 平铺参数已收拢成 arguments")

    css_braces = (
        'tool_call复制下载{"name":"write","file_path":"D:/work/pelican.html",'
        '"content":"<style>body{color:red}.x{margin:0}</style><p>hi</p>"}'
    )
    _, css_calls = parse_reply(css_braces)
    ca = json.loads(css_calls[0]["function"]["arguments"])
    assert ca.get("file_path") == "D:/work/pelican.html"
    assert ".x{margin:0}" in ca.get("content", ""), ca.get("content")
    print("  [PASS] 参数值内含 CSS 花括号时仍能完整解析")

    dsml = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="pwsh">\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">Check working directory contents</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem -Force | Select-Object Name, Length | Format-Table -AutoSize</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="workdir" string="true">D:\\DeepSeek Harness Desktop\\work</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    dc, dcalls = parse_reply(dsml)
    assert [c["function"]["name"] for c in dcalls] == ["pwsh"], dcalls
    da = json.loads(dcalls[0]["function"]["arguments"])
    assert da["command"] == "Get-ChildItem -Force | Select-Object Name, Length | Format-Table -AutoSize"
    assert da["workdir"] == "D:\\DeepSeek Harness Desktop\\work"
    assert dc == ""
    print("  [PASS] DeepSeek DSML 工具调用已还原（含裸 | 与反斜杠参数）")

    _, c2 = parse_reply('<|DSML|invoke name="read"><|DSML|parameter name="n" string="false">5</|DSML|parameter></|DSML|invoke>')
    assert json.loads(c2[0]["function"]["arguments"])["n"] == 5
    print("  [PASS] DSML 半角竖线与非字符串参数也能解析")

    wrapped = ('<｜tool_call>\n{"name":"pwsh","arguments":{"description":"render preview",'
               '"command":"\\(out = \'D:\\\\work\\\\p.png\'; \\)url = \'file:///x.html\'; '
               'Start-Process -FilePath \\(edge -ArgumentList \'--headless=new\',"--screenshot=\\(out\\",\\)url -Wait"}}\n'
               '</tool_call>')
    wc, wcalls = parse_reply(wrapped)
    assert [c["function"]["name"] for c in wcalls] == ["pwsh"], wcalls
    wa = json.loads(wcalls[0]["function"]["arguments"])
    assert wa["description"] == "render preview" and "Start-Process" in wa["command"]
    assert "\\(" not in wa["command"]
    print("  [PASS] tool_call 标签包裹 + 非法 \\( \\) 转义已修复并还原参数")

    guard = '{"name":"pwsh","arguments":{"command":<<<not json>>>}}'
    gc, gcalls = parse_reply(guard)
    assert gcalls == [], "解析不出参数时不应交回空参数触发 Harness 死循环"
    print("  [PASS] 空参数保险丝：解析失败当纯文本交回，不触发 invalid-arguments 重试")

    _, gc2 = parse_reply('tool_call{"name":"get_goal","arguments":{}}')
    assert [c["function"]["name"] for c in gc2] == ["get_goal"]
    print("  [PASS] 合法无参工具(get_goal)不被保险丝误杀")

    # 复现线上事故：超长 system + 几十个工具的完整 schema + 运行时快照挤掉任务
    huge_system = "You are an AI agent powered by DeepSeek Harness.\n" + (
        "Current mode: bing. Enabled engines: exa, tavily, firecrawl, parallel. " * 900
    )
    tools = [{"type": "function", "function": {
        "name": f"engine_{i}", "description": "Search the web. " * 40,
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "query text"},
            "limit": {"type": "integer", "description": "max results"},
        }, "required": ["query"]},
    }} for i in range(24)]
    msgs = [
        {"role": "system", "content": huge_system},
        {"role": "user", "content": "你是什么模型"},
        {"role": "assistant", "content": "我是 DeepSeek Harness 里的智能体。"},
        {"role": "user", "content": "做一个鹈鹕骑自行车的 HTML 页面，保存为 pelican.html 放到桌面"},
        {"role": "user", "content": "Time sampled while preparing turn 4, step 1: 2026-11-09T19:54:18+08:00\nBrowser time zone for this request: Asia/Hong_Kong."},
        {"role": "tool", "name": "get_goal", "content": '{"goal":null}'},
    ]
    prompt = flatten_messages(msgs, tools, max_chars=24000)
    print(f"  [{'PASS' if len(prompt) < 9000 else 'FAIL'}] 超长 system 已压缩: prompt {len(prompt)} chars")
    assert len(prompt) < 9000
    assert "鹈鹕骑自行车的 HTML 页面" in prompt, "真正的用户任务被截掉了"
    print("  [PASS] 真正的用户任务保留在 prompt 中")
    assert "Time sampled while preparing" not in prompt, "运行时快照不应挤占任务"
    print("  [PASS] 运行时快照未挤占真正的用户任务")
    assert prompt.rstrip().endswith("不需要工具就直接给出最终回答。")
    print("  [PASS] 本轮要求位于 prompt 末尾")
    assert "engine_0" in prompt and "query*" in prompt
    print("  [PASS] 工具定义已紧凑化并保留必填标记")

    short = flatten_messages(msgs, tools, max_chars=24000, background_chars=700)
    assert len(short) < 8000 and len(short) < len(prompt), len(short)
    print(f"  [PASS] 首轮精简背景: prompt {len(short)} chars（默认 {len(prompt)} chars）")

    delta = flatten_delta([
        {"role": "system", "content": "ignored background"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
        {"role": "assistant", "tool_calls": [{"function": {
            "name": "write", "arguments": '{"file_path":"a.java","content":"' + "X" * 5000 + '"}'}}]},
        {"role": "tool", "name": "pwsh", "content": "desktop-path-ok"},
        {"role": "user", "content": "继续"},
    ])
    assert "ignored background" not in delta and "hello there" not in delta
    assert "hi" in delta and "继续" in delta and "desktop-path-ok" in delta
    assert "助手的工具调用" not in delta and "XXXXX" not in delta, "助手工具调用不应回传"
    print(f"  [PASS] 增量提示词只含新增消息且不回传助手工具调用: {len(delta)} chars")

    noisy_tool = (
        "[P:Normal] Q1 题目正文\n[P empty]\n[P:List Paragraph] 属性 name\n"
        "javac : The term 'javac' is not recognized\n"
        "+ CategoryInfo          : ObjectNotFound: (javac:String) [], CommandNotFoundException\n"
        "+ FullyQualifiedErrorId : CommandNotFoundException\n"
        "    + ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
        + "中间内容\n" * 1500
    )
    compact = _compact_tool_result(noisy_tool, 3000)
    assert "[P empty]" not in compact and "[P:Normal]" not in compact
    assert "CategoryInfo" not in compact and "FullyQualifiedErrorId" not in compact
    assert "题目正文" in compact and "javac" in compact
    assert len(compact) <= 3100, len(compact)
    print(f"  [PASS] 工具返回已去噪并截断: {len(noisy_tool)} -> {len(compact)} chars")

    filler = [{"type": "function", "function": {
        "name": f"filler_{i}", "description": "Search and fetch data from the web. " * 6,
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}
        for i in range(50)]
    core = [{"type": "function", "function": {
        "name": n, "description": "core tool",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}
        for n in ("write", "read", "edit", "pwsh")]
    listed = _compact_tools(filler + core)  # 核心工具排在最后，旧逻辑会被截掉
    for n in ("write", "read", "edit", "pwsh"):
        assert f"- {n}(" in listed, f"核心工具 {n} 被字数上限截掉了"
    print("  [PASS] 核心工具(write/read/edit/pwsh)优先列入清单，不会被截断而诱发幻觉工具名")


if __name__ == "__main__":
    print("protocol selftest:")
    selftest()
