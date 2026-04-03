# -*- coding: utf-8 -*-
"""
===================================
公众号格式化模块
===================================

职责：
1. 将复盘报告格式化为适合公众号发布的格式
2. 添加公众号特有的元素（引导关注、免责声明等）
3. 优化排版和视觉效果
4. 支持多种发布平台格式（微信公众号、小红书等）
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class PublishPlatform(Enum):
    """发布平台枚举"""
    WECHAT = "wechat"           # 微信公众号
    XIAOHONGSHU = "xiaohongshu" # 小红书
    WEIBO = "weibo"             # 微博
    ZHIHU = "zhihu"             # 知乎


@dataclass
class WechatConfig:
    """公众号配置"""
    account_name: str = "A股智能分析"
    slogan: str = "AI驱动的股市复盘，让投资更智能"
    qr_code_text: str = "扫码关注获取每日复盘"
    
    # 样式配置
    use_emoji: bool = True
    use_dividers: bool = True
    add_footer: bool = True
    add_disclaimer: bool = True
    
    # 内容配置
    max_length: int = 8000      # 最大字符数
    include_data_tables: bool = True
    include_charts_placeholder: bool = True


class WechatFormatter:
    """
    公众号格式化器
    
    功能：
    1. 格式化复盘报告为公众号格式
    2. 添加引导关注和互动元素
    3. 优化排版和视觉效果
    4. 确保合规性
    """
    
    def __init__(self, config: Optional[WechatConfig] = None):
        self.config = config or WechatConfig()
    
    def format_market_review(
        self, 
        report: str, 
        platform: PublishPlatform = PublishPlatform.WECHAT,
        add_interactive_elements: bool = True
    ) -> str:
        """
        格式化市场复盘报告
        
        Args:
            report: 原始复盘报告
            platform: 发布平台
            add_interactive_elements: 是否添加互动元素
            
        Returns:
            str: 格式化后的报告
        """
        if platform == PublishPlatform.WECHAT:
            return self._format_for_wechat(report, add_interactive_elements)
        elif platform == PublishPlatform.XIAOHONGSHU:
            return self._format_for_xiaohongshu(report)
        else:
            return self._format_for_general(report)
    
    def _format_for_wechat(self, report: str, add_interactive: bool = True) -> str:
        """格式化为微信公众号格式"""
        
        # 1. 添加标题和引言
        formatted_report = self._add_wechat_header(report)
        
        # 2. 优化正文格式
        formatted_report = self._optimize_wechat_content(formatted_report)
        
        # 3. 添加数据可视化占位符
        if self.config.include_charts_placeholder:
            formatted_report = self._add_chart_placeholders(formatted_report)
        
        # 4. 添加互动元素
        if add_interactive:
            formatted_report = self._add_interactive_elements(formatted_report)
        
        # 5. 添加页脚
        if self.config.add_footer:
            formatted_report = self._add_wechat_footer(formatted_report)
        
        # 6. 长度控制
        formatted_report = self._control_length(formatted_report)
        
        return formatted_report
    
    def _add_wechat_header(self, report: str) -> str:
        """添加公众号标题和引言"""
        today = datetime.now().strftime('%Y年%m月%d日')
        
        # 提取原报告的日期标题
        import re
        title_match = re.search(r'##\s*📊\s*(\d{4}-\d{2}-\d{2})\s*A股智能复盘', report)
        if title_match:
            date_str = title_match.group(1)
            # 转换日期格式
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                today = date_obj.strftime('%Y年%m月%d日')
            except:
                pass
        
        header = f"""# 📊 {today} A股智能复盘

> 🤖 **AI驱动的专业分析** | 📈 **数据说话，理性投资**
> 
> {self.config.slogan}

---

"""
        
        # 移除原标题，避免重复
        report = re.sub(r'##\s*📊.*?A股智能复盘\n*', '', report)
        
        return header + report
    
    def _optimize_wechat_content(self, content: str) -> str:
        """优化公众号内容格式"""
        
        # 1. 优化标题层级
        content = self._optimize_headings(content)
        
        # 2. 优化引用块
        content = self._optimize_quotes(content)
        
        # 3. 优化列表格式
        content = self._optimize_lists(content)
        
        # 4. 添加分割线
        if self.config.use_dividers:
            content = self._add_section_dividers(content)
        
        # 5. 优化表格
        content = self._optimize_tables(content)
        
        return content
    
    def _optimize_headings(self, content: str) -> str:
        """优化标题格式"""
        import re
        
        # 三级标题添加更多装饰
        content = re.sub(
            r'###\s*(🎯|📈|🔥|🌍|📊|💡|⚠️)\s*([一二三四五六七八九十]+、.*?)(?=\n)',
            r'### \1 **\2**',
            content
        )
        
        # 为主要章节添加背景色效果（使用引用块模拟）
        section_patterns = [
            (r'(### 🎯 \*\*一、.*?\*\*)', r'\n> \1\n'),
            (r'(### 📈 \*\*二、.*?\*\*)', r'\n> \1\n'),
            (r'(### 🔥 \*\*三、.*?\*\*)', r'\n> \1\n'),
        ]
        
        for pattern, replacement in section_patterns:
            content = re.sub(pattern, replacement, content)
        
        return content
    
    def _optimize_quotes(self, content: str) -> str:
        """优化引用块格式"""
        import re
        
        # 将数据速览块转换为更醒目的格式
        content = re.sub(
            r'>\s*📊\s*\*\*市场数据速览\*\*',
            '> 📊 **【市场数据速览】**',
            content
        )
        
        # 优化情绪指标块
        content = re.sub(
            r'>\s*🎭\s*\*\*情绪指标\*\*',
            '> 🎭 **【情绪温度计】**',
            content
        )
        
        # 优化热点板块块
        content = re.sub(
            r'>\s*🔥\s*\*\*热点板块\*\*',
            '> 🔥 **【今日热点追踪】**',
            content
        )
        
        return content
    
    def _optimize_lists(self, content: str) -> str:
        """优化列表格式"""
        import re
        
        # 为重要的列表项添加强调
        content = re.sub(
            r'^-\s*\*\*(.*?)\*\*:\s*(.*?)$',
            r'📌 **\1**: \2',
            content,
            flags=re.MULTILINE
        )
        
        return content
    
    def _add_section_dividers(self, content: str) -> str:
        """添加章节分割线"""
        import re
        
        # 在主要章节之间添加分割线
        content = re.sub(
            r'(### 🎯.*?\n.*?)(\n### 📈)',
            r'\1\n\n---\n\2',
            content,
            flags=re.DOTALL
        )
        
        content = re.sub(
            r'(### 📈.*?\n.*?)(\n### 🔥)',
            r'\1\n\n---\n\2',
            content,
            flags=re.DOTALL
        )
        
        content = re.sub(
            r'(### 🔥.*?\n.*?)(\n### 🌍)',
            r'\1\n\n---\n\2',
            content,
            flags=re.DOTALL
        )
        
        return content
    
    def _optimize_tables(self, content: str) -> str:
        """优化表格格式"""
        import re
        
        # 为表格添加说明文字
        content = re.sub(
            r'(\| 指数 \| 最新 \| 涨跌幅 \| 成交额\(亿\) \|)',
            r'**📊 主要指数表现**\n\n\1',
            content
        )
        
        return content
    
    def _add_chart_placeholders(self, content: str) -> str:
        """添加图表占位符"""
        
        # 在情绪解读后添加情绪指数图表占位符
        import re
        
        chart_placeholder = """
> 📈 **【情绪指数走势图】**
> 
> *（此处插入恐慌贪婪指数走势图）*

"""
        
        content = re.sub(
            r'(### 📈.*?情绪解读.*?\n.*?)(\n---\n|\n### 🔥)',
            r'\1\n' + chart_placeholder + r'\2',
            content,
            flags=re.DOTALL
        )
        
        return content
    
    def _add_interactive_elements(self, content: str) -> str:
        """添加互动元素"""
        
        # 在策略建议后添加互动提示
        interactive_section = """

---

## 💬 互动时间

**💭 你觉得明日市场会如何走？**

A. 继续上涨 📈  
B. 震荡整理 🔄  
C. 调整回落 📉

**在评论区留下你的观点，我们一起讨论！**

**📱 想要获取更多实时分析？**
- 点击"在看"支持我们
- 转发给需要的朋友  
- 留言互动获得回复

"""
        
        # 在风险提示前插入互动元素
        import re
        content = re.sub(
            r'(\n### ⚠️.*?风险提示)',
            interactive_section + r'\1',
            content
        )
        
        return content
    
    def _add_wechat_footer(self, content: str) -> str:
        """添加公众号页脚"""
        
        footer = f"""

---

## 📢 关于我们

**{self.config.account_name}** - {self.config.slogan}

🔹 **每日复盘**: 专业AI分析，数据驱动决策  
🔹 **实时提醒**: 重要市场变化及时通知  
🔹 **策略分享**: 量化选股，理性投资  

**📱 {self.config.qr_code_text}**

*（此处插入公众号二维码）*

---

## ⚠️ 免责声明

本文内容仅供学习研究，不构成任何投资建议。股市有风险，投资需谨慎。投资者应当根据自身情况独立做出投资决策，并承担相应风险。

**📊 数据来源**: 公开市场数据  
**🤖 分析工具**: AI智能分析系统  
**📅 发布时间**: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}

---

*如果觉得有用，请点击"在看"并转发支持！*

"""
        
        return content + footer
    
    def _control_length(self, content: str) -> str:
        """控制内容长度"""
        if len(content) <= self.config.max_length:
            return content
        
        logger.warning(f"内容长度 {len(content)} 超过限制 {self.config.max_length}，进行裁剪")
        
        # 优先保留核心章节，裁剪次要内容
        import re
        
        # 如果太长，移除一些装饰性内容
        content = re.sub(r'\n---\n', '\n', content)  # 移除分割线
        content = re.sub(r'\*（此处插入.*?）\*\n?', '', content)  # 移除图表占位符
        
        # 如果还是太长，截断到指定长度
        if len(content) > self.config.max_length:
            content = content[:self.config.max_length - 100] + "\n\n...\n\n*内容过长，完整版请查看原文*"
        
        return content
    
    def _format_for_xiaohongshu(self, report: str) -> str:
        """格式化为小红书格式"""
        
        # 小红书特点：更多emoji，更短的段落，更口语化
        import re
        
        # 简化标题
        report = re.sub(r'##\s*📊.*?A股智能复盘', '📊今日A股复盘来啦！', report)
        
        # 添加小红书风格的开头
        xiaohongshu_header = """📊今日A股复盘来啦！

姐妹们！今天的股市表现如何？
让AI来给大家分析分析～

#A股复盘 #股市分析 #投资理财 #AI分析

---

"""
        
        # 简化内容，使用更多emoji
        report = re.sub(r'### 🎯 \*\*一、市场概况\*\*', '🎯 市场表现', report)
        report = re.sub(r'### 📈 \*\*二、情绪解读\*\*', '📈 市场情绪', report)
        report = re.sub(r'### 🔥 \*\*三、热点聚焦\*\*', '🔥 今日热点', report)
        
        # 添加小红书风格结尾
        xiaohongshu_footer = """

---

💕 觉得有用记得点赞收藏哦～
📝 有问题欢迎评论区讨论！
🔔 关注我，每日复盘不错过！

#理财小白 #股票入门 #投资笔记

⚠️ 投资有风险，仅供参考学习～

"""
        
        return xiaohongshu_header + report + xiaohongshu_footer
    
    def _format_for_general(self, report: str) -> str:
        """通用格式化"""
        return report
    
    def create_title_suggestions(self, report: str) -> List[str]:
        """
        根据报告内容生成标题建议
        
        Args:
            report: 复盘报告内容
            
        Returns:
            List[str]: 标题建议列表
        """
        
        # 分析报告内容，提取关键信息
        import re
        
        # 提取日期
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', report)
        date_str = date_match.group(1) if date_match else datetime.now().strftime('%Y-%m-%d')
        
        # 提取市场表现关键词
        market_keywords = []
        if "上涨" in report:
            market_keywords.append("上涨")
        if "下跌" in report:
            market_keywords.append("下跌")
        if "震荡" in report:
            market_keywords.append("震荡")
        if "放量" in report:
            market_keywords.append("放量")
        if "缩量" in report:
            market_keywords.append("缩量")
        
        # 提取热点板块
        sector_matches = re.findall(r'(\w+板块|\w+概念)', report)
        hot_sectors = list(set(sector_matches))[:3]
        
        # 生成标题建议
        titles = [
            f"📊 {date_str} A股复盘：AI深度解析市场走势",
            f"🎯 今日复盘 | {' '.join(market_keywords[:2])}行情全解析",
            f"📈 A股智能复盘：{date_str} 市场情绪与热点追踪",
        ]
        
        if hot_sectors:
            titles.append(f"🔥 {date_str} 复盘：{hot_sectors[0]}领涨，后市如何？")
            
        if "恐慌" in report:
            titles.append("⚠️ 市场恐慌情绪升温，现在该如何操作？")
        elif "贪婪" in report:
            titles.append("🚨 市场贪婪情绪高涨，注意风险控制！")
            
        return titles[:5]
    
    def generate_summary(self, report: str, max_length: int = 200) -> str:
        """
        生成报告摘要
        
        Args:
            report: 完整报告
            max_length: 最大长度
            
        Returns:
            str: 报告摘要
        """
        
        # 提取关键信息
        import re
        
        # 提取市场概况
        market_summary = ""
        market_match = re.search(r'### 🎯.*?市场概况.*?\n(.*?)(?=\n###|\n---|\Z)', report, re.DOTALL)
        if market_match:
            market_summary = market_match.group(1).strip()[:100]
        
        # 提取策略建议
        strategy_summary = ""
        strategy_match = re.search(r'### 💡.*?策略建议.*?\n(.*?)(?=\n###|\n---|\Z)', report, re.DOTALL)
        if strategy_match:
            strategy_summary = strategy_match.group(1).strip()[:100]
        
        # 组合摘要
        summary_parts = []
        if market_summary:
            summary_parts.append(market_summary)
        if strategy_summary:
            summary_parts.append(strategy_summary)
        
        summary = "。".join(summary_parts)
        
        # 长度控制
        if len(summary) > max_length:
            summary = summary[:max_length-3] + "..."
        
        return summary or "AI智能分析今日A股市场表现，提供专业投资建议。"


# 使用示例和测试
if __name__ == "__main__":
    
    # 测试报告
    test_report = """## 📊 2024-04-02 A股智能复盘

### 🎯 一、市场概况
今日A股市场整体呈现震荡下跌态势，市场情绪相对谨慎。

### 📈 二、情绪解读
恐慌贪婪指数45，处于中性偏恐慌区域。

### 🔥 三、热点聚焦
新能源板块表现活跃，AI概念股受到关注。

### 💡 六、策略建议
建议控制仓位，关注优质成长股回调机会。

### ⚠️ 七、风险提示
市场有风险，投资需谨慎。
"""
    
    # 创建格式化器
    formatter = WechatFormatter()
    
    # 格式化为公众号格式
    wechat_report = formatter.format_market_review(test_report, PublishPlatform.WECHAT)
    print("=== 微信公众号格式 ===")
    print(wechat_report)
    
    # 生成标题建议
    titles = formatter.create_title_suggestions(test_report)
    print("\n=== 标题建议 ===")
    for i, title in enumerate(titles, 1):
        print(f"{i}. {title}")
    
    # 生成摘要
    summary = formatter.generate_summary(test_report)
    print(f"\n=== 摘要 ===")
    print(summary)