# 参考费用（pricing）维护说明

本工具**测量** Token，费用是**推导**出来的：`Token × 厂商官方标价`。
这决定了它的全部设计约束——费用永远不会比价格表更可信。

## 四条硬规则

1. **查不到官方价的 Model，不计费用，标注「无公开价」。**
   绝不从名字相近的模型插值。`hy4std`、`space-bunny`、`GLM-5-Base`
   都没有公开单价，它们贡献 Token 但不贡献金额，覆盖率随之下降。
2. **价格表是数据，不是代码。** 见 `agent_token_radar/pricing.json`。
   厂商调价只改这个文件，不改任何 Python。
3. **费用列 NULL 不等于 0。** `SUM()` 会跳过 NULL，所以未定价的用量
   不会被算成「免费」，而是体现在 `priced_ratio`（覆盖率）里。
   界面必须把覆盖率一起显示，否则部分金额会被读成完整账单。
4. **分时计价的模型，按每条请求自己的时间判断峰谷。**
   不是按「现在」，也不是按当天均价。一条 02:00 UTC 的记录永远是峰时价，
   哪怕报表是在周末生成的。

## 币种：只用官网的美元价

**表里每一格都是美元，任何地方都不做汇率换算。**

Zhipu 和 Qwen 的国内站以人民币标价，但它们**都有国际站的美元定价页**，
直接读那个数即可。这样做的好处是不引入任何汇率假设：

- 美元价是厂商自己对国际结算的开价，不是我们折算出来的
- 汇率会漂移，而价格表有读取日期；混在一起就说不清哪部分在变
- 少一个 `FX_USD_PER_CNY` 常量，就少一处「这个数怎么来的」的解释

早期版本确实存过 CNY 再按固定汇率折美元，已全部改掉。

## 分时计价：DeepSeek

DeepSeek 是唯一按时间计价的厂商。官网脚注原文：

> Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and
> 06:00 - 10:00 UTC, Monday through Friday, excluding Chinese public holidays.
> All other hours are off-peak, including weekends and Chinese public holidays
> in full.

写成数据就是 `pricing.json` 的 `peak_hours.deepseek`：

| 项 | 值 |
|---|---|
| 峰时窗口（UTC） | 01:00–04:00、06:00–10:00 |
| 只算工作日 | 是（周一到周五） |
| 排除日期 | 中国法定节假日 |
| 低谷倍率 | 0.5 |

**边界是左闭右开**：04:00 整算低谷。`test_window_boundaries` 锁住这一点。

**节假日清单**取的是法定节假日（2026 年 6 天），不含调休上班日——
调休日仍是普通计费日。国务院公布的安排里还有「连休搭桥」日，
这张表**不试图覆盖**：真遇上搭桥放假的那天，会因为按工作日算了峰时价而略偏高。
每年需要更新一次这个清单。

**实现在 SQL 里，不是 Python 里。** 因为费用是聚合查询算的：
七处 `COST_ROW_SQL` 都要逐行判断，Python 侧算不了。窗口和节假日落在
`price_peak` / `price_peak_skip` 两张小表里，SQL 只写一份通用判断
（`pricing.PEAK_HOURS_SQL`）。改窗口 = 改 JSON，连接打开时重建，无需迁移。

`PeakSchedule.is_peak()` 是同一套规则的 Python 版，只用于测试和 API 元数据，
不参与实际计费——两个实现必须给出相同答案，`test_cost.py` 两侧都测。

## 改价的实测影响（2026-11-22 本机）

| 场景 | 金额 |
|---|---|
| DeepSeek 全部按峰时算（旧行为） | $280.94 |
| DeepSeek 按请求时间区分峰谷 | $166.88 |
| 全库按峰时算（旧） | $1,989.50 |
| 全库按峰谷区分（现） | $1,875.35 |

DeepSeek 有 52,539 条低谷请求（11.8B Token）和 11,795 条峰时请求（2.36B Token），
也就是说**大头其实发生在低谷时段**，一律按峰时价会高估 40%。

## 数据从哪来

价格全部从厂商**官方定价页**读取，每条都记了来源 URL 和读取日期：

| vendor | 来源 | 币种 |
|---|---|---|
| deepseek | https://api-docs.deepseek.com/quick_start/pricing | USD |
| openai | https://developers.openai.com/api/docs/pricing | USD |
| anthropic | https://platform.claude.com/docs/en/about-claude/pricing | USD |
| kimi | https://platform.kimi.ai/docs/pricing/chat | USD |
| zhipu | https://docs.z.ai/guides/overview/pricing | USD（国际站） |
| qwen | https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash | USD（北京区） |

读价时的口径：**Standard / 短上下文 / 峰时**。OpenAI 的 Long context、
Batch、Fast、Ultrafast 四档差价很大，不混用。DeepSeek 表里存峰时价，
低谷价单列在同一条的 `offpeak` 块里。

**Qwen 的价按区域走**：北京区 input 0.113 / output 0.382，新加坡与香港是
0.15 / 0.47。本机日志走的是北京区，所以取北京区；换了区域这条要跟着改。

## 模型名的匹配

Agent 日志里的模型名和厂商的 model id 经常不一致。`PriceTable.resolve()`
只做**保守**归一化，绝不相似匹配：

- 精确命中
- 大小写不敏感命中
- 去掉网关注入的后缀：`-0731`、`-expires-on-0910`、`-sg`
- 显式别名（表里单独写了一行）：`workbuddy/kimi-k3.1-pro`、`kimi-k3-1`

这条边界是刻意的：`gpt-6.1-terra` **不得**落进 `gpt-6.1-sol` 的价。
`test_cost.py::test_alias_does_not_fall_through_to_a_different_model` 锁住它。

## 加一条价格

1. 到厂商官方定价页确认 input / output / cache_read（必要还有 cache_write）。
   **优先找带美元的那一版**，找不到再考虑别的方式，不要引汇率。
2. 在 `pricing.json` 的 `models` 里加一条，把 `matched` 写成官方 model id。
3. 如果这条价格来自新的厂商，在 `sources` 里补一行 URL 和读取日期。
4. 若该厂商分时计价，在 `peak_hours` 里加它的窗口，并在模型条目里写 `offpeak` 块。
5. 跑 `python -m unittest tests.test_cost`。
6. 重启服务，价格与窗口在每次连接打开时重建，改价即时生效。

## 覆盖率怎么读

`priced_ratio` = 有官方价的 Token ÷ 全部 Token。

2026-11-22 实测：总覆盖率 **96.5%**，估算 **$1,875.35**（已含峰谷区分）。
剩下 3.5% 是 WorkBuddy 的 `hy4*` 系列、`space-bunny`、`mimo-v2.5`、
`gemini-3.8-flash-n`、`GLM-5-Base` 等没有公开标价的型号。

WorkBuddy 的产品计费是**积分**，日志里虽有 `credit` 字段，
但实测 126 条里只有 1 条非零，拿不到可用数值，因此不显示积分。
它的模型若在官网上有美元价（如 `deepseek-v4.1-flash`），仍按该价估算。
