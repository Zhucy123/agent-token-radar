# agent-token-radar · 设计与实现说明

本地多 Agent **Token 用量**统计工具。只读扫描本机 AI Agent 的会话日志 / SQLite，归一化入库，提供 CLI 报表与本地 Web 仪表盘。

> **Token 是测量值，费用是推导值。** 后者等于「Token × 厂商官方标价」，永远不会比价格表更可信。
> 因此费用永远和覆盖率一起显示；查不到官方价的模型标「无公开价」而不是算成 0。
> 详见 [`PRICING-NOTES.md`](PRICING-NOTES.md)。

这份是**技术文档**（为什么这么实现、证据在哪），面向维护者。
想先跑起来看 [`README.md`](README.md)。

## 参考来源

本项目不是从零发明的，三个方向各有明确的参照对象。每处「为什么这么实现」都标了出处，
方便后来者复核——尤其是那些**反直觉但有实测证据**的决定。

### 1. [cclank/tokei](https://github.com/cclank/tokei) — Agent 覆盖面与解析规格

借鉴的部分：

| 借鉴点 | 具体做法 | 出处 |
|---|---|---|
| **Agent 覆盖面** | 25 个适配器全部对齐 tokei 的支持列表；tokei 不支持的（Cursor、Grok Bot、Doubao Work）不擅自新增 | tokei README「支持的工具」 |
| **两种 input 语义** | `Additive`（input 不含 cache，直接相加）与 `Inclusive`（input 已含 cache，需减）；每个适配器显式声明，不靠猜 | tokei 各采集器的 `input` 处理；本仓库 `adapters/_usagekit.py` |
| **reasoning 是 output 的子集** | 单独报告，**绝不**加进 total；唯一例外 MiniMax 只在 `raw.total == in+out+cr+cw+reason` 时才认 | tokei README + `_usagekit.py` |
| **Copilot 只统计 output** | 上游 SDK 的 input/cache 包含关系未定论，`in`/cache 记 0，`reasoning` 从 output 拆出，宁可留空也不猜 | tokei README「GitHub Copilot 计量覆盖」、[copilot-sdk#2834](https://github.com/github/copilot-sdk/issues/2834) |
| **逐 Agent 解析要点** | Pi/Prime 同构、CodeBuddy 三处 usage 挂点、Qoder 的 input 是 inclusive（与 Claude 相反）、Qwen 的 per-request 必须跳过 summary、Kimi 两代协议、Muse 时间戳 µs/ms 判据、Devin 兄弟节点去重、Grok Build 在应用日志而非会话文件、OpenClaw 的 `event_zstd` 多帧 | 见 [`TOKEI-NOTES.md`](TOKEI-NOTES.md) 第 3 节，逐条记录了 tokei 的实现与原话依据 |
| **去重键设计** | `event_key` 用各 Agent 天然的请求 id（Claude 用 `message.id`、Codex 用 `response_id`、Qoder 用 `request_id`），而不是行号 | tokei 的去重实现 |
| **项目维度（本轮新增）** | 按 Agent 的工作目录统计用量 | tokei README「项目追踪」 |
| **Agent 官方图标** | 25 个全部官方标识，无一自绘；来源与取证 URL 记在 `_build_icons.py` 头部 | tokei 的图标资源 |
| **参考费用估算（本轮新增）** | 按厂商官方标价把 Token 折算成金额，并强制同时显示「覆盖率」；无公开价的模型标「无公开价」而非 0；分时计价的厂商按每条请求自身的时间判峰谷 | tokei 的 `docs/automatic-pricing.md` |

**费用这块刻意做得比 tokei 保守**（本项目的定位差异）：

tokei 用 OpenRouter 目录覆盖 317 个模型，缺价时**沿用同档位上一版或兜底价**，并标「估算参照」。
本项目**不跟随**这套兜底：查不到官方价就不算，代价是覆盖率停在 96.5% 而不是 100%。
理由写在 [`PRICING-NOTES.md`](PRICING-NOTES.md)——一个编出来的价格比一个承认的缺口更糟。
定价实现与逐模型来源见 `agent_token_radar/pricing.json` 和 `agent_token_radar/pricing.py`。

**刻意不跟随 tokei 的部分**（本项目的定位差异）：

- **不做额度查询**。tokei 的额度卡要复用登录态或读 Keychain + 调官方 API；
  本项目对第三方目录严格只读。价格表是手工维护的静态数据，不是联网抓的。
- **唯一的联网是版本更新检查**（`updates.py`）。它只向 GitHub 问「最新 tag 是什么」，
  结果缓存 24 小时、每天最多一个请求、失败静默降级为「无法检查」。
  不发机器标识、不发使用数据、不发数据库内容。这是全项目唯一的外网出口，
  所以刻意隔离在单独模块里，`grep updates` 就能审完全部。
- **不做 macOS 菜单栏 / 多设备同步 / 年度回顾**。这些是 GUI 应用能力，本项目是 CLI + 单页 Web。

### 2. [magpie](https://magpie.im) 本机网关 — 会话标题与 provider 的真实来源

本机 Claude Code / Codex 的流量经过 magpie 网关（`127.0.0.1:3425`）。这个网关自己的记录，
比任何 Agent 日志都更接近「真实发生了什么」，因此被当作**实测来源**使用。

| 借鉴点 | 具体做法 | 文件 |
|---|---|---|
| **会话标题怎么读** | DSH 标题在 `storages/cost-meter/ledger.json` 的 `days.<日期>.sessions[]`（`id` + `title`）；WorkBuddy 在 `workbuddy.db` 的 `sessions` 表；Gemini 在 `conversation_summaries.db` | 见下方「会话标题」 |
| **provider 实测优先** | Claude Code 日志**没有** provider 字段，但它的 `message.id` 就是 magpie 的 `responseId`，逐请求对上即可拿到真实 provider | `provider_sources.py` |
| **路由账本优先于导出日志** | magpie 的 `routing/*.jsonl[.gz]` 是逐请求路由账本，`usage[0].provider` + `usage[0].model` 记为「谁答了、答了什么」，比 `usage.jsonl` 更权威 | `provider_sources.py::_iter_routing_records` |
| **路由别名问题** | Claude Code 记的是**别名**（`claude-sonnet-5`），网关实测的真实模型是 `deepseek-v4.1-flash`；保存 `served_model` 并把别名降为副标题 | `store.py::by_model` |
| **读哪些 Agent** | magpie 的 `sessionReads.agents` 列了它读的 7 个 App，本项目据此补齐了 WorkBuddy / Gemini 的标题 | `adapters/workbuddy.py`、`adapters/gemini.py` |

**证据强度**（不是字符串撞上了）：587 条匹配请求的 input/output token 与 magpie 记录**逐条完全一致，0 条不符**。
Claude Code 侧 591 条请求命中 587 条（99.3%）。

### 3. 本项目的取舍原则

三个来源冲突时，按这个顺序：

1. **Agent 自己的记录**说了算（DSH 的 `request/header.config.provider` 就是它自己写的）；
2. **本机网关实测**次之（magpie 观察到了真实请求，但它是旁证）；
3. **模型名推断**最后，且**必须在界面上标注为「推断」**，绝不能伪装成实测。

推断不出来的就**留空**——账本里一个错数比缺一条更糟。


## 快速开始

有两种用法，按需选择一个。

### 方式 A：pip 安装（推荐给别人用）

```bash
# 只要 CLI（scan / stats）——纯标准库，不装任何第三方包
pip install "agent-token-radar @ git+https://github.com/Zhucy123/agent-token-radar"

# 还要 Web 面板，就带上 [web] 附加依赖（fastapi + uvicorn）
pip install "agent-token-radar[web] @ git+https://github.com/Zhucy123/agent-token-radar"
```

装好之后在**任意目录**都能用（不再是 `python -m`，也不用管启动目录）：

```bash
agent-token-radar scan
agent-token-radar stats --range 7d --by agent
agent-token-radar serve
```

数据库写在用户数据目录，不写进 `site-packages`：

| 平台 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\agent-token-radar\token_radar.db` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/agent-token-radar/token_radar.db` |

`AGENT_TOKEN_RADAR_DB=/path/to/x.db` 可以覆盖。

> **`serve` 必须装 `[web]`。** 只装了基础包时，`serve` 会直接告诉你
> `serve needs fastapi + uvicorn: pip install fastapi uvicorn` 然后退出——
> 这是有意的：不想为了数 Token 就让 CLI 背上 Web 框架依赖。

### 方式 B：直接用仓库（不改环境）

> 下面的命令都在 **`agent_token_radar/` 的上一级目录**运行。
> 换机器、给别人用、以及那唯一的启动目录坑，见 [换一台机器 / 给别人用](#换一台机器--给别人用)。

```bash
# 1) 一次增量扫描（写入 ./data/token_radar.db）
python -m agent_token_radar scan

# 2) 终端报表
python -m agent_token_radar stats --range 7d --by agent
python -m agent_token_radar stats --range all --by model    # Agent / Model / Provider
python -m agent_token_radar stats --range all --by project  # 项目（工作目录）维度
python -m agent_token_radar stats --range all --by model --json

# 3) 本地 Web 面板（默认 http://127.0.0.1:8770，每 30s 后台增量扫描）
pip install fastapi uvicorn     # 只有 serve 需要
python -m agent_token_radar serve
```

**端口策略：8770 优先，被占就自动顺延。**

```
8770 被占 → 8771；8771 也占 → 8772；依此类推
```

不需要加任何参数，也不会再抛 `[Errno 10048]` 崩溃。启动时会明确告诉你换到了哪个端口：

```
port 8770 is in use -> using 8772 instead
agent-token-radar dashboard: http://127.0.0.1:8772
```

想固定死在某个端口、被占就报错退出，加 `--no-fallback`；想换起点用 `--port <n>`。

**语言切换（中 / 英）**：

```bash
python -m agent_token_radar stats --range 7d --by agent --lang en   # 终端英文
```

Web 面板右上角点 `中文 / EN` 切换，或直接用 URL 参数：
`http://127.0.0.1:8770/?lang=en`（可分享、可脚本化）。

中文模式只显示中文，英文模式只显示英文；但 `Token` / `Agent` / `Model` /
`Session` / `Cache` / `Input` / `Output` / `Reasoning` 等专有名词**两种语言下都保持英文**
（中文界面写「按 Agent 分布」，不写「按智能体分布」）。

**Agent 图标**：25 个已启用适配器全部使用**官方标识**，内联在 `icons.py`（离线可用，无网络请求）。
`icons.py` 的 `ICONS` 字典共有 37 条条目，因为同一厂商的别名、旧名和未启用变体也收在同一张表里（例如同一家的多个产品名共用一个官方 SVG）；「37 条条目」与「25 个适配器」不是矛盾，前者是资源表规模，后者是实际启用的 Agent 数。
图标由 `_build_icons.py` 生成，脚本头部逐条记录了取证 URL，可用 `--offline` / `--refresh` / `--check` 重现与校验。

- **simple-icons 官方 SVG**（CC0，厂商自提交）：Claude、CodeBuddy、Copilot、Gemini、OpenCode、
  Qwen Code、Kimi、MiniMax、DeepSeek Harness、Windsurf、Zed、Cursor、Mistral、Ollama。
- **厂商站点的 SVG**：Codex(OpenAI)、Devin、Pi（抽取 path）；Muse、OpenClaw、WorkBuddy、
  Command Code（整份保留，因为依赖渐变 / clipPath / 父级 transform）。
- **厂商站点的位图**：Grok、Hermes、MiMoCode、Prime、ZCode、Qoder 全系、Doubao。

> 三个必须留意的点：① `hermes` 在 simple-icons 命中**爱马仕**、`pi` 命中**数学符号 π**，
> 同名不同物，已排除；② Command Code 的 SVG 是 potrace 输出，字形包在父级 `transform` 里，
> 只抽 path 会渲染成残缺一角，因此整份保留；③ 部分官方图标自带不透明底衬，
> 这类条目带 `tiled: true`，深色主题下由 CSS 补同色衬底，避免出现刺眼白块。

**深色模式**：近黑色的品牌标记（Cursor / Copilot / Kimi 等）带 `dark_color`，
在深色主题下自动换成厂商的浅色版；`kind="svg"` 与位图通过 `<img>` 原生解码，
完整保留渐变与蒙版。

环境变量：
- `AGENT_TOKEN_RADAR_DB`：自定义数据库路径（默认 `<项目>/data/token_radar.db`）
- `DSH_HOME` / `CODEX_HOME` / `CLAUDE_CONFIG_DIR` / `WORKBUDDY_HOME` / `HERMES_HOME` / `TOKEI_GEMINI_DIR`：覆盖各 Agent 的数据根目录

## 依赖

| 组件 | 依赖 |
|---|---|
| 采集 / 存储 / CLI | Python 3.11+ **标准库**（`sqlite3`, `json`, `datetime`, `argparse`…） |
| DSH 会话日志解压 | Python 3.14+ 标准库 `compression.zstd`；3.11–3.13 可选 `pip install zstandard` |
| Web 面板 | `fastapi`, `uvicorn`（仅 `serve` 需要） |
| 前端 | 单页原生 HTML/CSS/JS，图表为**纯 Canvas2D 自绘**（无 CDN、无第三方库） |

无前端构建链、无打包步骤。

## 换一台机器 / 给别人用

**不需要 AI 介入，也不需要任何配置。** 数据是当场从各 Agent 自己的记录里读出来的，
没有预置数据、没有要填的表、没有联网校验。

```bash
pip install fastapi uvicorn     # 只有 serve 需要；只用 scan / stats 的话一个都不用装
cd agent_token_radar的上一级目录  # ← 唯一容易踩的坑，见下
python -m agent_token_radar serve
```

打开 `http://127.0.0.1:8770` 就会看到本机数据。**首次启动即自动扫描**（`serve` 启动时
先跑一次），所以打开就有数字，不用先点刷新。

### 只有一条真正的坑：必须在「包的上一级」目录运行

`python -m agent_token_radar` 要求 `agent_token_radar/` 在当前目录下能被找到。
在包**内部**运行会得到 `No module named agent_token_radar`：

```bash
cd agent_token_radar && python -m agent_token_radar scan   # ✗ 报错
cd ..            && python -m agent_token_radar scan       # ✓
```

不想记这条的话，直接跑文件也行（两种写法都已支持，且会自动把父目录加进 `sys.path`）：

```bash
python path/to/agent_token_radar/__main__.py serve
```

### 别人机器上会看到什么

扫到哪个 Agent 就显示哪个，**没装的 Agent 安静跳过**（`SOURCES=0`，不是错误）。
已实测过一台干净机器：25 个适配器全部注册、无一报错、数据库自动创建，一个 Agent 都没装时
面板显示 0 条而不是崩溃。

推断类标签在别人机器上会更常见：`provider` 的「实测」依赖本机 magpie 网关的逐请求路由账本，
别人没跑 magpie 就会退回「推断」或留空——**界面会明确标出来**，不会把猜测当事实。

### 数据落在哪里

默认写 `<包>/data/token_radar.db`，**在项目目录内**，所以整个文件夹拷走就是全部数据。
想放别处就设 `AGENT_TOKEN_RADAR_DB`。

**这个项目只写自己这一个目录**，对任何 Agent 的数据都只读：不修改、不删除、不新建。
扫描是幂等的（`event_key` 唯一 + UPSERT），重复点刷新不会翻倍。

### 跨平台注意

- 路径按 `USERPROFILE` / `HOME` + `APPDATA` / `XDG_*` 双套规则解析，Windows / Linux 通用。
- CLI 会把 stdout 强制转成 UTF-8：中文版 `stats` 在英文 Windows（cp1252 控制台）或重定向到
  文件时原本会 `UnicodeEncodeError` 直接崩，现已修掉。

## 架构

```
agent_token_radar/
├── base.py        # BaseAdapter ABC + UsageEvent / Checkpoint / ScanResult / 报告
├── paths.py       # Windows/Linux 跨平台只读路径解析
├── provider_sources.py # provider 解析：本机 magpie 实测 → 带标注的推断
├── readers.py     # JSONL 增量读、多帧 zstd 解码、只读 SQLite 临时副本
├── store.py       # SQLite: usage_events (event_key UNIQUE + UPSERT) / scan_checkpoints / session_titles
├── registry.py    # 自动发现 adapters/*.py 并注册 BaseAdapter 子类
├── scanner.py     # 编排：detect -> scan -> UPSERT -> save_checkpoints
├── cli.py         # scan / stats / serve（--lang zh|en）
├── web.py         # FastAPI + 后台定时增量扫描
├── i18n.py        # 中文 / 英文两套独立字符串表
├── icons.py       # 37 条图标条目（含别名/未启用变体），覆盖 25 个已启用适配器；由 _build_icons.py 生成
├── _build_icons.py # 图标构建脚本（源资产 → icons.py）
├── static/        # index.html / app.css / app.js
└── adapters/      # 每个 Agent 一个文件，新增工具只需新增一个文件
    ├── codex.py  claude.py  gemini.py  hermes.py  dsh.py
    ├── workbuddy.py  workbuddy_intl.py
    ├── pi_prime.py        # Pi + Prime（同构）
    ├── sqlite_family.py   # OpenCode/MiMoCode/ZCode/Devin/MiniMax/Qoder Desktop
    ├── qoder_cli.py       # Qoder CLI + CN
    ├── codebuddy.py  cmdcode.py  kimi.py  muse.py  qwencode.py
    ├── grok.py  openclaw.py  copilot.py
    └── _common.py / _helpers.py / _usagekit.py / _jsonl_base.py / _protobuf.py
        （下划线开头，不注册）
```

**新增适配器**：在 `adapters/` 放一个含 `class XxxAdapter(BaseAdapter)` 的 `.py` 文件即可自动注册，无需改任何中心列表。必须实现 `name`、`detect()`、`scan(checkpoint)`，并保证 `event_key` 稳定（重复扫描 UPSERT 不翻倍）。下划线开头的模块 / 类会被跳过，方便写共享基类。

17 个新适配器里，多数只需继承 `JsonlAdapter` 或 `BaseAdapter` 并实现一个 `parse_record` / 一段 SQL——
字段名容错由 `_usagekit.pick_usage()` 统一处理，inclusive/additive 两种 input 语义由
`TokenBuckets.inclusive()` / `.additive()` 显式声明，避免复制粘贴出错。

## 已适配的 25 个 Agent

全部来自 [cclank/tokei](https://github.com/cclank/tokei) 已支持的范围。**表内 7 个在本机实测过**（✅），
其余按 tokei 的解析逻辑实现，本机未安装无法实测（⏳）。

| Agent | 数据源 | 格式 | 权威记录 | event_key |
|---|---|---|---|---|
| ✅ Codex | `~/.codex/sessions/**/rollout-*.jsonl` | JSONL | `token_usage_record`→`payload.usage` | `codex\|session\|response_id\|ordinal` |
| ✅ Claude Code | `~/.claude/projects/**/*.jsonl` | JSONL | `assistant`→`message.usage` | `claude\|message.id` |
| ✅ Gemini / Antigravity | `~/.gemini/*/conversations/*.db` | SQLite+**protobuf BLOB** | `gen_metadata.data` | `gemini\|会话\|idx` |
| ✅ Hermes | `%LOCALAPPDATA%/hermes/state.db` | SQLite | `session_model_usage` | `hermes\|session\|model\|task\|last_seen` |
| ✅ WorkBuddy | `~/.workbuddy/projects/**/*.jsonl` | JSONL | `providerData.rawUsage` | `workbuddy\|id` |
| ✅ WorkBuddy AI | `~/.workbuddy-ai/projects/**/*.jsonl` | JSONL | `providerData.rawUsage` | `workbuddy-intl\|id` |
| ✅ DSH | `~/.dsh/sessions/**/session.vN.jsonl.zstd` | 多帧 zstd JSONL | `assistant/message`→`data.usage` | `dsh\|header.id\|seq` |
| ⏳ OpenClaw | `~/.openclaw/state/openclaw.sqlite` | SQLite（`event_json`/`event_zstd`） | `message.role=assistant`→`usage` | `openclaw\|sid\|event id` |
| ⏳ Pi Coding Agent | `~/.pi/agent/sessions/**/*.jsonl` | JSONL | `type=message`→`usage` | `pi\|sid\|record id` |
| ⏳ Prime Agent | `~/.prime/agent/{sessions,session-artifacts}/**/*.jsonl` | JSONL（同 Pi） | `type=message`→`usage` | `prime\|sid\|record id` |
| ⏳ CodeBuddy Code | `~/.codebuddy/projects/**/*.jsonl` | JSONL | `message/providerData.usage` | `codebuddy\|messageId` |
| ⏳ OpenCode | `~/.local/share/opencode/opencode.db` | SQLite | `session_message`/`message`→`tokens` | `opencode\|msg id` |
| ⏳ Qwen Code | `~/.qwen/usage/token-usage-*.jsonl` + `usage_record.jsonl` | JSONL | `schemaVersion=1` / `version=1` | `qwencode\|id` |
| ⏳ Qoder CLI | `~/.qoder/projects/**/*.jsonl` | JSONL | `type=assistant`→`usage` | `qodercli\|request_id` |
| ⏳ Qoder CN | `~/.qoder-cn/projects/**/*.jsonl` | JSONL | 同上 | `qodercli-cn\|request_id` |
| ⏳ Qoder Desktop | `…/Qoder/SharedClientCache/cache/db/local.db` | SQLite | `chat_message.token_info` | `qoder-ide\|id` |
| ⏳ Kimi Code | `~/.kimi-code/sessions/**/wire.jsonl` | JSONL | `usage.record`/`StatusUpdate` | `kimi\|sid\|ts\|model` |
| ⏳ Muse Code | `~/.local/share/muse/sessions/**/session.jsonl` | JSONL | `runtime.session`→`model_completed` | `muse\|source_run_record_id` |
| ⏳ Command Code | `~/.commandcode/projects/*/*.jsonl` | JSONL | `role=assistant`→`usage` | `cmdcode\|sha(id+ts)` |
| ⏳ ZCode | `~/.zcode/cli/db/db.sqlite` | SQLite | `model_usage` | `zcode\|id` |
| ⏳ MiMoCode | `…/mimocode/mimocode.db` | SQLite（同 OpenCode） | `session_message`→`tokens` | `mimocode\|msg id` |
| ⏳ Devin | `~/.local/share/devin/cli/sessions.db` | SQLite | `message_nodes.chat_message`→`metadata.metrics` | `devin\|hash` |
| ⏳ MiniMax Code | `~/.minimax/v2/sqlite/runtime-state.sqlite` | SQLite | `local_runtime_token_usage` | `minimax\|id` |
| ⏳ Grok Build | `~/.grok/logs/unified.jsonl` | JSONL | `msg=shell.turn.inference_done`→`ctx` | `grok\|sid:ts:loop:attempts` |
| ⏳ GitHub Copilot | `~/.copilot/**/events.jsonl` + sqlite | JSONL+SQLite | `session.shutdown`→`data.modelMetrics` | `copilot\|sid\|model\|ts` |

**两种 input 语义必须区分**（弄反会让 prompt 翻倍或漏算）：
- **Additive**（Claude / CodeBuddy / Command Code / Devin / Copilot）：`input` 不含 cache，直接相加。
- **Inclusive**（Pi / Prime / Muse / ZCode / Qoder / OpenCode / MiMoCode / Qwen / Grok）：`input` 含 cache，需减去 `cache_read + cache_write`。

**GitHub Copilot 是唯一 output-only 的**：上游 SDK 的 input/cache 语义尚未定论（见 tokei README 与
copilot-sdk#2834），因此 input/cache 记为 0、`reasoning` 从 `output` 中拆出，绝不猜测。

**未纳入的 2 个**（tokei 也只统计活动量、不统计 token）：
- **Grok Bot**：本地是 Chromium 持久化 blob，**无任何 token/usage 字段**，只有 turns/calls/tools。
- **Doubao Work（豆包办公）**：LevelDB 里只有会话时间戳，**无 token 字段**。

**Grok Build 本机说明**：目录存在且适配器已就绪，但本机 `unified.jsonl` 中
`shell.turn.inference_done` 记录数为 0（该文件主要是启动/鉴权日志），因此暂无量。

**Gemini 特别说明**：用量存在 protobuf BLOB 里且没有公开 schema，字段号是逆向得出
（`4.2` 输入 / `4.3` 输出含思考 / `4.5` 缓存读 / `4.9` 思考 / `4.10` 回复 / `19` 模型名）。
因此做了**上界校验**：数值越界一律丢弃而不是入库——账本里一个错数比缺一条更糟。

字段映射、去重策略与阶段 0 探测详情记录在各适配器文件头部与 `TOKEI-NOTES.md`；
逐 Agent 解析规格见 [`TOKEI-NOTES.md`](TOKEI-NOTES.md)。

## 健壮性设计

- **精准切行**：JSONL 只按 `\n` 切分。`str.splitlines()` 还会在 U+0085(NEL)、U+2028/2029 处断开，
  而 Agent 日志的 JSON 字符串里合法地含有这些字符——实测一个真实 DSH 会话含 66 个 NEL，
  用 `splitlines()` 会切出 68 条无法解析的「行」，改用 `split("\n")` 后为 0。
- **严格只读**：绝不写 / 移动 / 删除 / 修复任何第三方 Agent 目录中的文件。
- **SQLite 只读副本**：读取 Hermes 的 `state.db` 时先复制 DB + `-wal` + `-shm` 到临时目录，再以 `mode=ro` + `query_only=ON` 打开，用完删除副本，绝不锁死原软件。
  临时目录优先落在本项目自己的 `data/tmp/`，因为系统 `%TEMP%` 可能不可写；
  且刻意不使用 `tempfile.mkdtemp`（它建出的目录权限是 `0700`，部分受限环境下反而写不进去）。
  *这一条曾是真 bug*：早期版本固定用系统临时目录，导致 Gemini 与 Hermes 在本机被整体跳过。
- **UPSERT 去重**：`usage_events.event_key` 为 PRIMARY KEY，写入用 `ON CONFLICT ... DO UPDATE`。重复扫描、流式输出、fork 会话复制的前缀事件都不会让 Token 翻倍。
- **增量读取**：JSONL 按 `byte_offset` 增量；DSH 多帧 zstd 因帧边界不可由偏移推导（且崩溃恢复会原地重编码尾部），按 `size + mtime` 判定变化并整文件重读，仍靠 UPSERT 保证幂等。
- **容错**：损坏 / 不完整的 JSON 行、坏 zstd 帧、schema 漂移、权限错误全部记 warning 并跳过，不中断整体扫描；单个适配器失败也不影响其它适配器。
- **线程安全**：`Store` 为每个线程提供独立的 WAL 连接（`threading.local()`），Web 线程池与后台扫描线程互不冲突；扫描由互斥锁串行化。

## 数据模型

`usage_events`（16 列，只存 Token；费用由 `model_prices` 联表算出，不落库）：
`event_key (UNIQUE)`, `agent`, `model`, `provider`, `provider_source`, `served_model`,
`session_id`, `project`, `timestamp` (UTC 毫秒),
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`reasoning_tokens`, `total_tokens`, `source_file`

### provider：哪个后端提供了这个 model

`provider` **单独成列而不并进 model**，因为同一个 model 走不同来源是两笔不同的用量：
实测本机 `deepseek-v4-flash` 在 DSH 下走过 4 个不同 provider，合并就会看不出钱花在哪儿。

`provider_source` 记录**这个值是怎么来的**，界面上对应 `实测` / `自有后端` / `推断` 三种标记：

| 值 | 含义 | 界面 |
|---|---|---|
| `measured` | 记录本身就写了（DSH 的 `request/header`），或逐请求对上本机 magpie 网关的路由账本 | 绿色 `实测` |
| `product` | 产品结构决定的**自有后端**：Codex Desktop 只能连 Codex 自己的服务，WorkBuddy 只能连 WorkBuddy。这不是猜测 | 蓝色 `自有后端` |
| `inferred` | 记录里没有，纯粹按 model 名推断出来的，**可能错** | 橙色虚线 `推断` |
| `''` | 推断不出来，留空 | 无标记 |

**`product` 是为此专门拆出来的一档**：它既不是网关实测（没测到），也不是按模型名猜
（不靠名字），而是产品的架构事实。早先把它归进 `inferred` 会把 Codex 官方模型误标成
「推断」，归进 `measured` 又会和 magpie 实测的 `codex` 后端名冲突——`openai` 是 API 厂商名，
`codex` 是网关侧的后端名，同一个上游的两套命名，混用会凭空拆出两行。

一个分组只有在**全部**行都是实测时才显示「实测」——只要混进一行推断，整组降级为「推断」，
避免一条猜测藏在实测标签下面。页脚会报出总数（实测 / 自有后端 / 推断 / 未知各多少条）。

`served_model` 记录**后端实际服务的模型**。Claude Code 日志里写的是路由别名
（`claude-sonnet-5`），网关实测真正跑的是 `deepseek-v4.1-flash`；表格按真实模型分组，
把别名降为小字副标题，否则会凭空多出几个并不存在的模型。

### 缓存命中率：为什么必须「先聚合再相除」

汇总卡片和四张表都显示 `总量 / 缓存命中 / 未缓存输入 / 缓存读取 / 输出`。其中两个是**算出来的**，
不是存下来的，因为各 Agent 对 `input_tokens` 的定义不一致：

| 约定 | 谁在用 | 该 Agent 自己的 total | 未缓存输入 |
|---|---|---|---|
| **additive**（input 不含缓存） | DSH、Claude、Gemini、Hermes | `input + output + cache_read + cache_write` | 就是 `input` |
| **inclusive**（input 含缓存） | Codex、WorkBuddy | `input + output` | `input - cache_read - cache_write` |

Codex 实测一行：`input_tokens=572,797,230`，`cached_input_tokens=558,823,552`。
把那个 input 当「未缓存输入」显示会**虚高 41 倍**（真实只有 13,973,678）。

两种约定都满足同一个恒等式，所以只需一条公式，**不需要迁移数据、不需要按 Agent 分支**：

```
未缓存输入 = total_tokens − output_tokens − cache_read_tokens − cache_write_tokens
```

已在 73,864 行上验证：每一行代入都成立，**没有一行算出负数**，且
`未缓存 + 缓存读取 + 缓存写入 + 输出` 与 `total_tokens` **分毫不差**。

命中率：

```
缓存命中 = 缓存读取 ÷ (未缓存输入 + 缓存读取)
```

**必须写成 `SUM(缓存) / (SUM(未缓存) + SUM(缓存))`，不能写 `AVG(每行比率)`。**
后者会平均一堆比率：一个空 prompt 的行贡献 0.0，把总数拖下去——实测得到 **0%**，
而真实值是 **96.8%**。`cache_write` 也不进分母：它是「写一次」的成本，不是重新读的输入。

### project：Token 花在哪个项目上

`project` 是 Agent 运行时的工作目录。这一维回答的是 token 总数回答不了的问题：
「67M tokens」没有意义，「Untitled Game 里花了 67M」才有。

几处必须注意的取值方式：

| Agent | 来源 | 注意 |
|---|---|---|
| Claude Code | 每条记录的顶层 `cwd` | subagent 日志比会话深一层，用目录名会标错，所以只读记录里的 `cwd` |
| Codex | `session_meta` 的 **`payload.cwd`** | 在 `payload` 里面，不在顶层；且该记录位于文件开头，增量扫描永远读不到，必须独立读 |
| WorkBuddy | 每条记录的 `cwd` | |
| DSH | **会话目录名**，如 `--D--UEProjects-Untitled~0020Game--` | 目录名就是编码后的 cwd（`~XXXX` 是码点、`-` 是分隔符）；DSH 不把 cwd 写进任何记录 |

`project_label()` 把 `\` / `/`、盘符大小写、重复分隔符统一，保证「同一个项目」只有一行。
同一个目录用两个 Agent 打开会是**两行**——这是有意的，Agent 本身是「这笔开销」的一部分。

DSH 的目录名编码是**有损**的：`deepseek-harness` 里那个真实的连字符会被解码成路径分隔符。
它只当标签用，绝不回喂给文件系统，界面上悬停可看完整值。

### 旧库升级与回填

三个后加的列各有各的补法，因为**增量扫描永远到不了历史行**（检查点已经越过了它们）：

| 列 | 补法 | 为什么 |
|---|---|---|
| `provider` | `backfill_providers()`：从 `event_key` 里还原请求 id 再去查 | 请求 id 就存在 key 里，不用重读文件 |
| `provider_source` | 同上；已有 provider 的行直接标 `measured` | 那些值本来就是 Agent 自己写的 |
| `served_model` / `project` | `backfill_projects()`：**重新读源文件** | 这两个值只存在于日志里，没有别的来源 |

`provider_source = 'inferred'` 的行**每次扫描都会重算**（推断规则改好了，旧猜测就是错的）；
`measured` 的行**永不覆盖**（它来自证据，重新推导只会丢信息）。

`project` 的回填刻意做得很便宜：DSH 的目录名就是项目，不解压（780 个日志 / 2-3 GB 解一次要几分钟）；
JSONL 只在文件开头若干行里找 `cwd`（Codex 在第一条、Claude 很早就有），找到就停。

`scan_checkpoints`：`file_path (PK)`, `mtime`, `byte_offset`（扩展 `size`, `updated_at`）

计数字段语义互斥：`input_tokens` 为**未缓存**输入，缓存读/写单列；`reasoning_tokens` 是 `output_tokens` 的细分，**不重复计入** `total_tokens`。缺失 `total_tokens` 时按 `input + output + cache_read + cache_write` 计算。

> DSH 的 provider 是**按请求**声明的（`request/header`），位于该请求的用量记录**之前**。
> 增量扫描只回放新增尾部，读得到 message 却读不到更早的 header，因此历史数据需要一次全量重读
> 才能补上 provider。重读不会让总量翻倍——`event_key` + UPSERT 保证幂等。

## 还缺什么 / 与 tokei 的剩余差距

诚实清单。这些**没有做**，不是忘了：

| 缺口 | 说明 | 为什么先不做 |
|---|---|---|
| **17 个适配器未实测** | 本机只有 7 个 Agent 装了且有数据；其余按 tokei 规格移植，标 ⏳ | 目录不存在就没法验；故意不「造数据假装验证过」 |
| **Codex 的 2,979 条 provider 是推断** | 本机 Codex 大量走 `chatgpt.com` 直连，不经 magpie，没有路由账本可对 | 已标注为「推断」；要变成实测需 Codex 自身的 provider 记录（它只写 `openai`） |
| **Gemini / Hermes 没有项目维度** | 两者都是聚合表，schema 里没有 cwd | 不猜、不用别的字段凑 |
| **DSH 项目名有损** | 目录名编码分不清「真实连字符」和「路径分隔符」 | 编码本身的限制；只当标签用，不影响用量 |
| **参考费用估算（非账单）** | 按官方标价折算，只覆盖有公开价的模型；无价的如实标「无公开价」 | 已知边界，不是缺陷；见 [`PRICING-NOTES.md`](PRICING-NOTES.md) |
| **无额度 / 配额查询** | tokei 要联网 + 复用登录态 | 违背纯本地只读原则 |
| **无 Cursor** | 本机 `.cursor` 有 751 个文件，magpie 也读它的会话 | tokei 的**用量**覆盖面不含 Cursor（它的 Cursor 支持要联网调官方 API）；加它等于新增 Agent 而非补齐 |
| **无 Grok Bot / Doubao Work** | tokei 统计它们但**只有活动量** | 本地数据里根本没有 token 字段，接了也无数可数 |
| **无多设备同步 / 年度回顾 / 菜单栏** | tokei 是 macOS 菜单栏 App | 本项目定位是 CLI + 单页 Web，离线单机 |
| **热力图 / 6 种时间维度** | tokei 有「每周热力图」和今天/昨天/本周/上周/本月/今年 | 目前只有 4 档（今日/7 天/30 天/全部）；热力图可加，价值中等 |
| **按小时粒度** | tokei 有 `hours` / `day_hours` 作息分析 | 当前只到「天」；加它是纯展示层改动 |

## 测试

```bash
# 在仓库根目录（agent_token_radar/ 的上一级）运行
python -m unittest discover -s tests -p 'test_*.py'   # 255 个测试：桶映射 + 幂等性 + provider 来源 + 项目维度 + 切行 + 缓存命中率 + 端口顺延 + 费用估算
python -m agent_token_radar scan                       # 全量扫描冒烟
python _build_icons.py --check                         # 确认 icons.py 与源规格一致（在 agent_token_radar/ 内）
```

Mock 样本全部为合成数据，不含任何对话正文、Key 或真实会话 ID。

测试用自己的临时目录（`tests/_tmp.py`），**刻意不用** `tempfile.TemporaryDirectory`：
后者建出的目录权限是 `0700`，在受限环境下反而写不进去，会让测试在碰业务代码之前就失败。

费用相关的测试在 `tests/test_cost.py`：价格表解析、别名匹配、未定价记 NULL 而非 0、
覆盖率分母、「`gpt-6.1-terra` 不得落进 `gpt-6.1-sol` 的价」这条边界，
以及分时计价的全部边界——窗口左右开闭、周末、法定节假日、平价厂商不受影响、
峰谷混在一条聚合里仍逐行计价。

**分时计价为什么在 SQL 里做**：费用是七个聚合查询各自 `SUM()` 出来的，
Python 侧无法逐行介入。窗口与节假日落在 `price_peak` / `price_peak_skip`
两张小表里，`pricing.PEAK_HOURS_SQL` 只写一份通用判断，
所以「按每条请求自己的时间」这件事在所有维度（Agent / Model / 项目 / 会话 / 天）
自动一致，不会有某个报表忘了区分峰谷。

## 实测结果（本机，2026-11-21）

一次全量扫描：**7 个适配器有量**（Claude / Codex / DSH / Gemini / Hermes / WorkBuddy /
WorkBuddy AI）、约 900 个数据源、73,571 行入库，**0 条 warning**。

| 维度 | 覆盖率 | 说明 |
|---|---|---|
| `project` | 73,571 行里 **70,178 行**（95.4%） | Gemini / Hermes 的聚合表没有 cwd 概念，如实留空 |
| `provider` | 69,903 实测 / 3,576 推断 / 8 未知 | 未知的几条 model 名本身为空，不猜 |

按项目口径的 Top 5（真实路径已脱敏，只保留结构）：

| 项目 | Agent | Token |
|---|---|---|
| `C:/Users/<user>/workspace` | dsh | 8,114,776,889 |
| `D:/UEProjects/<project>` | dsh | 2,975,457,310 |
| `…/<workspace>/默认工作区` | dsh | 1,964,992,335 |
| `…/workspace/<proj>/Workbench/Final` | dsh | 1,426,912,905 |
| `D:/UEProjects/<project>` | codex | 498,197,875 |

DSH 单个 4.7 MB / 1738 帧会话日志正确解出 4,016 行、0 条损坏 JSON。

> 本轮修掉的真 bug：① Gemini 与 Hermes 因系统临时目录不可写而**被整体跳过**
> （现改为优先使用项目自己的 `data/tmp/`，且不再用权限为 `0700` 的 `mkdtemp`）；
> ② `serve` 端口被占用时抛裸 `[Errno 10048]`，现改为给出口径明确的提示与可用端口；
> ③ 项目维度回填时只读了顶层 `cwd`，而 Codex 把它放在 `payload.cwd` 里，
> 导致 **3,359 条 Codex 记录全部没项目**（Claude 同一段代码却是好的，所以没被立刻发现）；
> ④ 旧的推断值不重算，规则改了也不生效——现在推断行每次重算，实测行永不覆盖。
