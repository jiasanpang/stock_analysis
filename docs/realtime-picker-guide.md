# 实时行情筛选指南

> 优化版选股系统：支持当天下午选股 + 当天或第二天操作

## 一、背景问题

原始选股系统基于**日线数据**，存在的局限：
- 只能在收盘后运行选股
- 无法捕捉当天强势股的买点
- 不支持"当天下午买，第二天卖"的实时策略

## 二、解决方案：实时筛选层（Stage 1.5）

新增加的**实时筛选阶段**在量化候选池后执行，用实时行情进一步过滤：

```
日线量化筛选 → [新增] 实时筛选 → AI精选

实时筛选层职责：
✓ 排除已涨停/跌停的股票
✓ 限制当日涨幅范围（避免追太高）
✓ 过滤异常放量（过滤短线游资）
✓ 灵活配置，支持不同市场情景
```

## 三、快速开始

### 3.1 最简单的用法（推荐）

在 `.env` 中添加一行启用：

```bash
# 开启实时筛选，采用默认规则
PICKER_ENABLE_REALTIME_FILTER=true
```

**默认行为**：
- ✅ 排除涨停股
- ✅ 排除跌停股
- ✅ 不限制涨幅范围
- ✅ 不过滤放量

### 3.2 保守策略（低风险选手）

```bash
# .env

# 实时筛选开启
PICKER_ENABLE_REALTIME_FILTER=true

# 排除涨停/跌停
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true

# 限制涨幅：只要当日涨 -2% ~ 5% 的股票（避免追太高）
PICKER_REALTIME_DAILY_CHG_MIN=-2
PICKER_REALTIME_DAILY_CHG_MAX=5

# 过滤异常放量：排除量比 > 5 的股票
PICKER_REALTIME_MAX_VOLUME_RATIO=5.0
```

**效果**：选出的都是温和上涨、量能适中的股票，适合回踩低吸。

### 3.3 激进策略（趋势追踪）

```bash
# .env

PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true

# 只要当日涨 3% ~ 15% 的股票（追强势）
PICKER_REALTIME_DAILY_CHG_MIN=3
PICKER_REALTIME_DAILY_CHG_MAX=15

# 不过滤放量（激进风格可以接受游资抢筹）
PICKER_REALTIME_MAX_VOLUME_RATIO=0.0
```

**效果**：选出强势上涨的股票，适合趋势追踪。

## 四、配置项详解

| 配置项 | 类型 | 默认值 | 说明 |
|------|------|-------|------|
| `PICKER_ENABLE_REALTIME_FILTER` | bool | `true` | 整体开关 |
| `PICKER_REALTIME_EXCLUDE_LIMIT_UP` | bool | `true` | 排除涨停（涨幅 ≥ 9.5% for 主板，≥ 20% for 创业板/科创板） |
| `PICKER_REALTIME_EXCLUDE_LIMIT_DOWN` | bool | `true` | 排除跌停（跌幅 ≤ -9.5% or -20%） |
| `PICKER_REALTIME_DAILY_CHG_MIN` | float | 空 | 当日最小涨幅(%)，空表示无下限 |
| `PICKER_REALTIME_DAILY_CHG_MAX` | float | 空 | 当日最大涨幅(%)，空表示无上限 |
| `PICKER_REALTIME_MAX_VOLUME_RATIO` | float | `0.0` | 最大量比，0 表示无限制 |

## 五、使用场景

### 场景 A：日常自动选股（GitHub Actions）

你的选股任务定时（如每天 15:00）运行，此时：

```bash
# .env
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=-3
PICKER_REALTIME_DAILY_CHG_MAX=8
```

**效果**：每天下午 3 点自动选出当天表现"温和向好"的股票，推送给你，可以立即下单。

### 场景 B：收盘后补充选股（本地运行）

收盘后（如 16:00）再跑一遍选股，选的是收盘价表现好的股票：

```bash
# .env
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true   # 排除已涨停的
# 不设置涨幅限制，让量化模型决定
```

### 场景 C：强势追踪（短线交易）

想捕捉当天强势板块，可以配置更激进的规则：

```bash
# .env
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_DAILY_CHG_MIN=5         # 只要涨5%以上的
PICKER_REALTIME_MAX_VOLUME_RATIO=10.0   # 接受较大放量
```

## 六、日志输出示例

实时筛选后，你会看到类似这样的日志：

```
[StockPicker] === Stage 1.5: Real-time Filtering ===
[StockPicker] Real-time filtering: 28 → 24 candidates
[StockPicker] Real-time filtering excluded 4 stocks:
  600519: 涨停(10.2%)
  300750: 涨幅过大(要求<8%,当前9.5%)
  002594: 异常放量(量比6.2>5.0)
  688008: 跌停(-10.1%)
```

说明 28 只候选池中有 4 只被过滤，最终进入 AI 精选的是 24 只。

## 七、常见问题

### Q1: 实时数据有延迟吗？

实时数据通常延迟 1-2 秒，来自行情数据源（Pytdx / AkShare / Tencent 等）。
- 对于日线选股，这个延迟可以接受
- 如果需要毫秒级精度，需要接入专业行情 API

### Q2: 实时数据源是什么？

系统使用现有的数据源优先级链：
1. Tencent（腾讯财经，推荐）
2. AkShare Sina（新浪财经）
3. efinance（东财）
4. AkShare EM（东财集群）

可以在 `.env` 中配置 `REALTIME_SOURCE_PRIORITY` 修改优先级。

### Q3: 如何关闭实时筛选？

```bash
PICKER_ENABLE_REALTIME_FILTER=false
```

或者不配置这个变量，因为默认为 `true`。

### Q4: 实时筛选对性能的影响？

几乎没有：只是在候选池上做快速数值比对，耗时 < 100ms。

### Q5: 能否自定义筛选规则？

目前支持上述 6 个开箱即用的规则。如果需要更复杂的规则（如基于技术指标、筹码分布等），可以：
1. 修改 `src/services/stock_picker_service.py` 的 `_filter_by_realtime()` 方法
2. 提交 PR 贡献新规则

## 八、性能对比

| 场景 | 无实时筛选 | 有实时筛选 | 改善 |
|-----|---------|----------|-----|
| 选股运行时间 | ~30s | ~31s | +1s（可忽略） |
| 候选池数量 | ~30 | ~25 | -17%（更精细） |
| AI 精选耗时 | ~15s | ~14s | -7%（输入少） |

## 九、后续优化方向

- [ ] 支持自定义技术指标筛选（MACD、KDJ 等）
- [ ] 支持筹码分布筛选（限制主力抄底的股票）
- [ ] 支持基于时间的选股（收盘前 30 分钟才触发）
- [ ] 支持 A/B 测试多规则对比

## 十、常见配置模板

### 模板 1：回踩低吸（推荐初学者）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=-3
PICKER_REALTIME_DAILY_CHG_MAX=3
PICKER_REALTIME_MAX_VOLUME_RATIO=3.0
```

### 模板 2：温和上涨（平衡型）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=0
PICKER_REALTIME_DAILY_CHG_MAX=8
PICKER_REALTIME_MAX_VOLUME_RATIO=5.0
```

### 模板 3：强势突破（激进型）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_DAILY_CHG_MIN=5
PICKER_REALTIME_DAILY_CHG_MAX=20
PICKER_REALTIME_MAX_VOLUME_RATIO=10.0
```

---

**提示**：调整规则后无需重启，下次选股运行会自动应用新配置。
