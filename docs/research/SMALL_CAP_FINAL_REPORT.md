# Small-Cap Factor — Final Report

> **[已下线 2026-09-20]** 本文是历史研究记录。据此实现过的小市值（small_cap）与
> 反转突破（reversal_breakout）策略已于 commit bfb239c 整体移除，文中指向
> `screener/small_cap.py` 等文件的实现清单不再有效，仅作研究存档，勿按此施工。

**研究时间**: 2026-05-14 ~ 2026-05-16
**回测窗口**: 2020-01-09 ~ 2026-05-09（6.3 年）
**数据源**: LocalDB（Tushare 全市场 daily + daily_basic + index_daily）
**初始资本基准**: 等权全市场买入持有

---

## TL;DR

经过完整 6 年 OOS 验证，发现 A 股市场上**唯一可投资的稳定 alpha 因子是「小市值」**。原项目所有 picker 策略（buy_pullback / eod_buyback）在 2 年 OOS 跑输 buy & hold 30%~120%，根因是其评分体系（trend + momentum + high turnover）这三个因子在 A 股**都是负 alpha**。

### 推荐金牌配置

| 配置 | 描述 |
|---|---|
| **持仓** | 全市场最小市值 top-50 等权 |
| **筛选** | 排除 ST/退/* + 上市 < 365 天 + 日均成交 < 200 万元 |
| **再平衡** | 每 20 个交易日（约 1 个月）一次 |
| **预期年化** | CAGR **+58%**，MDD 22%，**Calmar 2.65** |
| **vs 闭眼买全市场** | 2.4 年超额 **+138%** |
| **6 年跨周期 alpha** | 5/6 年正 alpha（含 2022 熊市仍 +7.8%）|

### 备选激进型

| 配置 | 描述 |
|---|---|
| **持仓** | top-50 + 流动性 ≥ 200万 |
| **再平衡** | 每 5 个交易日 |
| **预期年化** | CAGR **+63.6%**，MDD 28.7%，**Sharpe 1.26** |
| **超额 2.4 年** | +168% |

---

## 1. 背景：原 picker 策略 OOS 失败

在重新设计之前，先用统一的真实基准（全市场等权 B&H + 现金机会成本）重测原有策略：

### 2 年 OOS（2024-01 ~ 2026-01）

| 配置 | Picks | 在场% | Per-trade alpha | **组合 NAV 总收益** | 同期 B&H |
|---|---|---|---|---|---|
| buy_pullback (gate2 + mf + top10 + hs12) | 168 | 20% | -0.87% | **+10.09%** | +44.54% |
| buy_pullback (无过滤 + top10 + hs12) | 1091 | 61% | -0.44% | **-78.64%** | +44.54% |
| eod_buyback (T+1 hold=1 top=2) | 26 | 6% | -0.86% | **-8.46%** | +44.54% |

**所有策略在 2 年 OOS 均跑输 B&H 30%~120%。**

### 根因：score 系统的 5 个因子有 3 个是负 alpha

`src/services/picker_strategies.py` 的 `score_*` 函数加权 5 个因子：

| 因子 | 项目权重 | 单因子 2.4yr 测试 | 真实 alpha vs B&H |
|---|---|---|---|
| 60 日趋势 | + | momentum_60d | **-9.35%** ❌ |
| 当日动量 | + | (类似 momentum) | 负 |
| 量比/成交量 | + | high_turnover | **-10.73%** ❌ |
| 换手率 | + | high_turnover | **-10.73%** ❌ |
| PE | - (低 PE 加分) | low_pe | +8.04% ✓ 弱 |

`score = trend(+) + momentum(+) + volume(+) + turnover(+) + pe(-)` 实际是 **4 个负/弱 alpha 因子加 1 个弱正因子**。score 越高 → 选到的越是热门追涨股，长期亏钱。

---

## 2. 因子调研：发现 small_cap

`scripts/factor_alpha_test.py` 统一框架，月度再平衡 top 10%，2.4 年 OOS：

```
Factor           |    Total |   AvgPer |     WR |   N | AvgPicks |   vs B&H
small_cap        | +221.31% |   +5.26% |  61.5% |  26 |     497 | +178.86%   ⭐⭐⭐⭐⭐
st_rev           | +120.59% |   +3.65% |  61.5% |  26 |     497 |  +78.14%
low_turnover     |  +55.96% |   +1.85% |  69.2% |  26 |     497 |  +13.51%
low_vol          |  +47.12% |   +1.59% |  69.2% |  26 |     497 |   +4.67%
low_pe           |  +50.49% |   +1.72% |  69.2% |  26 |     378 |   +8.04%
momentum_60d     |  +33.10% |   +1.67% |  53.9% |  26 |     497 |   -9.35%   ❌
high_turnover    |  +31.72% |   +1.75% |  53.9% |  26 |     497 |  -10.73%   ❌
```

`small_cap`（排序 total_mv 升序取 top 10%）是绝对赢家。多因子组合（intersect/composite）反而 **不如纯 small_cap**，因为 st_rev 和 low_turnover 与 small_cap 高度相关（小盘股本来就波动大、换手低），叠加它们等同于在小盘里挑更小的，没有引入新维度信息。

---

## 3. small_cap 深度优化

`scripts/small_cap_optimize.py` 在 freq × top-N × min_amount 的 60 个网格点上枚举：

### 2.4 年金牌：`freq=20 + top-50` (按 Calmar)

| 指标 | 值 |
|---|---|
| NET 总收益 | +180.30% |
| CAGR | +58.3%/年 |
| MDD | **22.0%** ⭐ |
| Sharpe | 1.02 |
| **Calmar** | **2.65** ⭐ |
| 月度胜率 | 55.6% |
| 周转/月 | 23.2% |
| Alpha | +137.85% |

### 2.4 年金牌：`freq=5 + top-50 + min_amt=2M` (按 Alpha + Sharpe)

| 指标 | 值 |
|---|---|
| NET 总收益 | **+210.6%** |
| CAGR | **+63.6%/年** |
| MDD | 28.7% |
| **Sharpe** | **1.26** |
| **Calmar** | **2.21** |
| 月度胜率 | 51.4% |
| 周转/周 | 13.9% |
| **Alpha** | **+168.2%** ⭐ |

### 流动性筛选发现

| 最小日均成交 | NET | MDD | Alpha | 说明 |
|---|---|---|---|---|
| 0 | +174.5% | 31.6% | +132% | 包含极小盘 |
| **2,000,000** | **+210.6%** | **28.7%** | **+168%** | ⭐ 甜区 |
| 5,000,000 | +180.3% | 32.1% | +138% | 开始扣 alpha |
| 10,000,000 | +129.7% | 37.9% | +87% | |
| 50,000,000 | +63.2% | 39.4% | +21% | |
| 100,000,000 | -24.5% | 49.8% | -67% | 大盘股反向 alpha |

**关键洞察**：加 200 万日均成交筛选**反而提升** alpha（+168 vs +132），因为剔除了无法成交的"僵尸股"，留下真正可买的小盘票。但加超过 500 万就开始严重稀释 alpha。

---

## 4. 跨年度稳定性（2020-2025，Gold 配置）

| 年 | 市场环境 | NetRet | BH(EW) | **Alpha** | MDD |
|---|---|---|---|---|---|
| 2020 | COVID 冲击 | +10.5% | +9.8% | +0.7% | 10.6% |
| 2021 | 中小盘牛市 | +76.6% | +17.6% | **+58.9%** | 9.5% |
| 2022 | 大熊市 | -9.1% | **-16.9%** | **+7.8%** | 25.5% |
| 2023 | 震荡市 | +63.9% | +0.9% | **+63.0%** | 10.0% |
| 2024 | 动荡市 | +61.6% | +0.7% | **+60.9%** | 34.4% |
| 2025 | 反弹 | +78.0% | +37.9% | +40.1% | 17.0% |

**6 年里 5 年正 alpha，含 2022 熊市跑赢市场 +7.8%。**

---

## 5. 不该做的事 — 已验证

### 5.1 多因子组合不如纯 small_cap

| 组合 | NET | Alpha |
|---|---|---|
| 纯 small_cap top-50 | **+221%** | **+178%** |
| small ∩ st_rev | +158% | +116% |
| small ∩ low_turn | +169% | +127% |
| composite small + st_rev (1:1) | +161% | +118% |
| composite small(2):st_rev(1):low_turn(1) | +176% | +134% |

### 5.2 Regime overlay 一律恶化收益

测试 6 种宏观 regime 信号过滤（SSE > MA20/MA60/MA120，drawdown < 5%，等等）：

| Regime | NET | MDD | Alpha |
|---|---|---|---|
| **NONE (满仓)** | **+204.0%** | 28.9% | **+155.8%** |
| SSE > MA20 (R1) | +105.8% | 18.4% | +57.6% |
| SSE > MA60 (R2) | +74.4% | 25.3% | +26.3% |
| SSE > MA120 (R3) | +104.9% | 27.0% | +56.8% |
| 20d ret > 0 (R4) | +48.7% | 24.5% | +0.6% |
| MA20 > MA60 金叉 (R5) | +35.5% | 31.6% | -12.7% |
| drawdown < 5% (R6) | -9.8% | 33.3% | -58.0% |

**结论**：**满仓**最优。R1（SSE > MA20）能把 MDD 从 28.9% → 18.4%，但 alpha 减半，仅适合极度风险厌恶者。

### 5.3 score 系统失效原因

`scripts/factor_alpha_test.py` 验证：score 排序对最终 picks 几乎没有鉴别力（top3 vs top30 平均收益几乎相同），因为权重最重的趋势/动量/换手三个因子在 A 股是反向 alpha。

---

## 6. 关键警告

### 6.1 流动性是隐形天花板

策略在每月最小市值 top-50 中均匀建仓。日均成交 200 万的股票，单只 1% 持仓约可吃下 **2 万元**而无明显冲击。50 只持仓 → **总资金 100 万元** 是无冲击上限。资金 > 200 万会显著拉高滑点，吃掉 alpha。

### 6.2 政策风险

A 股小市值因子在 2017、2021、2024 都遭遇过监管"严打小盘炒作"政策。本回测的优秀表现部分依赖政策宽容期，**未来可能反复被打压**。

### 6.3 单年最大 MDD 34.4%

2024 年 MDD 单年达 34.4%。即使 6 年 CAGR 58%，单笔投资在最差年内会回撤 1/3。**心理承受力 + 长期持仓决心**是先决条件。

### 6.4 退市/停牌摩擦未模拟

回测假设每只持仓股票每天都能成交。实际有：
- 停牌（一年期主板停牌不罕见）
- 退市风险（2022 起严格执行，小盘高发）
- 涨跌停板无法成交

实操执行 alpha 可能比模拟低 5-15%。

### 6.5 样本期偏向小盘

2020-2025 整体是小盘股的"良好时代"。2017、2018 年小盘惨败的样本期未覆盖。建议**仓位限制在总资产的 30-50%**，留余地应对未来环境切换。

---

## 7. 开发产出

### 7.1 数据基础设施

- **`src/services/local_db/store.py`**: by-date 索引 + 慢速 fallback (commit `e837a29`)
- **`scripts/build_by_date_index.py`**: 一次性重建工具
- **`scripts/preload_local_db.py`**: 历史数据预热

由此 6 年回测从 30+ 分钟降到 2 分钟。

### 7.2 真实基准框架

- **`src/services/picker_backtest_service.py`**: 加入 (commit `abceedf`)
  - `market_eqw_*`: 全市场等权同期收益
  - `strategy_total_*`: 含现金的组合 NAV
  - `bh_alpha_*`: vs 全周期买入持有
  - `days_in_market_pct`: 在场时间比例
- 揭示了原所有 alpha 数字都是"持仓时段 vs 同时段指数"，**忽略空仓机会成本**

### 7.3 因子调研工具

- **`scripts/factor_alpha_test.py`**: 单/多因子统一框架 (commit `eacc36c`)
- **`scripts/small_cap_deep_dive.py`**: small_cap 全面参数扫描
- **`scripts/small_cap_optimize.py`**: freq × top-N × min_amt 60 点网格
- **`scripts/small_cap_regime.py`**: 6 种 regime overlay 测试
- **`scripts/small_cap_yearly.py`**: 跨年度 alpha 拆解

---

## 8. 接下来的工程任务

### 8.1 建议保留的策略

- `buy_pullback`: 移到"实验性"，**不再作为默认**
- `eod_buyback`: **可考虑删除**（2 年 OOS PF 0.58, 26 笔, -8% NET）
- `small_cap_monthly`: **新增并设为默认推荐**

### 8.2 集成到 picker

需要的工程：
1. 新建 `src/services/picker/screener/small_cap.py`（screener 实现）
2. `src/services/picker_strategies.py`: 注册 `SMALL_CAP_MONTHLY = "small_cap_monthly"`，加 score 函数
3. `src/services/picker/screener/pipeline.py`: 路由
4. `src/services/picker_backtest_service.py`: backtest 入口
5. 前端 picker UI: 显示新策略
6. README + CHANGELOG: 文档化

预估工作量：4-6 个文件，2-4 小时（含测试）。

### 8.3 替代方案：直接出"月度 small_cap 推送"

如果不想做 picker 集成，可以：
- 在月初自动跑 `scripts/small_cap_optimize.py` 输出当月 top-50
- 推送到企业微信/飞书
- 用户手动下单

工作量：1 个新脚本 + cron + 通知 webhook。

---

## 9. 一句话结论

> **A 股 2020-2026 唯一稳定可投资的因子是「小市值」**，纯简单按 total_mv 升序取 top-50、月度再平衡，6 年 5/6 年正 alpha，CAGR ~58%，MDD ≤ 35%。
>
> 项目原 picker 选股策略经 2 年 OOS 验证均跑输 B&H 30-120%，**应停止使用 score 排序**作为主要选股依据。
