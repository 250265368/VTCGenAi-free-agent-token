# VTCGenAi Free Agent Token

**Turn any logged-in web AI into a 100% OpenAI-compatible agent brain — with zero API bill.**

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20Compatible-10a37f.svg)](#openai-compatible-api)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6.svg)](#quick-start)
[![Zero Cost](https://img.shields.io/badge/API%20Cost-%240.00-brightgreen.svg)](#disclaimer)

> **The poor developer's supercomputer. One .exe. Infinite agent.**
>
> 你不需要再为 agent 的 token 账单发愁。VTCGenAi Free Agent Token 把浏览器里已经登录的网页版大模型，接管成一台全天候在线的智能体引擎——
> 函数调用、流式输出、工具循环、多站点热切换，全部开箱即用。

---

## Table of Contents

- [What is this](#what-is-this)
- [Why it exists](#why-it-exists)
- [Architecture](#architecture)
- [Features](#features)
- [Quick Start](#quick-start)
- [OpenAI Compatible API](#openai-compatible-api)
- [Supported Sites](#supported-sites)
- [Configuration](#configuration)
- [Project Layout](#project-layout)
- [Roadmap](#roadmap)
- [Disclaimer](#disclaimer)
- [License & Credits](#license--credits)

---

## What is this

VTCGenAi Free Agent Token（内部代号 **EAgent Bridge**）是一座桥。

桥的一头，是你浏览器里那些**已经登录、已经买了会员、已经忘了账单**的网页版大模型；
桥的另一头，是任何遵循 `OpenAI /v1/chat/completions` 协议的 Agent 框架、Harness、IDE 插件或自研脚本。

它用 Chrome DevTools Protocol 直接接管真实浏览器会话，把"人类在输入框里打字"这件事，
变成一次毫秒级、可编程、可并发的 API 调用。

它解决的不是"怎么调模型"，而是**"怎么把一个只会聊天的网页，改造成一台能自己干活、会调工具、永不疲倦的 Agent 主机"**。

## Why it exists

| | 传统 API 路线 | VTCGenAi Free Agent Token |
|---|---|---|
| 成本 | 按 token 计费，Agent 跑一晚能烧掉一个月工资 | **$0.00，跑穿地球也不涨** |
| 速率限制 | 429 / RPM / 并发上限 | **由你的浏览器决定，没有配额仪表盘** |
| 上下文 | 受模型窗口限制 | **增量上下文引擎，长任务逐轮续命** |
| 工具调用 | 部分模型不支持 | **协议层双向翻译，不支持也能支持** |
| 接入成本 | SDK、密钥管理、余额监控 | **一个 base_url 改完就走** |
| 站点切换 | 换一家就重写一遍代码 | **一行配置热切换 4 家引擎** |

一句话：**把免费的聊天窗口，升级成收费 API 的能力。**

## Architecture

```
┌──────────────────┐   OpenAI /v1/chat/completions (SSE / JSON)
│  Agent / Harness │ ─────────────────────────────────────────┐
│  (任何框架均可)   │                                          │
└──────────────────┘                                          ▼
                              ┌─────────────────────────────────────────┐
                              │        VTCGenAi Free Agent Token        │
                              │  ┌───────────────────────────────────┐  │
                              │  │ protocol.py  OpenAI ⇄ 网页纯文本   │  │
                              │  │   · tools schema 压缩              │  │
                              │  │   · tool_calls 反向解析            │  │
                              │  │   · DSML 原生标记兼容              │  │
                              │  ├───────────────────────────────────┤  │
                              │  │ server.py    会话池 / 增量上下文   │  │
                              │  ├───────────────────────────────────┤  │
                              │  │ panel.html   可视化控制台 :8300    │  │
                              │  └───────────────────────────────────┘  │
                              └───────────────────┬─────────────────────┘
                                                  │ CDP (Chrome DevTools Protocol)
                                                  ▼
                     ┌──────────────────────────────────────────────┐
                     │  你的 Edge / Chrome（真实登录态，真实会员）     │
                     │  DeepSeek · 豆包 · 腾讯元宝 · VTC            │
                     └──────────────────────────────────────────────┘
```

**核心设计取舍：** 不碰模型权重，不逆向加密，不做任何"破解"。
只用浏览器本来的调试协议——也就是开发者工具里那个 F12——去点击、输入、读取。
网页怎么服务人类，就怎么服务你的 Agent。

## Features

- **Zero-cost brain** — 复用已有网页会话，边际成本恒为 0。
- **OpenAI-compatible by design** — `/v1/chat/completions`、`/v1/models`，SSE 流式与非流式都支持，OpenAI SDK 直接指过来就能跑。
- **Function calling 翻译层** — 网页 AI 没有原生 `tools`，本项目在协议层把工具定义压成紧凑提示词，再把回答里的调用块还原成标准 `tool_calls`；连 DeepSeek 系的原生 `<｜DSML｜>` 标记也能识别。
- **增量上下文引擎** — `incremental` 模式下只把新增对话压进输入框，长链路 Agent 任务不会被 8k 输入框截断。
- **多站点热切换** — DeepSeek / 豆包 / 元宝 / VTC 四端配置化，改一个字段立即换脑。
- **内置可视化控制台** — 双击即开 `http://127.0.0.1:8300/`，切换站点、启动浏览器、自检、看日志全在图形界面里点。
- **零第三方依赖** — 核心仅用 Python 标准库，不需要 `pip install` 任何东西，打包成单文件 exe 即可分发。
- **可断点自愈** — 网页选择器变动、输出污染、代码围栏被 textContent 吃掉，都有容错回退。
- **单文件分发** — `dist/EAgentBridge.exe`，双击运行，无需 Python 环境。

## Quick Start

### 方式一：直接运行 exe（推荐）

```powershell
git clone https://github.com/250265368/VTCGenAi-free-agent-token.git
cd VTCGenAi-free-agent-token
.\dist\EAgentBridge.exe
```

控制台会自动打开：<http://127.0.0.1:8300/>

### 方式二：从源码运行

```powershell
python bridge\app.py
```

### 三步点火

1. **登录网页 AI** — 用 Edge/Chrome 打开 `chat.deepseek.com`、`www.doubao.com`、`yuanbao.tencent.com` 或 VTC，正常登录一次即可（登录态会被复用）。
2. **启动桥** — 双击 `EAgentBridge.exe`，在控制台里选站点、点「启动浏览器」、点「自检」。
3. **接入 Agent** — 在你的框架里把 base_url 指向 `http://127.0.0.1:8300/v1`：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8300/v1",
    api_key="not-needed",  # 本地桥接，默认不校验；需要时可自行配置
)

resp = client.chat.completions.create(
    model="web-ai",
    messages=[{"role": "user", "content": "帮我把这份日志里的异常按类型归类"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content or "", end="")
```

## OpenAI Compatible API

| Endpoint | Method | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | 主接口，支持 `stream`、`tools`、`tool_choice` |
| `/v1/models` | GET | 返回当前别名（默认 `web-ai`） |
| `/` | GET | 内置控制台面板 |
| `/health` 等自检端点 | GET | 面板内一键自检，用于排查登录态与选择器问题 |

**工具调用的完整闭环：**

```
Agent ──tools[]──▶ protocol.py ──紧凑提示词──▶ 网页 AI
Agent ◀──tool_calls── protocol.py ◀──调用块文本── 网页 AI
```

## Supported Sites

| 站点 | Key | 状态 |
|---|---|---|
| DeepSeek 网页版 | `deepseek` | 稳定 |
| 豆包 | `doubao` | 稳定 |
| 腾讯元宝 | `yuanbao` | 稳定 |
| VTC | `vtc` | 稳定 |

新增站点只需在 `sites.json` 里补一条 `url` + 三段脚本（打开 / 发送 / 关闭），无需改代码。

## Configuration

`config.json` 关键项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` / `listen_port` | `127.0.0.1` / `8300` | 桥接服务监听地址 |
| `site` | `vtc` | 当前使用的站点 |
| `cdp_port` | `9222` | 浏览器调试端口 |
| `model_alias` | `web-ai` | 对外暴露的模型名 |
| `max_prompt_chars` | `8000` | 单次输入上限，超出自动压缩背景 |
| `max_tool_result_chars` | `3000` | 单个工具结果长度上限（head + tail 截断） |
| `api_key` | 空 | 留空则不校验 `Authorization`；填了则要求 Bearer |
| `context_mode` | `incremental` | 增量上下文 / 全量 |
| `timeout_sec` / `fast_fail_sec` | `180` / `150` | 超时与快速失败阈值 |

## Project Layout

```
.
├── EAgentBridge.exe              # 单文件发布版
├── config.json                   # 主配置
├── sites.json                    # 站点定义
├── panel.html                    # 控制台（根目录副本）
├── harness-provider.example.yaml # 接入示例
├── README.md / LICENSE / .gitignore
├── bridge/                       # 核心源码
│   ├── app.py                    # 双击入口
│   ├── server.py                 # HTTP 服务 / 会话池 / 增量上下文
│   ├── protocol.py               # OpenAI ⇄ 网页文本 协议转换
│   ├── panel.html                # 控制台页面
│   ├── sites.json / config.json
│   ├── dist/                     # 打包产物（含 EAgentBridge.exe）
│   ├── build/                    # PyInstaller 中间产物（已加入 .gitignore）
│   └── scripts/                  # 站点脚本（open / send / close）
├── scripts/                      # 顶层站点脚本
└── upstream/EAgent/              # 上游 EAgent 源码（GPL-3.0，见 Credits）
```

## Roadmap

- [ ] 站点无头模式（headless）常驻
- [ ] 多站点并发与自动故障转移
- [ ] 会话快照与跨进程恢复
- [ ] macOS / Linux 适配
- [ ] 一键站点脚本录制器

## Disclaimer

本项目是**协议桥接与自动化工具**，不包含任何模型权重、不绕过任何付费墙、不破解任何加密。
它复用的是使用者**本人**已经合法获得的网页会话能力。

请遵守你所使用站点的服务条款。因高频调用、自动化访问导致的账号限制，
以及由此产生的任何后果，由使用者自行承担。请勿用于商业转售、大规模对外提供服务等侵权场景。

## License & Credits

**GPL-3.0** — 本项目为 [YaoGaoChaoLe/EAgent](https://github.com/YaoGaoChaoLe/EAgent)（GPL-3.0）的衍生作品，
同样以 GPL-3.0 授权发布，并保留上游署名。`upstream/EAgent/` 目录为上游源码快照，版权归原作者所有。

网络引擎：EAgent · 协议适配：VTCGenAi Free Agent Token

---

**If this saved you an API bill, give it a ⭐ — and go build something ridiculous.**
