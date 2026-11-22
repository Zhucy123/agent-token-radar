# 参考调研：cclank/tokei 的 Agent 数据接入方式

> 来源：<https://github.com/cclank/tokei>（Python 采集器 `usage.30s.py`，19,083 行）
> 本项目的定位差异：**不做额度查询、无任何网络请求**。
> 费用用官方标价做参考估算（不做 tokei 那种兜底价），见 [`PRICING-NOTES.md`](PRICING-NOTES.md)。

## 1. 全部 Agent → 数据源（tokei 支持范围）

“本机”列表示该目录在这台 Windows 机器上是否存在。

| # | Agent | 数据源 | 本机 |
|---:|---|---|:--:|
| 1 | Claude Code | `~/.claude/projects/<proj>/<session>.jsonl` | ✅ |
| 2 | Codex CLI | `~/.codex/sessions/YYYY/MM/DD/*.jsonl` | ✅ |
| 3 | Gemini / Antigravity | `~/.gemini/{antigravity,antigravity-cli,antigravity-ide,gemini-cli}/conversations/*.db` | ✅ |
| 4 | Hermes | `~/.hermes/state.db` | ✅ |
| 5 | WorkBuddy | `~/.workbuddy/projects/<proj>/*.jsonl` | ✅ |
| 6 | WorkBuddy AI | `~/.workbuddy-ai/projects/<proj>/*.jsonl` | ✅ |
| 7 | DeepSeek Harness | `~/.dsh/sessions/**/session.jsonl.zstd` | ✅ |
| 8 | Grok Build | `$GROK_HOME/logs/unified.jsonl`（默认 `~/.grok`） | ⚠️ 有文件无记录 |
| 9 | OpenClaw | `~/.openclaw/state/openclaw.sqlite` | ❌ |
| 10 | Pi Coding Agent | `~/.pi/agent/sessions/<proj>/*.jsonl` + `~/.omp/...` | ❌ |
| 11 | Prime Agent | `~/.prime/agent/sessions/*.jsonl` + `session-artifacts` | ❌ |
| 12 | CodeBuddy Code | `~/.codebuddy/projects/**/*.jsonl` | ❌ |
| 13 | OpenCode | `~/.local/share/opencode/opencode.db` | ❌ |
| 14 | Qwen Code | `~/.qwen/usage/token-usage-*.jsonl` + `~/.qwen/usage_record.jsonl` | ❌ |
| 15 | Qoder CLI | `~/.qoder/projects/**/*.jsonl` | ❌ |
| 16 | Qoder CN | `~/.qoder-cn/projects/**/*.jsonl` | ❌ |
| 17 | Qoder Desktop | `…/Qoder/SharedClientCache/cache/db/local.db` | ❌ |
| 18 | Kimi Code | `~/.kimi-code/sessions/**/wire.jsonl`（兼容 `~/.kimi`） | ❌ |
| 19 | Muse Code | `~/.local/share/muse/sessions/**/session.jsonl` | ❌ |
| 20 | Command Code | `~/.commandcode/projects/*/*.jsonl` | ❌ |
| 21 | ZCode | `~/.zcode/cli/db/db.sqlite` | ❌ |
| 22 | MiMoCode | `…/mimocode/mimocode*.db` | ❌ |
| 23 | Devin | `~/.local/share/devin/cli/sessions.db` | ❌ |
| 24 | MiniMax Code | `~/.minimax/v2/sqlite/runtime-state.sqlite` | ❌ |
| 25 | GitHub Copilot | `~/.copilot/**/events.jsonl` + `assistant_usage_events` | ❌ |
| — | Grok Bot | `…/Grok Bot/sand-client-persistence/*.blob` | ❌ **无 token 字段** |
| — | Doubao Work | `…/DoubaoWork/**/IndexedDB/…leveldb` | ❌ **无 token 字段** |
| — | Cursor | `state.vscdb` + **官方 usage API（联网）** | 仅登录态 |
| — | Zed / Sub2API / z.ai | Keychain + 联网 API 或额度 | ❌ |

结论：**tokei 支持 27 个，本项目接入了其中 25 个**；剩 2 个（Grok Bot / Doubao Work）tokei 也只统计
活动量（turns/calls/tools），**本身就没有 token 字段**，接入也无 token 可得。

## 2. 关键：两种 input 语义（弄反必错）

| 语义 | `input` 含义 | 处理 | 使用它的 Agent |
|---|---|---|---|
| **Additive** | 不含 cache；total = in+out+cr+cw | 直接相加 | Claude、CodeBuddy、Command Code、Devin、Copilot、OpenClaw、Kimi |
| **Inclusive** | **已含** cache；需减 | `in = input - cr - cw` | Pi、Prime、Muse、ZCode、Qoder 全系、OpenCode、MiMoCode、Qwen、Grok Build |

另外 **reasoning 是 output 的子集**，全局规则：单独报告，**绝不加进 total**。
唯一的例外是 MiniMax——只有当 `raw.totalTokens == in+out+cr+cw+reason` 时才认 reasoning，
否则丢弃（证明不了就不记）。

## 3. 逐 Agent 解析要点

### 3.1 Pi Coding Agent / Prime Agent（同构）
```
{"type":"session","id":..,"cwd":..}                      -> 粘性 session/project
{"type":"model_change","provider":..,"modelId":..}       -> 粘性 model
{"type":"message","timestamp":"iso","message":{
   "role":"assistant","responseModel":..,
   "usage":{"input","output","cacheRead","cacheWrite","reasoning"}}}
```
Inclusive。Prime 的 `session-artifacts` 也要扫；子代理归因记录只是记账，不重复计。

### 3.2 CodeBuddy Code
usage 可能挂在 `message.usage` / `providerData.usage` / `providerData.rawUsage` 三处，键名版本间不一致
（`input_tokens`/`inputTokens`/`input`/`prompt_tokens` 都出现过）。当 `total == input+output` 时判定
input 为 inclusive。model 优先 `providerData.requestModelId`。

### 3.3 Qoder CLI / CN / Desktop
- CLI/CN：Claude 形状，但 **`input_tokens` 是 inclusive**（与 Claude Code 相反！）。去重键 `request_id`。
- Desktop：`chat_message.token_info` 是 JSON 字符串（`prompt_tokens`/`completion_tokens`/`cached_tokens`），
  `gmt_create` 是 epoch ms。

### 3.4 OpenCode / MiMoCode（同 schema）
```
tokens: { input, output, reasoning, cache: { read, write } }
time.created = epoch ms
```
Inclusive。表：V2 `session_message`（`type='assistant'`）；V1 `message`（`data` 是 JSON）。

### 3.5 Qwen Code
两套日志：per-request `token-usage-*.jsonl`（`schemaVersion=1`）与旧版 `usage_record.jsonl`（`version=1`）。
`inputTokens` **含** `cachedTokens`。**同一 session 若已在 per-request 文件中出现，summary 必须跳过**，
否则整会话翻倍。

### 3.6 Kimi Code
两代协议：
- 1.5：`agents/<a>/wire.jsonl` → `{"type":"usage.record","usage":{inputOther,output,inputCacheRead,inputCacheCreation}}`
- 1.0：`{"type":"StatusUpdate","payload":{"token_usage":{input_other,output,input_cache_read,...}}}`
`inputOther` 已是 fresh（additive）。根 `wire.jsonl` 会镜像 agent wire，需去重（本项目对同 session 目录
只取 agent wire）。

### 3.7 Muse Code
三层嵌套：`payload_type=="runtime.session"` → `payload.kind=="run"` → `payload.event.kind=="model_completed"`。
时间戳单位看量级：**µs > 1e14，ms > 1e11**。Inclusive。

### 3.8 Command Code
`role` 有时在顶层有时在 `message.role`；`usage` 键是 `inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens`。
Additive，无 reasoning 字段（记 0，不猜）。

### 3.9 ZCode
表 `model_usage`。`input_tokens` inclusive；`output = output_tokens - reasoning_tokens`，reasoning 单列。

### 3.10 Devin
表 `message_nodes.chat_message`（JSON）→ `metadata.metrics.{input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens}`。
Additive（与 Anthropic 同侧）。**同一回复会写进两个兄弟节点**，需按
`(session, created_at, model, 四个 token 值)` 去重。

### 3.11 MiniMax Code
表 `local_runtime_token_usage`。四个桶相加；reasoning 仅在 `raw.totalTokens` 能证明时保留。

### 3.12 Grok Build

**不在 session 文件里**，在应用日志 `logs/unified.jsonl`：
```
{"msg":"shell.turn.inference_done","sid":..,"ts":"iso",
 "ctx":{"prompt_tokens":N,"cached_prompt_tokens":N,
        "completion_tokens":N,"reasoning_tokens":N,"loop_index":N,"attempts":N}}
```
`in = prompt - cached`，`out = completion - reasoning`。记录本身无 model——tokei 从
`sessions/*/*/summary.json:current_model_id` 关联，本项目同样处理。
⚠️ 本机该 msg 计数为 **0**（日志主要是启动/鉴权），所以暂无量。

### 3.13 OpenClaw
`transcript_events` 表，正文可能是 `event_json`（明文）或 `event_zstd`（**多帧 zstd**，magic `28 b5 2f fd`）。
reuse 已有 `decompress_zstd_frames()`。从 `agent_databases` 表动态发现各 agent 的 DB 路径。

### 3.14 GitHub Copilot
**唯一 output-only**：`session.shutdown` 的 `data.modelMetrics.<model>.usage` 只取 `outputTokens`，
`reasoning` 从中拆出；in/cache 记 0。原因：上游 SDK 的 input/cache 包含关系未定论
（[copilot-sdk#2834](https://github.com/github/copilot-sdk/issues/2834)），猜一个数比留空更糟。

### 3.15 DeepSeek Harness —— model 与 provider 是两个字段
用量在 `assistant/message` 的 `data.usage`；**provider 不在这条记录上**，而是按请求单独声明：

```jsonc
// 每个请求开头（早于该请求的用量记录）
{"type":"request/header","seq":13,"data":{"header":{"config":{
    "provider":"workbuddy","model":"deepseek-v4.1-flash","reasoningEffort":"max"}}}}
{"type":"request/context","seq":14,"data":{"provider":"workbuddy",
    "model":"deepseek-v4.1-flash","contextWindow":1000000}}
{"type":"model/selection","data":{"provider":"tokenrhythm","model":"deepseek-flash"}}
```

解析规则：三种记录都刷新**粘性**的 `(provider, model)`；随后每条 `assistant/message`
优先取 `message.source.{provider,model,modelId}`，缺失才回落到粘性值。

⚠️ 这带来一个增量扫描陷阱：增量只回放新增尾部，能读到 message 却读不到它前面的 header，
于是历史行的 provider 为空。补齐需一次全量重读（删掉该 agent 的 checkpoint 即可），
总量不会翻倍——`event_key` + UPSERT 保证幂等。

实测本机 provider 取值：`tokenrhythm`、`workbuddy`、`workbuddy-ai`、`workbuddy-global`、
`opencode-go`、`modlens-opencode-go`、`deepseek-official`、`deepseek-modlens`、`xiaomi`、
`scnet`、`scnet-glm`、`gpt`、`chatgpt`、`ollama-local`。同一个 `deepseek-v4-flash`
在一次会话里走过 4 个不同 provider，合并显示就会丢掉来源信息。

⚠️ 另一个坑：DSH 日志的 JSON 字符串里含有 U+0085(NEL) 等字符，
用 `str.splitlines()` 会被切成碎片（一个真实会话 66 个 NEL → 68 条解析失败）。
必须只按 `\n` 切分。

## 4. 明确排除
| 来源 | 原因 |
|---|---|
| **Grok Bot** | 本地是 Chromium 持久化 blob，**无任何 token/usage 字段**，只有 turns/calls/tools/duration。tokei 也明确“不按文本量估算”。 |
| **Doubao Work（豆包办公）** | LevelDB 里只有 `create_time`/`update_time`，**无 token 字段**。 |
| **Cursor** | 只有 `state.vscdb` 登录态；token 需调官方 usage API（联网 + 读凭据），违背纯本地原则。 |
| **Zed / Sub2API / z.ai / QwenWork 额度** | 纯额度/联网来源，无本地 token 日志。 |

## 5. 图标来源与取证

25 个 adapter 全部使用**官方标识**（`official: True`），无一自绘。构建脚本 `_build_icons.py`
是唯一来源，`icons.py` 由它生成，脚本头部按 agent 逐条记录了取证 URL。

| 来源 | Agent |
|---|---|
| simple-icons 官方 SVG（CC0，厂商自提交） | Claude、CodeBuddy、Copilot、Gemini、OpenCode、Qwen Code、Kimi、MiniMax、DeepSeek Harness、Windsurf、Zed、Cursor、Mistral、Ollama |
| 厂商站点 SVG，抽取 path | Codex(OpenAI)、Devin、Pi |
| 厂商站点 SVG，整份保留（渐变 / clipPath / 父级 transform） | Muse、OpenClaw、WorkBuddy、Command Code |
| 厂商站点位图（64×64 PNG data URI） | Grok、Hermes、MiMoCode、Prime、ZCode、Qoder 全系、Doubao |

**几处必须留意的陷阱**（都已实测确认）：

- `hermes` 在 simple-icons 里命中的是**爱马仕**（`#0091CD`），`pi` 命中的是**数学符号 π**——
  同名不同物，一律不能当产品图标用。
- **Command Code** 的 pinned-tab SVG 是 potrace 输出，整个字形包在
  `<g transform="translate(0,700) scale(0.1,-0.1)">` 里。只抽 `d=` 会渲染成残缺的一角，
  必须整份 SVG 保留。
- **Devin** 的画布是 425×425，但图形只占 70..360，不收紧 viewBox 会显得极小。
- **Grok** 的 favicon.svg 是 Figma 导出，含 `foreignObject` + `backdrop-filter`，
  无法可靠还原，改用其 512px 应用图标位图。
- **Prime Intellect** 官网只有横版 wordmark（161×21），不适合方图标，用其 48px 应用图标。
- **Hermes** 的官方图标本身就是一张灰阶人物线稿（48×48 不透明方块），不是矢量标记。
- 部分官方图标自带**不透明底衬**（Command Code 黑底、Grok 圆角黑块、Hermes 白底）。
  这类条目带 `tiled: true`，前端在深色主题下会补一层与面板同色的衬底，
  避免在深色背景上出现刺眼的白色方块。

**渲染验证**：用 `resvg` 把每个图标在浅色与深色两套配色下各栅格化一次逐一目检
（36 个条目、0 个渲染失败），确认没有裁切、错位、仅显示局部或不可见的情况。

## 6. 本轮新增的文件

```
adapters/_usagekit.py      # TokenBuckets + 两种 input 语义 + 容错取值
adapters/_jsonl_base.py     # JsonlAdapter 基类（增量读 + 粘性状态）
adapters/pi_prime.py        # Pi + Prime
adapters/codebuddy.py  cmdcode.py  kimi.py  muse.py  qwencode.py
adapters/grok.py  openclaw.py  copilot.py
adapters/qoder_cli.py       # Qoder CLI + CN
adapters/sqlite_family.py   # OpenCode/MiMoCode/ZCode/Devin/MiniMax/Qoder Desktop
_build_icons.py             # 图标构建脚本（源资产 → icons.py，--offline/--refresh/--check）
_icon_cache/                # 已下载的源资产（离线重建用）
tests/test_new_adapters.py  # 17 个新适配器的映射 + 幂等测试
```
```
