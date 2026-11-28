# Agent Token Radar

**简体中文** ｜ [English](README.en.md)

统计本机各个 AI Agent 的 **Token 用量**，并按厂商官方标价给出**参考费用估算**。
只读扫描本机会话日志 / SQLite，归一化入库，提供 CLI 报表与本地 Web 仪表盘。

**25 个 Agent · 中英双语 · 本地数据 · 只读 · 仅在检查更新时联网**

<p>
<a href="#安装"><b>↓ 安装</b></a> ·
<a href="#使用"><b>↓ 使用</b></a> ·
<a href="#更新日志"><b>↓ 更新日志</b></a> ·
<a href="#参考了什么"><b>↓ 参考了什么</b></a> ·
<a href="#说明与边界"><b>↓ 边界说明</b></a>
</p>

![汇总卡片与消耗趋势](docs/app-zh-1.png)

> **Token 是测量值，费用是推导值。** 费用 = Token × 官方标价，可信度不会超过那张价格表。
> 因此界面**强制把覆盖率和金额一起显示**：没有公开标价的模型标成「无公开价」，绝不算成 0。
> 细节见 [PRICING-NOTES.md](PRICING-NOTES.md)。

---

## 界面

以下截图取自**实际打包的桌面程序**（数据就是本机真实扫描结果，非示例数据）。

**汇总与趋势**：总用量、缓存命中率、输入/输出/缓存读取/推理六个指标，加参考费用与覆盖率，下面是按 Agent 堆叠的每日消耗趋势。

![汇总卡片与消耗趋势](docs/app-zh-1.png)

**按 Model 分布**：每个 Agent 下的各 Model（及 Provider）用量，含每百万 Token 单价与峰谷标注。

![按 Model 分布](docs/app-zh-2.png)

**消耗最高的 Top Session**：会话标题取自各 Agent 自己的记录，能看到真实的会话名。

![消耗最高的 Top Session](docs/app-zh-3.png)

---

## 功能

| | |
|---|---|
| **25 个 Agent 适配器** | Claude Code、Codex、DSH、Gemini / Antigravity、Hermes、WorkBuddy、Kimi、Copilot、Qoder、Qwen Code、Pi、Prime、CodeBuddy、Command Code、Devin、MiMoCode、MiniMax、OpenCode、ZCode、Muse 等 |
| **多维度统计** | 按 Agent / Model / Provider / 项目（工作目录）/ Session / 天 |
| **用量日历** | 一整年的热力图，一天一格；有记录的日子按排名分五档着色，悬停看当天 token 与费用，点一天就跳到那天 |
| **缓存命中率** | 先聚合再相除，不平均每行比率（详见 [DESIGN.md](DESIGN.md)） |
| **参考费用估算** | 按厂商官方标价，支持分时计价（DeepSeek 峰谷按每条请求自身时间判断） |
| **中英双语** | 严格分离，中文模式只显示中文 |
| **官方图标** | 25 个 Agent 全部用厂商官方标识，内嵌 SVG，离线可用 |
| **桌面版** | Windows 安装包，免 Python 环境 |

---

## 安装

### Windows 桌面版（推荐）

从 [Releases](https://github.com/Zhucy123/agent-token-radar/releases) 下载 **`AgentTokenRadar-Setup.exe`**，双击安装：

- 装到 `C:\Program Files\AgentTokenRadar`，安装时请求一次管理员权限
- 开始菜单自动建快捷方式，桌面图标是可选勾选项
- 卸载走「设置 → 应用」或控制面板，**保留**用量数据库（重装不丢历史）
- 无需 Python、无需命令行，打开即自动扫描

> **必须装到纯英文路径。** 界面用 pywebview + Edge WebView2，其 .NET 运行时加载原生 DLL 时按 ANSI 解析路径，中文路径会导致 `Failed to resolve Python.Runtime.Loader.Initialize`。安装程序会检查并拒绝非 ASCII 目标。

### pip 安装

```bash
# 只要 CLI —— 纯 Python 标准库，不装任何第三方包
pip install "agent-token-radar @ git+https://github.com/Zhucy123/agent-token-radar"

# 还要 Web 仪表盘（fastapi + uvicorn）
pip install "agent-token-radar[web] @ git+https://github.com/Zhucy123/agent-token-radar"
```

需要 Python **3.11+**。DSH 的 `.zstd` 会话日志在 3.14+ 用标准库解压；3.11–3.13 请加 `pip install zstandard`（或只用 `.jsonl` 回退）。

### 从源码运行

```bash
git clone https://github.com/Zhucy123/agent-token-radar
cd agent-token-radar
pip install -e ".[web]"          # 桌面版再加 pip install pywebview
python -m agent_token_radar serve
```

---

## 使用

```bash
agent-token-radar scan                          # 扫描一次
agent-token-radar stats --range 7d --by agent   # 终端报表
agent-token-radar stats --range all --by model  # 按 Model
agent-token-radar stats --range all --by project
agent-token-radar serve                         # 仪表盘 http://127.0.0.1:8770
agent-token-radar desktop                       # 原生桌面窗口
```

装好之后在任意目录都能运行。

**数据库位置**

| 平台 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\agent-token-radar\token_radar.db` |
| Linux | `~/.local/share/agent-token-radar/token_radar.db` |

用 `AGENT_TOKEN_RADAR_DB` 覆盖。

**端口策略**：8770 优先，被占用自动顺延到 8771、8772…… 启动时会明确告诉你在哪个端口。
想固定端口、被占即报错退出，加 `--no-fallback`。

**语言切换**：`stats --lang en`，或 Web 右上角 `中文 / EN`，或用 URL 参数 `?lang=en`。

---

## 参考了什么

这个项目不是从零发明的，三个方向各有明确的参照对象，每处「为什么这么实现」都标了出处。

### [cclank/tokei](https://github.com/cclank/tokei) — Agent 覆盖面与解析规格

| 借鉴点 | 具体做法 |
|---|---|
| **Agent 覆盖面** | 25 个适配器对齐 tokei 的支持列表；tokei 不支持的（Cursor、Grok Bot、Doubao Work）不擅自新增 |
| **两种 input 语义** | `Additive`（input 不含 cache）与 `Inclusive`（input 已含 cache 需相减），每个适配器显式声明，不靠猜 |
| **reasoning 是 output 的子集** | 单独报告，绝不加进 total |
| **Copilot 只统计 output** | 上游 input/cache 包含关系未定论，宁可留空也不猜 |
| **去重键设计** | `event_key` 用各 Agent 天然的请求 id，而不是行号 |
| **官方图标** | 25 个全部官方标识，无一自绘 |

**刻意不跟随的部分**：tokei 缺价时会「沿用同档位上一版或兜底价」，本项目**不这样做**——查不到官方价就不算，代价是覆盖率停在 96.5% 而不是 100%。一个编出来的价格比一个承认的缺口更糟。

逐 Agent 解析规格见 [TOKEI-NOTES.md](TOKEI-NOTES.md)。

### [magpie](https://magpie.im) 本机网关 — 会话标题与 provider 的真实来源

本机 Claude Code / Codex 的流量经过 magpie 网关，它自己的记录比任何 Agent 日志都更接近「真实发生了什么」，因此被当作**实测来源**：

| 借鉴点 | 具体做法 |
|---|---|
| **会话标题怎么读** | DSH 在 `ledger.json` 的 `days.<日期>.sessions[]`；WorkBuddy 在 `workbuddy.db` 的 `sessions` 表 |
| **provider 实测优先** | Claude Code 日志没有 provider 字段，但它的 `message.id` 就是网关的 `responseId`，逐请求对上即可 |
| **路由账本优先** | 网关的 `routing/*.jsonl` 是逐请求账本，比导出日志更权威 |

**证据强度**：587 条匹配请求的 input/output token 与网关记录**逐条完全一致，0 条不符**。

> **注意**：本项目**不需要** magpie 也能工作。没有网关时 provider 会退回「推断」或留空，界面会明确标出来，不会把猜测当事实。

未跟随 magpie 定价相关：magpie 本机安装里没有价格 / 费用记录，无可参考的定价实现。

### 厂商官方定价页 — 费用估算

价格全部手工取自厂商官方定价页，每条都记了来源 URL 和读取日期：

| 厂商 | 来源 |
|---|---|
| DeepSeek | <https://api-docs.deepseek.com/quick_start/pricing> |
| OpenAI | <https://developers.openai.com/api/docs/pricing> |
| Anthropic | <https://platform.claude.com/docs/en/about-claude/pricing> |
| Kimi | <https://platform.kimi.ai/docs/pricing/chat> |
| Zhipu | <https://docs.z.ai/guides/overview/pricing> |
| Qwen | <https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash> |

币种一律用官网**美元价**，任何地方都不做汇率换算。厂商调价只改 `agent_token_radar/pricing.json`，不改代码。

---

## 文档

| 文件 | 内容 |
|---|---|
| [DESIGN.md](DESIGN.md) | 完整技术文档：缓存命中率怎么算、各 Agent 的 input 语义差异、架构与取舍 |
| [PRICING-NOTES.md](PRICING-NOTES.md) | 定价口径：官方价来源、为什么无价不算 0、分时计价怎么实现 |
| [TOKEI-NOTES.md](TOKEI-NOTES.md) | tokei 调研与逐 Agent 解析规格 |
| [BUILD-NOTES.md](BUILD-NOTES.md) | 打包过程与 14 个坑的记录 |

---

## 开发

```bash
# 测试（需要 PYTHONPATH 指向 tests）
PYTHONPATH=tests python -m unittest discover -s tests -p 'test_*.py'   # 293 个测试

# 扫描冒烟
python -m agent_token_radar scan

# 打包 Windows 安装包
python -m PyInstaller --noconfirm agent-token-radar-dir.spec   # -> dist/AgentTokenRadar/
ISCC.exe agent-token-radar.iss                                  # -> dist-installer/AgentTokenRadar-Setup.exe
```

### 加一个新 Agent

在 `adapters/` 放一个含 `class XxxAdapter(BaseAdapter)` 的 `.py` 文件即可**自动注册**，无需改任何中心列表。
必须实现 `name`、`detect()`、`scan(checkpoint)`，并保证 `event_key` 稳定（重复扫描靠 UPSERT 幂等）。

---

## 更新日志

### 0.0.6

**新增**

- **用量日历**：趋势图上方新增一整年的热力图，一天一格。有记录的日子按排名分五档着色，一眼看出这一年什么时候在用；鼠标悬停或键盘聚焦能看到那天的 token、费用和计价覆盖率。点某一天就跳到那天。
- 日历可以翻到往年，键盘方向键在整年里移动（整年只占一个 Tab 停靠点，不会把后面的内容挤走）。

**修复**

- **窄窗口下日历停在一屏空白**。日历比窗口宽，滚动条默认在最左边，也就是年初——年初通常没数据，笔记本上打开只能看见一片空格子，得自己横着找。现在会自动停在**活跃天数最多的一屏**。
- **日历发虚**：最浅一档原来是 1px 内阴影环，在非整数缩放下会糊成灰晕，整年看着像失焦。现在改成实心底色。星期标签与所在行、月份标签与所在列也重新对齐了。
- **改字号后日历没有重新定位**：格子按 rem 定尺寸，字号一变列宽就不同，能放下几列也跟着变。原来的判断依据是推导出来的、不是实测的，算出 14px 列距（实际 17.594px），于是停在 84px，全年第 94 个活跃日只看得见 50 个。现在按实测列距判定，并在字号变更处直接重新定位。

### 0.0.5

**新增**

- **昨天**：工具栏新增「昨天」按钮，与今日、近 7 天并列。
- **任选日期区间**：工具栏的日期按钮始终写出当前窗口的实际起止（如「11-18 至 11-24」），点开可选任意起止日期，也能只选一天。选自定义区间后，几个预设按钮会熄灭，避免「近 7 天」亮着却只有 3 天数据。
- **点趋势图上的某一天直接跳到那天**：点整列任意位置都算（不只是点在柱子上），日期按钮随即显示那一天。
- **完整 / 缩写两种数字格式**：工具栏新增开关。「完整」显示精确数字（如 `2,967,603,835`）；「缩写」按界面语言用万/亿（中文）或 K/M/B（英文）。选择会被记住，刷新后保持。

**修复**

- **选到没有数据的时间段时，图表和表格会残留上一次的内容**。现在会清空图例、纵轴刻度、占比列表和柱状图，表格文案改为「这个时间范围没有数据」，不再提示去扫描——扫描本身已经跑过了。
- **卡片里的长数字会被裁掉**。根因有两个：数字字号跟着窗口宽度放大，而 7 张卡片并排时每张只有约 190px；卡片自身的 `overflow: hidden` 又把它的最小宽度压成 0，数字再长也撑不开。现在卡片等宽整齐，放不下的数字只在该卡片内缩小到刚合适，不再裁切，也不会挤出横向滚动条。
- **「昨天」曾返回全部历史**（92 天 / 8.1 万条），因为后端不认识这个范围却静默退化成了「全部」。现在未知范围会明确报错，不再伪装成「全部」。

**说明**

- CLI 的 `--range` 同步支持 `yesterday`，与网页一致。
- 切语言时，卡片的数字后缀、日期标签和日期面板的校验提示都会跟着变，不会留下旧语言。

### 0.0.4

**新增**

- **应用内一键更新**（安装版）：面板里的「前往下载」换成蓝色的「立即更新」主按钮。点下去之后，程序自己下载安装包 → 核对体积 → 拉起安装程序 → 装完删掉安装包 → 以新版本重新打开，全程不用再去浏览器。
- **安装包只在下载完整后才执行**：先写成 `.part` 临时名，全部到齐才改名为正式文件，中途崩溃不会留下一个看起来能装、其实是残包的文件。
- **启动时清扫残留**：更新中途重启过的机器，不会再攒下一堆 18 MB 的旧安装包。

**说明**

- **Windows 一定会弹一次权限确认，这一步无法省略**（安装程序要写入 Program Files）。界面在点按钮**之前**就写明这件事，不会让人以为是出错。下载、核对、安装、删除、重开都是自动的。
- 从源码运行时不走这条路，面板会直接提示用 `git pull` 更新，不会去下载安装包。
- 安全措施：只接受 HTTPS 地址（防止中途被降级替换）、比对 Release 公布的安装包体积（能分辨「传了一半」和「代理塞了错误页」）、安装包落在用户目录而不是安装目录、不经 shell 启动（带空格的路径不会被拆错）。

### 0.0.3

**新增**

- **版本更新检查**：顶部工具栏显示当前版本号（如 `v0.0.3`），点开可看是否有新版；有新版本时按钮会亮起并带一个小圆点，不用点开就知道。启动时在后台查一次，结果缓存 24 小时（一天最多一个请求），只读取版本标签、不上传任何数据、不含机器标识。断网或 GitHub 不通时安静显示「暂时无法检查」，不影响使用。

**说明**

- 这是本项目**唯一的联网行为**。此前「无网络请求 · 不联网」的定位不再适用，README 与 DESIGN 的相关表述已同步更正。
- 设环境变量 `AGENT_TOKEN_RADAR_NO_UPDATE_CHECK=1` 可彻底关闭：启动不查、手动点开也不查，一个请求都不发。

### 0.0.2

**新增**

- **页面跳转栏**：顶部一排六个按钮（汇总 / 消耗趋势 / 按 Agent 分布 / 按 Agent / Model / 按项目分布 / Session 排行），点击直达对应区块。滚动时自动高亮当前区块，窄窗口按钮折成两行仍能对齐。
- **文字设置窗口**（右上角齿轮）：字号 4 档、字体 23 种（按无衬线 / 衬线 / 中文 / 等宽分组）、字重 4 档、倾斜、文字颜色 7 种、描边 4 档。改完立即生效、刷新后保留，且首屏直接以设定值渲染，不会先闪一下默认样式。

**修复**

- **费用重复计费**：Codex、WorkBuddy 这类把缓存读写合并计的 Agent，缓存部分被收了两次钱。修复当时实测，总额由 $1875.84 修正为 $374.47，其中 Codex 由 $1532.53 降到 $120.38。**这两个数是修复那一刻的快照，金额会随本机新增用量持续变化**，以你装好后的实际显示为准。
- **跳转后高亮错位**：点某个区块后，高亮会跳到下一个区块。原因是滚动监听把「本轮状态变化」当成了「当前可见的全部章节」。
- **中文模式残留英文**：跳转栏的「Top Session」、状态点的鼠标悬停提示、语言切换按钮的无障碍名，此前在中文模式下仍是英文，现在都跟随语言切换。
- **等宽字体的中文**：项目名、Session 标题等使用等宽字体的位置没有中文兜底，中文会回落到系统默认字体，与页面其他文字不一致；23 种字体选项与画布图表也逐项确认过。
- **跳转落点间距**：文字调大后，跳转落点的实际间距与设定值不一致。

### 0.0.1

首个发布版本：25 个 Agent 适配器、CLI、单页 Web 仪表盘、中英双语、官方图标、按官方标价估算参考费用、Windows 安装包。

---

## 说明与边界

- **费用是参考估算，不是账单。** 按官方标价折算，只覆盖有公开价的模型；无价的如实标注并单独显示覆盖率
- **对第三方目录严格只读**，不写入、不删除、不修改任何 Agent 自己的数据
- **唯一的联网是检查更新**：启动时向 GitHub 查一次最新版本号，结果缓存 24 小时，所以一天最多一个请求。只读版本标签，**不上传任何使用数据、不含机器标识**。不联网或 GitHub 不通时功能完全不受影响，只是看不到更新提示。设 `AGENT_TOKEN_RADAR_NO_UPDATE_CHECK=1` 可彻底关掉
- **价格表是手工维护的静态数据**，不是联网抓的
- **未签名**，首次运行 SmartScreen 可能提示「未知发布者」
- 只有 Windows 安装包，Linux 目前用 `pip install`

---

## 许可证

MIT，见 [LICENSE](LICENSE)。

第三方品牌图标归各自厂商所有，仅为标识对应 Agent 而使用。
