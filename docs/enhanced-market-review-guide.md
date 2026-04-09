# 增强版A股复盘系统使用指南

## 📊 概述

增强版A股复盘系统是在原有复盘功能基础上开发的专业分析工具，特别适合公众号发布。相比原版本，新增了以下核心功能：

### 🆕 新增功能

1. **深度情绪分析**
   - 恐慌贪婪指数计算（0-100）
   - 市场热度评估
   - 资金流向趋势分析
   - 量比和换手率分析

2. **板块热点深度解读**
   - 概念板块轮动分析
   - 热点持续性评估
   - 催化剂识别
   - 龙头股票追踪

3. **外界环境分析**
   - 政策面影响评估
   - 国际市场联动分析
   - 宏观经济数据解读
   - 汇率和大宗商品走势

4. **技术面专业分析**
   - 关键技术位识别
   - 量价关系分析
   - 市场结构评估
   - 趋势方向判断

5. **公众号格式优化**
   - 微信公众号排版优化
   - 小红书格式适配
   - 自动生成标题建议
   - 内容摘要提取

## 🚀 快速开始

### 1. 配置启用

在 `.env` 文件中添加以下配置：

```bash
# 启用增强版复盘分析
USE_ENHANCED_MARKET_REVIEW=true

# 自动生成公众号格式
GENERATE_WECHAT_FORMAT=true

# 公众号配置
WECHAT_ACCOUNT_NAME=你的公众号名称
WECHAT_SLOGAN=你的公众号标语
WECHAT_QR_CODE_TEXT=关注提示文字
```

### 2. 运行增强版复盘

```bash
# 仅运行增强版大盘复盘
python main.py --market-review

# 运行完整分析（包含个股+增强版复盘）
python main.py

# 强制运行（跳过交易日检查）
python main.py --market-review --force-run
```

### 3. 查看生成的报告

运行后会在 `logs/` 目录下生成以下文件：

- `market_review_enhanced_YYYYMMDD.md` - 增强版原始报告
- `market_review_wechat_YYYYMMDD.md` - 公众号格式报告

## 📋 报告结构

### 增强版报告包含以下章节：

```markdown
## 📊 YYYY-MM-DD A股智能复盘

### 🎯 一、市场概况
- 整体市场表现
- 主要指数涨跌
- 成交量变化

### 📈 二、情绪解读
- 恐慌贪婪指数分析
- 市场热度评估
- 投资者情绪状态

### 🔥 三、热点聚焦
- 领涨板块深度解读
- 催化剂分析
- 持续性评估

### 🌍 四、外围影响
- 政策面分析
- 国际市场表现
- 宏观环境影响

### 📊 五、技术研判
- 趋势方向判断
- 关键技术位
- 量价关系分析

### 💡 六、策略建议
- 操作建议
- 仓位配置
- 重点关注方向

### ⚠️ 七、风险提示
- 主要风险点
- 应对策略
```

## 🎨 公众号格式特色

### 1. 视觉优化
- 丰富的emoji装饰
- 清晰的章节分割
- 突出的数据展示
- 专业的排版布局

### 2. 互动元素
- 读者投票区域
- 评论引导
- 关注提示
- 转发鼓励

### 3. 合规要素
- 完整的免责声明
- 数据来源说明
- 发布时间标注
- 风险提示

### 4. 示例效果

```markdown
# 📊 2026年04月03日 A股智能复盘

> 🤖 **AI驱动的专业分析** | 📈 **数据说话，理性投资**
> 
> AI驱动的股市复盘，让投资更智能

---

### 🎯 **一、市场概况**

今日A股市场整体呈现恐慌态势，市场情绪相对谨慎，投资者观望情绪浓厚。

> 📊 **【市场数据速览】**
> 
> 📈 涨跌: **1002** ↑ / **4441** ↓ / **44** → | 涨停: **18** / 跌停: **18**
> 
> 💰 成交额: **8598** 亿 | 情绪指数: **25**/100 (恐慌)

### 📈 **二、情绪解读**

> 🎭 **【情绪温度计】**
> 
> 恐慌贪婪指数: **25**/100 | 市场热度: **50**/100
> 
> 资金流向: **资金谨慎观望** | 量比: **1.0**

市场恐慌贪婪指数为25，处于恐慌区域，市场情绪偏向恐慌，谨慎观望为主。

---

### 🔥 **三、热点聚焦**

> 🔥 **【今日热点追踪】**
> 
> 1. 🚀 **地面兵装Ⅱ** +7.86% - 强势突破，关注持续性
>    💡 国防安全 | 装备升级 | 自主可控
> 2. 🚀 **地面兵装Ⅲ** +7.86% - 强势突破，关注持续性
> 3. 🚀 **大气治理** +7.44% - 强势突破，关注持续性

军工板块表现强势，地面兵装相关概念领涨...
```

## ⚙️ 配置选项详解

### 基础配置

```bash
# 是否启用增强版复盘（默认: true）
USE_ENHANCED_MARKET_REVIEW=true

# 是否生成公众号格式（默认: true）
GENERATE_WECHAT_FORMAT=true
```

### 公众号配置

```bash
# 公众号名称（默认: A股智能分析）
WECHAT_ACCOUNT_NAME=你的公众号名称

# 公众号标语（默认: AI驱动的股市复盘，让投资更智能）
WECHAT_SLOGAN=你的公众号标语

# 二维码提示文字（默认: 扫码关注获取每日复盘）
WECHAT_QR_CODE_TEXT=扫码关注获取每日复盘
```

### 格式配置

```bash
# 是否使用emoji（默认: true）
WECHAT_USE_EMOJI=true

# 是否使用分割线（默认: true）
WECHAT_USE_DIVIDERS=true

# 是否添加页脚（默认: true）
WECHAT_ADD_FOOTER=true

# 最大字符长度（默认: 8000）
WECHAT_MAX_LENGTH=8000
```

## 🔧 API集成

### 在代码中使用增强版分析器

```python
from src.enhanced_market_analyzer import EnhancedMarketAnalyzer
from src.wechat_formatter import WechatFormatter, PublishPlatform

# 创建增强版分析器
analyzer = EnhancedMarketAnalyzer(region="cn")

# 运行完整分析
report = analyzer.run_enhanced_daily_review()

# 格式化为公众号格式
formatter = WechatFormatter()
wechat_report = formatter.format_market_review(
    report, 
    PublishPlatform.WECHAT
)

# 生成标题建议
titles = formatter.create_title_suggestions(report)

# 生成摘要
summary = formatter.generate_summary(report)
```

### 自定义格式化配置

```python
from src.wechat_formatter import WechatConfig, WechatFormatter

# 自定义配置
config = WechatConfig(
    account_name="我的投资笔记",
    slogan="专业投资分析，理性决策",
    use_emoji=True,
    use_dividers=True,
    max_length=6000
)

# 使用自定义配置
formatter = WechatFormatter(config)
```

## 📊 数据源说明

增强版系统使用以下数据源：

1. **基础行情数据**: Tushare Pro / EFinance / AkShare
2. **板块数据**: 申万行业分类
3. **市场统计**: 实时涨跌家数、涨停跌停统计
4. **新闻数据**: 配置的搜索服务（可选）
5. **国际市场**: Yahoo Finance 等（模拟数据）

## 🚨 注意事项

### 1. API限制
- Tushare Pro 有调用频率限制
- 建议配置多个数据源作为备选
- 避免在短时间内频繁调用

### 2. 内容合规
- 自动添加免责声明
- 不构成投资建议提示
- 风险提示内容

### 3. 性能优化
- 数据获取可能需要较长时间
- 建议在非交易时间运行
- 可配置超时时间

## 🆚 版本对比

| 功能 | 标准版 | 增强版 |
| ---- | ---- | ---- |
| 基础指数数据 | ✅ | ✅ |
| 涨跌统计 | ✅ | ✅ |
| 板块排行 | ✅ | ✅ |
| 市场新闻 | ✅ | ✅ |
| 情绪分析 | ❌ | ✅ |
| 热点深度解读 | ❌ | ✅ |
| 外界环境分析 | ❌ | ✅ |
| 技术面分析 | ❌ | ✅ |
| 公众号格式 | ❌ | ✅ |
| 标题建议 | ❌ | ✅ |
| 多平台适配 | ❌ | ✅ |

## 🔮 未来规划

1. **更多平台支持**: 知乎、微博等平台格式
2. **图表生成**: 自动生成情绪指数图表
3. **个性化配置**: 更多自定义选项
4. **AI优化**: 更智能的内容生成
5. **数据增强**: 更多维度的市场数据

## 📞 技术支持

如遇到问题，请：

1. 检查 `.env` 配置是否正确
2. 查看日志文件了解详细错误
3. 确认API密钥配置有效
4. 参考测试脚本 `test_enhanced_review.py`

---

**免责声明**: 本系统生成的分析报告仅供学习研究，不构成任何投资建议。股市有风险，投资需谨慎。