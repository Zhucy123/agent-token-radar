# HANDOFF — 给接手的 Agent

如果你是一个 agent，被要求接手 / 继续 / 修复这个项目，**先读这一页**，再决定读哪些文件。

项目根目录：`C:\agent-token-radar`

---

## 这是什么

`agent-token-radar`：统计本机各个 AI Agent（Claude Code、Codex、DSH、Gemini CLI、WorkBuddy 等 25 个）的
**Token 用量**，并按厂商官方标价给出**参考费用估算**（不是账单，见 `PRICING-NOTES.md`）。

技术栈：Python 3.11+ / FastAPI / SQLite，插件化适配器（`adapters/` 下一个 Agent 一个文件），
外加 CLI 和单页 Web 仪表盘，以及一个 pywebview 桌面窗口。

---

## 一句话现状

**功能完成、测试全过、能打包成安装程序交付。** 没有已知的阻塞问题。

| 项 | 状态 |
|---|---|
| 单元测试 | 223 个全过（`python -m unittest discover -s tests -p 'test_*.py'`） |
| CLI | 可用（`scan` / `stats` / `serve` / `desktop`） |
| Web 仪表盘 | 可用，中英双语，25 个 Agent 官方图标 |
| 桌面版（源码运行） | 可用 |
| Windows 安装包 | 已产出：`dist-installer/AgentTokenRadar-Setup.exe` |

---

## 先读哪几个文件

按这个顺序，读到够用就停：

| 顺序 | 文件 | 为什么 |
|---|---|---|
| 1 | `README.md` | 项目是什么、怎么装、怎么用 |
| 2 | `DESIGN.md` | 完整技术文档：缓存命中率怎么算、各 Agent 的 input 语义差异、图标来源 |
| 2b | `PRICING-NOTES.md` | 定价口径：官方价来源、为什么无价不算 0、覆盖率怎么读 |
| 3 | `BUILD-NOTES.md` | **打包必读**。14 个坑的完整记录，含 4 个「本地正常、打包必炸」的陷阱 |
| 4 | `agent_token_radar/base.py` | 适配器基类，理解插件架构从这里开始 |
| 5 | `agent_token_radar/store.py` | 数据库 schema 和所有聚合 SQL |
| 6 | `agent_token_radar/web.py` | API 路由（只有 8 个，很短） |
| 7 | `agent_token_radar/static/app.js` | 前端全部逻辑 |

改具体功能时再按需读：

- 加新 Agent → `adapters/` 里新建一个文件即可，**不需要改任何注册表**（自动发现）
- 改界面 → `static/index.html` + `app.js` + `app.css`
- 改文案 → `i18n.py`（中英两份独立字典，**不要做回退链**）
- 改图标 → `icons.py`（内嵌 SVG data URI），重建用 `_build_icons.py`

---

## 硬约束（不要违反）

这些是用户在开发过程中明确要求的，改需求前先问：

1. **Token 是实测值，费用是推导值。** 费用按厂商官方标价折算，**查不到官方价的模型不算钱**，
   标「无公开价」而不是 0，并强制显示覆盖率。**绝不用相近模型名插值定价。**
   **币种一律用厂商官网的美元价，不做汇率换算**（Zhipu / Qwen 都有国际站美元页）。
   **分时计价的厂商（目前只有 DeepSeek）按每条请求自身的时间判峰谷**，
   不是按「现在」，也不是按当天均价。
   改价改窗口只改 `agent_token_radar/pricing.json`（带来源 URL 与读取日期），不改代码。
   详见 `PRICING-NOTES.md`。

2. **Agent 图标必须是官方图标。** simple-icons 的官方 glyph，或厂商站点 / 本机已安装程序的资源。
   不要随手画、不要找替代品。

3. **中英双语严格分离。** 中文模式只显示中文；英文模式全英文。
   例外：`token`、`agent` 以及 `Agent` / `Model` / `Provider` / `Session` 这类专有名词保留英文。
   卡片主标题用中文、副标题给对应英文术语。

4. **对第三方目录严格只读。** 绝不写入任何 Agent 自己的目录。

5. **标准库优先，依赖尽量轻。** 能不用第三方库就不用。

6. **路径要同时兼容 Windows 和 Linux。**

7. **改完要告诉用户：是刷新网页就行，还是需要重启服务。** 需要重启的话自己重启。

---

## 打包（这块最容易踩坑）

```bash
# 1. 打 onedir
python -m PyInstaller --noconfirm --clean agent-token-radar-dir.spec

# 2. 做安装包
"C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" agent-token-radar.iss
```

**必须知道的三件事**（细节在 `BUILD-NOTES.md`）：

- **安装路径必须是纯 ASCII。** 含中文的路径会让 .NET 运行时加载 DLL 失败
  （`Failed to resolve Python.Runtime.Loader.Initialize`）。`agent-token-radar.iss`
  里有校验会拒绝非 ASCII 路径。**不要把这个项目搬到中文目录下重新打包。**

- **适配器是运行时动态发现的，PyInstaller 看不见它们。** 两个 spec 里都有
  `collect_submodules("agent_token_radar.adapters")`，`registry.py` 里还有 PYZ 回退逻辑。
  删掉任何一个，打包版就会丢失全部图标和 Agent 名称（而且**不会报错**，只会静默显示原始 id）。

- **打包态的数据目录是 `%LOCALAPPDATA%\agent-token-radar\`，不是 `_internal/data/`。**
  `paths.py` 的 `_running_frozen()` 负责这个判定。装到 Program Files 后，
  往安装目录写会因权限失败。

打包后**务必**跑一遍验证清单（`BUILD-NOTES.md` 第 5 节），重点是
`GET /api/adapters` 要返回 **25** 个，不能是 0。

---

## 目录地图

```
C:\agent-token-radar\
├── agent_token_radar\          源码
│   ├── adapters\               25 个 Agent 适配器（一个文件一个 Agent）
│   ├── static\                 单页仪表盘 index.html / app.js / app.css
│   ├── base.py                 适配器基类 BaseAdapter
│   ├── registry.py             适配器自动发现（含 PYZ 回退）
│   ├── store.py                SQLite schema + 聚合查询
│   ├── web.py                  FastAPI 路由
│   ├── cli.py                  命令行入口
│   ├── desktop.py              pywebview 桌面窗口
│   ├── paths.py                跨平台路径解析（含 frozen 判定）
│   ├── i18n.py                 中英文案
│   ├── icons.py                内嵌官方图标 SVG
│   ├── provider_sources.py     Provider 判定（实测优先，推断次之并标注）
│   ├── pricing.json            官方标价表（数据，不是代码）
│   └── pricing.py              价格解析、峰谷判断（无汇率，全 USD）
├── tests\                      223 个测试
├── build\                      app.ico / icon_512.png
├── dist\                       PyInstaller 产物（可重建，勿手改）
├── dist-installer\             AgentTokenRadar-Setup.exe（交付物）
├── agent-token-radar-dir.spec  onedir 打包配置
├── agent-token-radar.spec      单文件打包配置（当前不用）
├── agent-token-radar.iss       Inno Setup 安装脚本
├── ChineseSimplified.isl       Inno 简体中文语言文件
├── pyproject.toml              pip 安装配置
├── BUILD-NOTES.md              打包过程与 14 个坑
├── DESIGN.md                   完整技术文档（维护者向）
├── PRICING-NOTES.md            定价口径与维护方法
├── TOKEI-NOTES.md              tokei 调研与逐 Agent 解析规格
└── README.md                   项目入口说明
```

---

## 常用命令

```bash
# 测试
python -m unittest discover -s tests -p 'test_*.py'

# 扫描一次
python -m agent_token_radar scan

# 终端报表
python -m agent_token_radar stats --range 7d --by agent

# Web 仪表盘（端口 8770 起，被占自动顺延）
python -m agent_token_radar serve

# 桌面窗口（源码方式）
python -m agent_token_radar desktop
```

---

## 环境注意

- Python **3.14.7**，64 位。3.11–3.13 读 DSH 的 `.zstd` 日志需要额外装 `zstandard`
- `Path.home()` 在 Windows 读的是 `HOMEDRIVE` + `HOMEPATH`，只设 `USERPROFILE` 不生效
- `DSH_HOME` 指向真实目录会绕过 HOME 模拟
- 当前 shell 是 **Medium 完整性级别**（Administrators 组 deny-only），
  装 / 卸到 `Program Files` 需要 UAC 确认

---

## 还没做 / 可以做的

不是 bug，是没动手的事，做之前先跟用户确认：

- 安装包**未签名**，首次运行 SmartScreen 会提示「未知发布者」
- 没有自动更新机制
- 只有 Windows 安装包，Linux 目前只能用 `pip install`
- 单文件 exe 打包配置保留着但不用（在这台机器上解包会失败，见 BUILD-NOTES 坑 5）
- Cursor 的适配器用户明确说先不做
