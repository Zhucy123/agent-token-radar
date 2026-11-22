# 打包成 Windows 桌面版：过程、坑与结论

这份文档记录 `agent-token-radar` 从「能跑的 Python 项目」变成「双击安装的 Windows 程序」的全过程，
重点是**踩过的坑**——那些花了很久才定位、而且不写下来下次一定会再踩一次的问题。

| 产物 | 大小 | 说明 |
|---|---|---|
| `dist-installer/AgentTokenRadar-Setup.exe` | 17.9 MB | 交付给用户的安装包（Inno Setup） |
| `dist/AgentTokenRadar/` | 43.2 MB | PyInstaller onedir 产物，安装包的内容来源 |

> **这份文档里的所有相对路径，都以 `C:\agent-token-radar` 为根。**
> 那是纯 ASCII 的完整工程目录（源码 + 测试 + 两个 spec + iss + 安装产物），
> 也是唯一能直接重新打包的位置。中文路径下的工作区副本仅供查看，
> 在那里打包会踩本文件坑 1。
> 接手的 agent 请先读 `C:\agent-token-radar\HANDOFF.md`。

---

## 1. 环境

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.14.7 | 64 位 |
| PyInstaller | 6.22.0 | 打包器 |
| pywebview | 6.2.1 | 原生窗口（Windows 后端 = WinForms + WebView2） |
| pythonnet | 3.2.1 | pywebview 的 Windows 后端依赖，.NET 互操作 |
| clr-loader | 0.3.1 | pythonnet 的 CLR 加载层 |
| fastapi / uvicorn | 0.141.1 / 0.52.4 | 内嵌 Web 服务 |
| Inno Setup | 6.7.3 | 安装包编译器 |

关键点：**pywebview 的 Windows 后端依赖 .NET Framework**。整个打包难题的一大半都源于这个依赖。

---

## 2. 整体流程

```
源码
 └─ PyInstaller (onedir) ──► dist/AgentTokenRadar/   (exe + _internal/)
      └─ Inno Setup ──────► dist-installer/AgentTokenRadar-Setup.exe
```

两条命令：

```bash
# 1. 打包成目录形态
python -m PyInstaller --noconfirm --clean agent-token-radar-dir.spec

# 2. 做成安装包
"C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" agent-token-radar.iss
```

**为什么用 onedir 而不是单文件**：单文件模式在这台机器上直接失败（见坑 5）。
onedir 解包一次、启动快，而且 DLL 路径是真实的、可排查的。

---

## 3. 踩过的坑

### 坑 1：中文路径下 .NET 加载失败（最耗时的一个）

**现象**：打包后的 exe 启动即崩，没有任何窗口：

```
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
  from ...\dist\AgentTokenRadar\_internal\pythonnet\runtime\Python.Runtime.dll
```

DLL 明明就在那个路径下。

**排查**：做了对照实验——把**同一份** DLL 放到不同目录：

| 路径 | 结果 |
|---|---|
| `C:\atr_ascii\` | ✅ 成功 |
| `...\文档\默认工作区\...` | ❌ 失败 |

**根因**：pythonnet 通过 `clr_loader` 把 DLL 路径以 **UTF-8** 传给 C 层
（`pyclr_get_function(const char* assembly_path, ...)`），而 .NET Framework 在 Windows 上按
**ANSI（当前代码页）** 解析这个路径。路径里有中文时，两边编码对不上，.NET 就认为「文件不存在」。

**修法**：把项目搬到纯 ASCII 路径（`C:\agent-token-radar`）重新打包。这一搬，问题直接消失。

`__app_entry__.py` 里保留了 `_prepare_clr()` 作为兜底（把 CLR 原生库复制到 ASCII 目录再加载），
但真正解决问题的是**安装位置本身是 ASCII**——这也是安装程序强制校验路径的原因。

---

### 坑 2：标准修法在受限环境里行不通

坑 1 的标准解法是「把 DLL 复制到一个短的 ASCII 目录再加载」。但这台机器上：

```
PermissionError: [Errno 13] Permission denied
```

打包后的进程**写不了** `%LOCALAPPDATA%` 和 `%TEMP%`。而 `exe` 自己所在的目录可写，但那个目录正好是含中文的路径。
于是「可写」和「ASCII」两个条件无法同时满足，标准修法失效。

**结论**：这是**受限环境**特有的死锁，真实用户装到 `C:\Program Files\AgentTokenRadar` 不会有这个问题。
所以最终没有继续和它搏斗，而是搬路径。

---

### 坑 3：8.3 短路径不可用

理论上可以用 `GetShortPathNameW` 把中文路径转成 `C:\PROGRA~1\...` 这种 8.3 短名，绕开编码问题。

实测：`GetShortPathNameW` 返回 `None`——**该卷的 8.3 名称生成被关闭了**（OneDrive 托管卷的常见配置）。

这条路直接封死。

---

### 坑 4：PyInstaller runtime hook 不执行

想用 runtime hook 在 `import clr` 之前修好环境变量，于是写了 `pyi_rth_pythonnet.py`。

实测：**hook 根本没被执行**，连日志都没生成。换最简的 `_simple_hook.py` 也一样。

**修法**：把这段逻辑放进入口脚本 `__app_entry__.py` 的 `main()` 里，执行顺序有保证。

---

### 坑 5：单文件模式解包失败

先试的是单文件 exe（`--onefile`），直接报：

```
Could not create temporary directory!
```

PyInstaller 单文件模式启动时要先解包到临时目录，而这台机器的 `%TEMP%` 不可写（同坑 2）。

**修法**：改用 onedir。对最终用户也没有坏处——安装包本来就是解压到目标目录的。

---

### 坑 6：数据库被写进安装目录

**现象**：装好的程序打开后界面全是 0，数据库文件出现在
`dist/AgentTokenRadar/_internal/data/token_radar.db`。

**根因**：`paths.py` 的判定逻辑是「在 checkout 里跑就写 `./data/`」。
PyInstaller 打包后 `PROJECT_ROOT` 指向 `_internal/`，看起来就像一个 checkout，于是数据库被写进安装目录。

**为什么严重**：真实用户装到 `C:\Program Files`，普通账户**没有写权限**，结果是
程序能开但永远存不下数据，界面永远空白。

**修法**：加 `_running_frozen()`，用 `getattr(sys, "frozen", False)` 判定打包态，
打包态一律走 `%LOCALAPPDATA%\agent-token-radar\`（Linux 走 `$XDG_DATA_HOME`），和 pip 安装态一致。

```python
def _running_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))

def _running_installed() -> bool:
    if _running_frozen():
        return True          # 打包态的目录不是我们该写的地方
    parts = {p.lower() for p in PROJECT_ROOT.parts}
    return bool(parts & {"site-packages", "dist-packages"})
```

补了 4 个测试锁住这个行为。

---

### 坑 7：高分屏下窗口尺寸算错

**现象**：4K 显示器、150% 缩放时，窗口开出来比屏幕还大。

**根因**：单位不一致。

- pywebview 的 Windows 后端按**逻辑像素**建窗口（它自己是 per-monitor DPI aware 的）
- `SystemParametersInfoW(SPI_GETWORKAREA)` 返回的是**物理像素**

在 3840×2160 / 150% 的屏幕上，代码算出 `3532×1920` 逻辑像素，实际变成 `5298×2880` 物理像素。

**修法**：拿到工作区尺寸后先除以 DPI 缩放（`GetDeviceCaps(hdc, LOGPIXELSX) / 96`）。

```python
width, height = 物理工作区
scale = _dpi_scale()          # 1.0 / 1.25 / 1.5 / 2.0
if scale > 1.0:
    width, height = int(width / scale), int(height / scale)
```

结果：`clamp(1280,860)` → `1280×860`；`clamp(9999,9999)` → `2355×1280`，物理上 `3532×1920`，正好放得下。

---

### 坑 8：适配器一个都没打进去（找得最久的一个）

**现象**：装好的程序能跑、数据库也有 7 万多条记录，但——

- Agent 名字显示成原始的 `dsh` / `codex` / `claude`
- 官方图标全部空白
- 页脚 25 个数据源全没了

接口直接暴露了问题：

```bash
GET /api/adapters  →  []
```

**根因**：适配器是**运行时动态发现**的：

```python
for info in pkgutil.iter_modules(_adapters_pkg.__path__):
```

`pkgutil.iter_modules` 走的是**文件系统**。PyInstaller 把 `.py` 编译进 `PYZ` 归档后，
磁盘上没有那些文件了——所以 `iter_modules` 返回空，一个适配器都发现不了。

更隐蔽的是：**静态导入图里也没有它们**（因为是动态导入），所以 PyInstaller 自然不会收集。

**修法**（两处都要改）：

1. `registry.py`：`pkgutil` 找不到任何模块时，回退扫描 `PYZ` 的模块表：

```python
def _adapter_module_names() -> list[str]:
    names = [... pkgutil.iter_modules ...]
    if names:
        return names                      # 源码模式：正常路径，还能捡到用户自己加的适配器
    # 打包态回退：PYZ 的 toc 里列着所有被打进去的模块
    toc = getattr(sys.modules["__main__"].__loader__, "toc", None)
    if toc:
        prefix = f"{_adapters_pkg.__name__}."
        return sorted(e[len(prefix):] for e in toc
                      if e.startswith(prefix) and "." not in e[len(prefix):])
    return []
```

2. 两个 spec 都显式收集适配器：

```python
from PyInstaller.utils.hooks import collect_submodules
hiddenimports += collect_submodules("agent_token_radar.adapters")
```

修完实测：`/api/adapters → 25 个`，图标、官方名称、数据源全部回来。

> 这个坑的价值：**动态导入 + 打包器 = 静默丢失**。打包后一定要验证「运行时才发现的东西」还在不在。

---

### 坑 14：`pricing.json` 没打进去，费用全变成「无公开价」

**现象**：装好的程序一切正常，Token、图标、Agent 名字都对，唯独**费用列整列变成「无公开价」**、
顶部金额显示 `—`。不报错、不崩溃、日志里只有一行 warning。

**根因**：`pricing.py` 用**真实路径**读价格表，不是走 `importlib.resources`：

```python
PRICING_PATH = Path(__file__).with_name("pricing.json")
```

`Path(__file__)` 在打包态指向 `_internal/agent_token_radar/`，而 spec 的 `datas` 只收了
`static/`，`pricing.json` 从来没被复制进去。`PriceTable.load()` 捕获了 `FileNotFoundError`，
返回**空表**——于是每个模型都查不到价，全部落进「无公开价」。

而且它**静默降级**：`logger.warning` 写在启动日志里，窗口模式下根本看不到。

**修法**：两个 spec 的 `datas` 都加上它。

```python
datas += [(str(ROOT / "agent_token_radar" / "pricing.json"), "agent_token_radar")]
```

`pyproject.toml` 的 `package-data` 同样漏了，pip 装出来的包也一样没价格表，一并补上：

```toml
agent_token_radar = ["static/*", "static/**/*", "pricing.json"]
```

**验证**（不能只看文件在不在，要看它真能读出价）：

```bash
GET /api/overview?range=all
→ {"pricing": {"model_count": 30, ...}, "totals": {"cost_usd": 375.60, "priced_ratio": 0.9621}}
```

`model_count: 30` 且 `cost_usd` 不为 0，才说明读到了。只看 `Test-Path` 会被空表骗过去。

（这里的金额是 2026-11-22 修复「inclusive 存法缓存被重复计费」后的实测值。旧文档记的 $1,989.55
是修复前的数字，已作废——它虚高约 5 倍，不能当验收基线。）

> 与坑 8 同一个模式：**运行时才读的文件 + 打包器 = 静默丢失**。
> 区别是坑 8 丢的东西会立刻看出来，这个丢了只显示一个无辜的「无公开价」，
> 很容易被当成「这模型本来就没价」。

---

### 坑 9：Inno Setup 6.7 不再自带简体中文

```
Couldn't open include file "...\Languages\ChineseSimplified.isl"
```

Inno Setup 6.7.3 的语言目录里只有 29 种语言，**简体中文被移到了社区维护的 Unofficial 目录**。

**修法**：从官方仓库取（`Files/Languages/ChineseSimplified.isl`，21516 字节），
放进编译器的 `Languages\` 目录。

取的时候也踩了坑：`raw.githubusercontent.com` 连接被重置（`curl: (56) Recv failure`），
最后用 jsDelivr 镜像拿到：

```
https://cdn.jsdelivr.net/gh/jrsoftware/issrc@main/Files/Languages/ChineseSimplified.isl
```

---

### 坑 10：`findstr` 的 ASCII 校验会误判

安装脚本里想提前拒绝非 ASCII 安装路径，第一版这么写：

```bat
echo %TARGET% | findstr /R /C:"^[ -~]*$" >nul
```

结果在中文代码页下，`C:\Program Files\AgentTokenRadar` 这种完全合法的路径**也被判为非 ASCII**。

**根因**：`findstr` 的字符类依赖当前控制台代码页。

**修法**：改用 PowerShell 做这条判断：

```bat
powershell -NoProfile -Command "if ('%TARGET%' -match '[^\x20-\x7E]') { exit 1 } else { exit 0 }"
```

---

### 坑 11：`ProgramData` 的开始菜单快捷方式需要提权

```
Unable to save shortcut "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\..."
  UnauthorizedAccessException
```

即使 `Test-Path` 显示目录存在，普通账户也写不进去。

**修法**：改用**当前用户**的开始菜单（`%APPDATA%\Microsoft\Windows\Start Menu\Programs`），不需要 UAC。
（在最终的 Inno Setup 版里不存在这个问题——安装程序本身就带提权。）

---

### 坑 12：桌面不是 `%USERPROFILE%\Desktop`

这台机器的桌面被 OneDrive 重定向到了 `C:\Users\<用户>\OneDrive\Desktop`。
所以用 `%USERPROFILE%\Desktop` 拼路径创建的快捷方式，用户**根本看不到**。

**修法**：始终用 `[Environment]::GetFolderPath('Desktop')` 取真实位置，不要手拼。

---

### 坑 13：卸载需要提权，无头环境里卡住

装到 `Program Files` 后，卸载同样需要管理员权限。在自动化环境里 UAC 弹窗没人点，
`Start-Process -Verb RunAs` 会失败并报「操作已被用户取消」。

**这不是安装包的缺陷**，是环境限制。真实用户双击卸载时会看到 UAC 确认框，点「是」即可。

排查时还顺便确认了一件事：Inno Setup 生成的 Setup.exe，清单里写的是
`requestedExecutionLevel level="asInvoker"`——这是**正常且正确的**，Inno 在运行时自己请求提权，
而不是靠清单强制。

---

## 4. 走过的死路（记录下来避免重复尝试）

| 尝试 | 结果 |
|---|---|
| `PYTHONNET_RUNTIME=netfx` 强设 | 反而更糟。Windows 上 pythonnet 默认已经是 netfx |
| 把 `PYTHONNET_RUNTIME` 当路径用 | 它是**运行时选择器**，取值只有 `netfx` / `coreclr` / `mono` |
| `PYTHONNET_NETFX_DIR` | 这个环境变量不存在 |
| 用 ctypes 手工接管 `pyclr_get_function` + `Initialize` | 能拿到函数指针（`2098378050060`），但手工构造结构体调用 `Initialize` 会崩在 pythonnet 内部的 `Py.GIL()` |
| 自定义 `pyi_rth_pythonnet.py` runtime hook | 不执行（见坑 4） |
| 8.3 短路径 | 卷上已禁用（见坑 3） |
| 单文件模式 | 解包失败（见坑 5） |

---

## 5. 验证清单

打包完**必须**跑一遍这些，缺一项就可能交付一个看起来正常、实际残缺的包：

- [ ] exe 能启动，窗口标题正确
- [ ] `/api/adapters` 返回 **25** 个（不是 0！见坑 8）
- [ ] `/api/overview` 的 `pricing.model_count` 是 **30**、`cost_usd` 不为 0（见坑 14）
- [ ] 界面有官方图标和中文名称
- [ ] 数据库落在 `%LOCALAPPDATA%\agent-token-radar\`，**不在**安装目录
- [ ] 窗口尺寸在 4K / 150% 缩放下不超出屏幕
- [ ] 静默安装退出码 0
- [ ] 开始菜单快捷方式存在且目标有效
- [ ] 注册表卸载项存在（`DisplayName` / `DisplayVersion` / `InstallLocation`）
- [ ] 静默卸载后目录、快捷方式、注册表全清
- [ ] 卸载后数据库**保留**（重装不丢历史）
- [ ] 单元测试全过（当前 223 个）

实测结果：

```
adapters: 25
窗口: 1280×860 逻辑像素（4K 150% 屏上物理 1920×1290）
安装: exit 0 → 目录/快捷方式/注册表齐全
卸载: exit 0 → 全清，数据库保留
测试: 149 passed
```

---

## 6. 已知限制

- **只能装到纯 ASCII 路径**。安装程序会检查并拒绝，附中英双语说明。这是坑 1 的直接后果。
- **需要 Windows 10 1809+ / Server 2019+**（WebView2 基线）。
- **只支持 x64**，打包的 exe 是 64 位的。
- **依赖系统的 Edge WebView2 运行时**。Windows 11 自带；Windows 10 绝大多数已随 Edge 安装。
- **未签名**。首次运行 SmartScreen 可能提示「未知发布者」，需要用户点「仍要运行」。

---

## 7. 一句话总结

**打包的难点不在 PyInstaller 本身，而在「运行时环境假设」的静默失效**：

- 路径编码假设（中文路径 → .NET 崩）
- 写入位置假设（`_internal/` 可写 → 装到 Program Files 就废）
- 模块发现假设（文件系统 → PYZ 归档里看不见）
- 像素单位假设（物理 vs 逻辑 → 高分屏溢出）

这四个都是「本地开发完全正常、打包后必炸」的类型，也是这份文档存在的理由。
