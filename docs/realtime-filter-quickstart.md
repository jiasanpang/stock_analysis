# 实时筛选 - 快速参考卡片

## 一行启用（推荐）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
```

## 三个推荐模板

### 模板 1: 低吸回踩（保守）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=-3
PICKER_REALTIME_DAILY_CHG_MAX=3
PICKER_REALTIME_MAX_VOLUME_RATIO=3.0
```
**用途**：选出温和回踩、量能萎缩的低吸机会

### 模板 2: 温和上涨（平衡，推荐）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=true
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=0
PICKER_REALTIME_DAILY_CHG_MAX=8
PICKER_REALTIME_MAX_VOLUME_RATIO=5.0
```
**用途**：选出温和上涨、量能适中的股票

### 模板 3: 强势突破（激进）

```bash
PICKER_ENABLE_REALTIME_FILTER=true
PICKER_REALTIME_EXCLUDE_LIMIT_UP=false
PICKER_REALTIME_EXCLUDE_LIMIT_DOWN=true
PICKER_REALTIME_DAILY_CHG_MIN=5
PICKER_REALTIME_DAILY_CHG_MAX=20
PICKER_REALTIME_MAX_VOLUME_RATIO=10.0
```
**用途**：选出强势突破、大幅拉升的趋势股

## 工作流程

```
第1步：更新 .env（选择上述模板或自定义）
      ↓
第2步：运行选股
      python main.py --picker-only
      ↓
第3步：查看日志中的 Stage 1.5 输出
      看是否有股票被过滤掉及原因
      ↓
第4步：根据需要调整参数，再试
```

## 配置详解

| 配置项 | 作用 | 常用值 |
| ---- | ---- | ---- |
| `PICKER_REALTIME_EXCLUDE_LIMIT_UP` | 排除涨停 | `true` ✅ |
| `PICKER_REALTIME_EXCLUDE_LIMIT_DOWN` | 排除跌停 | `true` ✅ |
| `PICKER_REALTIME_DAILY_CHG_MIN` | 最小涨幅 | `-3` / `-2` / `0` / 空 |
| `PICKER_REALTIME_DAILY_CHG_MAX` | 最大涨幅 | `3` / `5` / `8` / `15` / 空 |
| `PICKER_REALTIME_MAX_VOLUME_RATIO` | 最大量比 | `0` / `3` / `5` / `10` |

## 日志解读

```
[StockPicker] === Stage 1.5: Real-time Filtering ===
[StockPicker] Real-time filtering: 28 → 24 candidates
  ↑                                    ↑   ↑
  处理中                     过滤前  过滤后
  
[StockPicker] Real-time filtering excluded 4 stocks:
  600519: 涨停(10.2%)
  300750: 涨幅过大(要求<8%,当前9.5%)
  002594: 异常放量(量比6.2>5.0)
  688008: 跌停(-10.1%)
```

**解读**：28 只候选中，4 只因各种原因被排除，最终 24 只进入 AI 精选。

## 常见调整

**问题**：选出的股票太多，质量不够  
**解决**：
- 降低 `PICKER_REALTIME_DAILY_CHG_MAX`（如 8 → 5）
- 提高 `PICKER_REALTIME_MAX_VOLUME_RATIO`（如 5 → 3）

**问题**：选不出来  
**解决**：
- 放宽 `PICKER_REALTIME_DAILY_CHG_MIN`（如 -2 → -5）
- 降低 `PICKER_REALTIME_MAX_VOLUME_RATIO`（如 5 → 10）

**问题**：想捕捉涨停前的机会  
**解决**：
```bash
PICKER_REALTIME_EXCLUDE_LIMIT_UP=false
PICKER_REALTIME_DAILY_CHG_MIN=7
```

## 无缝集成

- ✅ 无需改代码
- ✅ 无需重启
- ✅ 只需改 `.env` 后重新运行选股即可
- ✅ 所有现有功能保持不变（如 AI 精选、推送、回测等）

---

**更多详情**：[实时行情筛选指南](docs/realtime-picker-guide.md)
