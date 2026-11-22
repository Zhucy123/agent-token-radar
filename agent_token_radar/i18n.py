"""Dashboard localisation: Simplified Chinese (default) and English.

Rules the user gave:
  * Chinese UI shows Chinese only.
  * English UI shows English only.
  * Domain nouns stay in English in BOTH languages: ``Token``, ``Agent``,
    ``Model``, ``Session``, ``Cache``, ``Input``, ``Output``, ``Reasoning``.
    So a Chinese label reads like "按 Agent 分布", never "按 智能体 分布".

Two separate dictionaries (not a key + fallback chain) so a missing English
string can never silently render Chinese.

Usage:
    from .i18n import t, STRINGS
    t("zh", "nav.today")   -> "今日"
    t("en", "nav.today")   -> "Today"
"""

from __future__ import annotations

__all__ = ["SUPPORTED", "DEFAULT_LANG", "STRINGS", "t", "normalize_lang"]

SUPPORTED: tuple[str, ...] = ("zh", "en")
DEFAULT_LANG = "zh"

# --------------------------------------------------------------------------
# Chinese (Simplified)
# --------------------------------------------------------------------------
_ZH: dict[str, str] = {
    # shell
    "app.title": "Agent Token Radar",
    "app.tagline": "本地 · 只读 · Token 统计与参考费用",
    "skip": "跳到主内容",
    # toolbar
    "toolbar.range": "时间范围",
    "range.today": "今日",
    "range.7d": "近 7 天",
    "range.30d": "近 30 天",
    "range.all": "全部",
    "toolbar.refresh": "刷新扫描",
    "toolbar.autoscan": "自动刷新",
    "toolbar.lang": "语言",
    "toolbar.theme": "主题",
    # Settings dialog. One set of keys per setting type, plus the generic
    # dialog chrome, so adding a setting is a config entry + its labels here.
    "settings.open": "设置",
    "settings.title": "设置",
    "settings.close": "关闭",
    "settings.reset": "恢复默认",
    "settings.fontSize": "字号",
    "settings.fontSize.hint": "整体缩放界面字号，不影响浏览器自身的缩放设置。",
    "settings.fontFamily": "字体",
    "settings.fontFamily.hint": "只用本机已安装的字体，不加载网络字体。",
    "settings.fs.small": "小",
    "settings.fs.normal": "标准",
    "settings.fs.large": "大",
    "settings.fs.xlarge": "特大",
    "settings.ff.system": "系统默认",
    "settings.ff.sans": "无衬线",
    "settings.ff.serif": "衬线",
    "settings.ff.mono": "等宽",
    "settings.ff.system.hint": "当前效果，跟随系统界面字体",
    # Typeface groups in the dropdown. The face names themselves (Segoe UI,
    # Georgia, 微软雅黑 …) are proper nouns and stay identical in both languages;
    # only these group headings are translated.
    "settings.ff.g.default": "默认",
    "settings.ff.g.sans": "无衬线（西文）",
    "settings.ff.g.serif": "衬线（西文）",
    "settings.ff.g.cjk": "中文",
    "settings.ff.g.mono": "等宽",
    "settings.ff.segoe": "Segoe UI（系统界面字体）",
    "settings.ff.arial": "Arial",
    "settings.ff.verdana": "Verdana（宽字距，易读）",
    "settings.ff.tahoma": "Tahoma（紧凑）",
    "settings.ff.trebuchet": "Trebuchet MS（柔和）",
    "settings.ff.calibri": "Calibri（办公软件默认）",
    "settings.ff.candara": "Candara（圆润）",
    "settings.ff.corbel": "Corbel（清秀）",
    "settings.ff.georgia": "Georgia（屏幕衬线，易读）",
    "settings.ff.times": "Times New Roman",
    "settings.ff.cambria": "Cambria（屏幕衬线）",
    "settings.ff.palatino": "Palatino Linotype（古典衬线）",
    "settings.ff.yahei": "微软雅黑",
    "settings.ff.dengxian": "等线",
    "settings.ff.simhei": "黑体",
    "settings.ff.simsun": "宋体",
    "settings.ff.kaiti": "楷体",
    "settings.ff.fangsong": "仿宋",
    "settings.ff.notoSans": "Noto Sans SC（思源黑体）",
    "settings.ff.cascadia": "Cascadia Code（编程等宽）",
    "settings.ff.consolas": "Consolas（编程等宽）",
    "settings.ff.lucida": "Lucida Console（等宽）",
    "status.loading": "正在加载…",
    "status.scanning": "正在扫描（只读读取各 Agent 数据…）",
    "status.scanSkipped": "上一轮扫描还在进行中，已跳过",
    "status.loaded": "已加载 · 数据库 {rows} 条记录 · {time}",
    "status.scanDone": "扫描完成 · 新增 {new} 条，更新 {updated} 条 · {time}",
    "status.loadFailed": "加载失败：{err}",
    "status.scanFailed": "扫描失败：{err}",
    # summary
    "sec.summary": "汇总",
    # Chinese mode shows Chinese as the headline; the English field name drops
    # to the sub-line, because Token accounting is read against English terms.
    "card.total": "总用量",
    "card.total.sub": "Total · 全部 Token",
    "card.cacheHit": "缓存命中",
    "card.cacheHit.sub": "Cache Hit · 缓存读取占全部输入",
    "card.input": "输入",
    "card.input.sub": "Input · 未走 Cache 的部分",
    "card.output": "输出",
    "card.output.sub": "Output · 由 Model 生成",
    "card.cacheRead": "缓存读取",
    "card.cacheRead.sub": "命中缓存的部分",
    "card.reasoning": "推理",
    "card.reasoning.sub": "Output 的细分，不重复计入总量",
    "card.cost": "参考费用",
    "card.cost.sub": "Estimate · 按官方标价估算",
    "cost.coverage": "覆盖 {pct} 的 Token",
    "cost.coverageTip": "只有查到官方标价的 Model 才计入；其余按「无公开价」处理，不猜测单价。估算值不代表实际账单。",
    "cost.unpriced": "无公开价",
    "cost.perMillion": "每百万 Token",
    "cost.peakTag": "（峰时）",
    "cost.offpeakTag": "低谷",
    "summary.note": "注：Total = 输入 + 缓存读取 + 缓存写入 + 输出；推理是 Output 的细分项，不重复计入。输入指未命中 Cache 的部分；缓存命中 = 缓存读取 ÷（输入 + 缓存读取）。费用 = 各厂商官方标价 × Token，仅作量级参考，不代表实际账单；无官方标价的 Model 不计入。",
    # trend
    "sec.trend": "消耗趋势（按 Agent 堆叠）",
    "sec.trendShare": "消耗占比（按 Agent）",
    "trend.aria": "按 Agent 堆叠的每日 Token 消耗趋势图",
    "trend.noData": "暂无数据",
    "trend.shareHead": "{day} 各 Agent 占比 · 合计 {total}",
    "chart.more": "还有 {n} 个 Agent",
    # rankings
    "sec.byAgent": "按 Agent 分布",
    "sec.byModel": "按 Agent / Model 细分",
    "sec.topSessions": "消耗最高的 Top Session",
    "sec.byProject": "按项目分布",
    "cap.byProject": "各项目（Agent 的工作目录）的 Token 用量",
    "col.project": "项目",
    "project.none": "(未记录目录)",
    "project.coverage": "有 {with} / {total} 条记录带项目目录（{pct}）",
    "project.explain": "项目取自各 Agent 记录的工作目录；同一目录用不同 Agent 打开会合并成一行，点开可看各 Agent 的用量。",
    "project.agents": "个 Agent",
    "project.expandHint": "展开 / 收起各 Agent 的用量",
    "cap.byAgent": "各 Agent 的 Token 用量排行",
    "cap.byModel": "每个 Agent 下各 Model（及 Provider）的 Token 用量",
    "cap.topSessions": "Token 消耗最高的 Session",
    "col.agent": "Agent",
    "col.model": "Model",
    "col.provider": "Provider",
    "col.events": "请求数",
    "col.total": "总量",
    "col.cacheHit": "缓存命中",
    "col.input": "输入",
    "col.output": "输出",
    "col.cacheRead": "缓存读取",
    "col.cost": "参考费用",
    "col.unitPrice": "单价",
    "col.session": "Session",
    "col.lastSeen": "最后活动",
    "empty.table": "暂无数据 —— 请先执行一次扫描",
    "model.unknown": "(未记录 Model)",
    "model.routedFrom": "请求名 {alias}",
    "model.aliasHint": "Agent 日志里记录的是路由别名 {alias}，本机网关实测实际服务的是上面的 Model",
    # provider provenance: measured from a record that states it (or from the
    # local magpie gateway's per-request log), versus guessed from the model name.
    "prov.measured": "实测",
    "prov.measuredHint": "Provider 来自记录本身或本机 magpie 网关的逐请求日志",
    "prov.product": "自有后端",
    "prov.productHint": "该 Agent 自己的服务就是 Provider（如 Codex Desktop 直连 Codex 后端），按产品身份判定，不是从模型名猜的",
    "prov.inferred": "推断",
    "prov.inferredHint": "记录里没有 Provider，按 Model 名推断，未必准确",
    "prov.unknown": "未知",
    "prov.unknownHint": "记录里没有 Provider，也无法从 Model 名推断",
    "prov.statsMeasured": "Provider 实测 {measured} 条",
    "prov.statsProduct": "自有后端 {product} 条",
    "prov.statsInferred": "推断 {inferred} 条（已标注）",
    "prov.statsUnknown": "未知 {unknown} 条",
    # footer
    "footer.sourcesLead": "数据源",
    "footer.caveat": "以上来源均对第三方目录严格只读",
    "footer.icons": "图标均为各 Agent 官方标识（simple-icons 或厂商站点资源）。",
    "footer.pricing": "价格取自各厂商官方定价页（更新于 {date}）：{vendors}。未收录的 Model 不估算。",
    "footer.peak": "分时计价按每条请求自身的时间判断：{schedule} 为峰时，其余时段与周末、中国法定节假日为低谷，按半价计。",
    "footer.peakRange": "{vendor} {windows} UTC，周一至周五",
    "icon.official": "官方图标",
    "icon.unofficial": "非官方图形",
}

# --------------------------------------------------------------------------
# English
# --------------------------------------------------------------------------
_EN: dict[str, str] = {
    "app.title": "Agent Token Radar",
    "app.tagline": "Local · read-only · Token usage & cost estimates",
    "skip": "Skip to main content",
    "toolbar.range": "Time range",
    "range.today": "Today",
    "range.7d": "Last 7 days",
    "range.30d": "Last 30 days",
    "range.all": "All time",
    "toolbar.refresh": "Rescan",
    "toolbar.autoscan": "Auto refresh",
    "toolbar.lang": "Language",
    "toolbar.theme": "Theme",
    "settings.open": "Settings",
    "settings.title": "Settings",
    "settings.close": "Close",
    "settings.reset": "Reset to defaults",
    "settings.fontSize": "Text size",
    "settings.fontSize.hint": "Scales the whole interface. Your browser's own zoom setting is left alone.",
    "settings.fontFamily": "Typeface",
    "settings.fontFamily.hint": "Only fonts already installed on this machine; no web fonts are loaded.",
    "settings.fs.small": "Small",
    "settings.fs.normal": "Default",
    "settings.fs.large": "Large",
    "settings.fs.xlarge": "Extra large",
    "settings.ff.system": "System default",
    "settings.ff.sans": "Sans-serif",
    "settings.ff.serif": "Serif",
    "settings.ff.mono": "Monospace",
    "settings.ff.g.default": "Default",
    "settings.ff.g.sans": "Sans-serif (Latin)",
    "settings.ff.g.serif": "Serif (Latin)",
    "settings.ff.g.cjk": "Chinese",
    "settings.ff.g.mono": "Monospace",
    "settings.ff.segoe": "Segoe UI (Windows interface face)",
    "settings.ff.arial": "Arial",
    "settings.ff.verdana": "Verdana (wide, legible)",
    "settings.ff.tahoma": "Tahoma (compact)",
    "settings.ff.trebuchet": "Trebuchet MS (soft)",
    "settings.ff.calibri": "Calibri (Office default)",
    "settings.ff.candara": "Candara (rounded)",
    "settings.ff.corbel": "Corbel (crisp)",
    "settings.ff.georgia": "Georgia (screen serif, legible)",
    "settings.ff.times": "Times New Roman",
    "settings.ff.cambria": "Cambria (screen serif)",
    "settings.ff.palatino": "Palatino Linotype (classical serif)",
    "settings.ff.yahei": "Microsoft YaHei",
    "settings.ff.dengxian": "DengXian",
    "settings.ff.simhei": "SimHei",
    "settings.ff.simsun": "SimSun",
    "settings.ff.kaiti": "KaiTi",
    "settings.ff.fangsong": "FangSong",
    "settings.ff.notoSans": "Noto Sans SC",
    "settings.ff.cascadia": "Cascadia Code (programming mono)",
    "settings.ff.consolas": "Consolas (programming mono)",
    "settings.ff.lucida": "Lucida Console (monospace)",
    "settings.ff.system.hint": "Current look, follows the OS interface font",
    "status.loading": "Loading…",
    "status.scanning": "Scanning (reading Agent data read-only…)",
    "status.scanSkipped": "Previous scan still running; skipped",
    "status.loaded": "Loaded · {rows} records in database · {time}",
    "status.scanDone": "Scan finished · {new} new, {updated} updated · {time}",
    "status.loadFailed": "Failed to load: {err}",
    "status.scanFailed": "Scan failed: {err}",
    "sec.summary": "Summary",
    "card.total": "Total",
    "card.total.sub": "All Token",
    "card.cacheHit": "Cache hit",
    "card.cacheHit.sub": "Cache Hit · cached share of all input",
    "card.input": "Input",
    "card.input.sub": "Uncached input",
    "card.output": "Output",
    "card.output.sub": "Generated output",
    "card.cacheRead": "Cache Read",
    "card.cacheRead.sub": "Cache hits",
    "card.reasoning": "Reasoning",
    "card.reasoning.sub": "Subset of Output, never added twice",
    "card.cost": "Est. cost",
    "card.cost.sub": "Estimate · official list prices",
    "cost.coverage": "covers {pct} of Token",
    "cost.coverageTip": "Only Models with a published official price are counted. Anything else is treated as unpriced rather than guessed. An estimate is not a bill.",
    "cost.unpriced": "No public price",
    "cost.perMillion": "per 1M Token",
    "cost.peakTag": " (peak)",
    "cost.offpeakTag": "Off-peak",
    "summary.note": "Note: Total = Input + Cache Read + Cache Write + Output; Reasoning is a subset of Output. Input means the part that missed the cache. Cache hit = Cache Read ÷ (Input + Cache Read). Cost = each vendor's official list price × Token, for scale only -- it is not an actual bill, and Models with no published price are left out.",
    "sec.trend": "Usage trend (stacked by Agent)",
    "sec.trendShare": "Usage share (by Agent)",
    "trend.aria": "Daily Token usage trend stacked by Agent",
    "trend.noData": "No data",
    "trend.shareHead": "{day} by Agent · {total} total",
    "chart.more": "{n} more Agents",
    "sec.byAgent": "Distribution by Agent",
    "sec.byModel": "Usage by Agent / Model",
    "sec.topSessions": "Top Sessions by usage",
    "sec.byProject": "Distribution by project",
    "cap.byProject": "Token usage per project (the Agent's working directory)",
    "col.project": "Project",
    "project.none": "(no directory recorded)",
    "project.coverage": "{with} of {total} records carry a project directory ({pct})",
    "project.explain": "A project is the working directory the Agent ran in; the same folder opened by several Agents is merged into one row. Expand it to see each Agent's usage.",
    "project.agents": "Agents",
    "project.expandHint": "Expand / collapse each Agent's usage",
    "cap.byAgent": "Token usage ranking per Agent",
    "cap.byModel": "Token usage per Agent, broken down by Model and Provider",
    "cap.topSessions": "Sessions with the highest Token usage",
    "col.agent": "Agent",
    "col.model": "Model",
    "col.provider": "Provider",
    "col.events": "Events",
    "col.total": "Total",
    "col.cacheHit": "Cache hit",
    "col.input": "Input",
    "col.output": "Output",
    "col.cacheRead": "Cache Read",
    "col.cost": "Est. cost",
    "col.unitPrice": "Unit price",
    "col.session": "Session",
    "col.lastSeen": "Last active",
    "empty.table": "No data yet — run a scan first",
    "model.unknown": "(no model recorded)",
    "model.routedFrom": "requested as {alias}",
    "model.aliasHint": "The Agent logged the routing alias {alias}; the local gateway measured the Model above as what actually served it",
    "prov.measured": "measured",
    "prov.measuredHint": "Provider came from the record itself or from the local magpie gateway's per-request log",
    "prov.product": "own backend",
    "prov.productHint": "This Agent's own service is the Provider (e.g. Codex Desktop talks to the Codex backend directly); decided by the product, not guessed from the model name",
    "prov.inferred": "inferred",
    "prov.inferredHint": "The record states no Provider; derived from the Model name and may be wrong",
    "prov.unknown": "unknown",
    "prov.unknownHint": "The record states no Provider and the Model name does not imply one",
    "prov.statsMeasured": "Provider measured for {measured} rows",
    "prov.statsProduct": "own backend for {product}",
    "prov.statsInferred": "inferred for {inferred} (marked)",
    "prov.statsUnknown": "unknown for {unknown}",
    "footer.sourcesLead": "Sources",
    "footer.caveat": "Every source above is strictly read-only towards third-party directories",
    "footer.icons": "Every icon is the vendor's own mark (simple-icons glyph or a vendor site asset).",
    "footer.pricing": "Prices come from each vendor's official pricing page (read {date}): {vendors}. Models not listed there are never estimated.",
    "footer.peak": "Time-of-day rates are applied per request, from that request's own timestamp: {schedule} is peak, all other hours plus weekends and Chinese public holidays are off-peak at half price.",
    "footer.peakRange": "{vendor} {windows} UTC, Monday to Friday",
    "icon.official": "Official icon",
    "icon.unofficial": "Unofficial mark",
}

STRINGS: dict[str, dict[str, str]] = {"zh": _ZH, "en": _EN}


def normalize_lang(value: str | None) -> str:
    """Map any input to a supported language code (defaults to Chinese)."""
    if not value:
        return DEFAULT_LANG
    v = str(value).strip().lower()
    if v.startswith("zh"):
        return "zh"
    if v.startswith("en"):
        return "en"
    return DEFAULT_LANG


def t(lang: str, key: str, **fmt: object) -> str:
    """Translate one key. Unknown keys return the key itself (visible bug)."""
    table = STRINGS.get(normalize_lang(lang), _ZH)
    template = table.get(key)
    if template is None:
        return key
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            return template
    return template
