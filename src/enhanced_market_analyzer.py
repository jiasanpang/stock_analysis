# -*- coding: utf-8 -*-
"""
===================================
增强版大盘复盘分析模块 - 适合公众号发布
===================================

职责：
1. 深度市场情绪分析（恐慌贪婪指数、资金流向）
2. 板块热点深度解读（概念轮动、资金流入、持续性）
3. 外界环境分析（政策面、国际市场、宏观数据）
4. 技术面专业分析（关键位、量价关系、市场结构）
5. 生成适合公众号发布的专业复盘报告
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum

import pandas as pd

from src.config import get_config
from src.search_service import SearchService
from src.market_analyzer import MarketAnalyzer, MarketOverview, MarketIndex
from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)


class MarketSentiment(Enum):
    """市场情绪枚举"""
    EXTREME_FEAR = "极度恐慌"
    FEAR = "恐慌"
    NEUTRAL = "中性"
    GREED = "贪婪"
    EXTREME_GREED = "极度贪婪"


@dataclass
class SentimentAnalysis:
    """市场情绪分析数据"""
    sentiment: MarketSentiment = MarketSentiment.NEUTRAL
    fear_greed_index: float = 50.0  # 0-100，50为中性
    market_heat: float = 0.0        # 市场热度 0-100
    fund_flow_trend: str = "平衡"    # 资金流向趋势
    volatility_level: str = "正常"   # 波动率水平
    
    # 细分指标
    volume_ratio: float = 1.0       # 量比（今日/5日均量）
    turnover_rate: float = 0.0      # 换手率
    new_high_low_ratio: float = 0.0 # 新高新低比
    
    def get_sentiment_description(self) -> str:
        """获取情绪描述"""
        descriptions = {
            MarketSentiment.EXTREME_FEAR: "市场极度恐慌，抄底机会可能出现",
            MarketSentiment.FEAR: "市场情绪偏向恐慌，谨慎观望为主",
            MarketSentiment.NEUTRAL: "市场情绪相对平稳，观察方向选择",
            MarketSentiment.GREED: "市场情绪偏向乐观，注意风险控制",
            MarketSentiment.EXTREME_GREED: "市场极度乐观，警惕回调风险"
        }
        return descriptions.get(self.sentiment, "情绪中性")


@dataclass
class SectorHotspot:
    """板块热点分析"""
    name: str                       # 板块名称
    change_pct: float              # 涨跌幅
    fund_inflow: float = 0.0       # 资金流入（亿元）
    leading_stocks: List[str] = field(default_factory=list)  # 龙头股票
    concept_tags: List[str] = field(default_factory=list)    # 概念标签
    sustainability: str = "待观察"   # 持续性评估
    catalyst: str = ""             # 催化剂
    risk_warning: str = ""         # 风险提示


@dataclass
class ExternalEnvironment:
    """外界环境分析"""
    policy_impact: str = ""        # 政策面影响
    international_market: str = "" # 国际市场表现
    macro_data: str = ""          # 宏观经济数据
    currency_trend: str = ""      # 汇率走势
    commodity_trend: str = ""     # 大宗商品
    
    # 具体数据
    us_futures: Dict[str, float] = field(default_factory=dict)  # 美股期货
    asia_markets: Dict[str, float] = field(default_factory=dict) # 亚太市场
    vix_index: float = 0.0        # 恐慌指数


@dataclass
class TechnicalAnalysis:
    """技术面分析"""
    key_support: float = 0.0      # 关键支撑位
    key_resistance: float = 0.0   # 关键阻力位
    trend_direction: str = "震荡" # 趋势方向
    volume_price_relation: str = "" # 量价关系
    market_structure: str = ""    # 市场结构
    
    # 技术指标
    ma_alignment: str = ""        # 均线排列
    macd_signal: str = ""        # MACD信号
    rsi_level: float = 50.0      # RSI水平
    
    def get_trend_emoji(self) -> str:
        """获取趋势emoji"""
        trend_map = {
            "强势上涨": "🚀",
            "温和上涨": "📈", 
            "震荡整理": "🔄",
            "温和下跌": "📉",
            "快速下跌": "💥"
        }
        return trend_map.get(self.trend_direction, "🔄")


@dataclass
class EnhancedMarketReport:
    """增强版市场报告"""
    date: str
    basic_overview: MarketOverview
    sentiment_analysis: SentimentAnalysis
    sector_hotspots: List[SectorHotspot] = field(default_factory=list)
    external_environment: ExternalEnvironment = field(default_factory=ExternalEnvironment)
    technical_analysis: TechnicalAnalysis = field(default_factory=TechnicalAnalysis)
    market_news: List[Dict] = field(default_factory=list)
    
    # 策略建议
    strategy_advice: str = ""
    position_suggestion: str = ""
    risk_level: str = "中等"
    
    def get_overall_rating(self) -> Tuple[str, str]:
        """获取整体评级和建议"""
        # 基于多个维度计算综合评级
        sentiment_score = self.sentiment_analysis.fear_greed_index
        
        if sentiment_score >= 80:
            return "高风险", "建议减仓观望，警惕回调"
        elif sentiment_score >= 60:
            return "中高风险", "适度参与，控制仓位"
        elif sentiment_score >= 40:
            return "中性", "均衡配置，观察方向"
        elif sentiment_score >= 20:
            return "中低风险", "可适度加仓，精选个股"
        else:
            return "低风险", "积极布局，关注反弹机会"


class EnhancedMarketAnalyzer(MarketAnalyzer):
    """
    增强版大盘分析器
    
    在原有功能基础上增加：
    1. 深度情绪分析
    2. 板块热点解读
    3. 外界环境分析
    4. 技术面分析
    5. 公众号格式输出
    """
    
    def __init__(
        self,
        search_service: Optional[SearchService] = None,
        analyzer=None,
        region: str = "cn",
    ):
        super().__init__(search_service, analyzer, region)
        self.enhanced_data_manager = DataFetcherManager()
    
    def analyze_market_sentiment(self, overview: MarketOverview) -> SentimentAnalysis:
        """
        分析市场情绪
        
        Args:
            overview: 基础市场概览
            
        Returns:
            SentimentAnalysis: 情绪分析结果
        """
        sentiment = SentimentAnalysis()
        
        try:
            # 计算恐慌贪婪指数（基于多个指标）
            fear_greed_score = self._calculate_fear_greed_index(overview)
            sentiment.fear_greed_index = fear_greed_score
            
            # 确定情绪等级
            if fear_greed_score >= 80:
                sentiment.sentiment = MarketSentiment.EXTREME_GREED
            elif fear_greed_score >= 60:
                sentiment.sentiment = MarketSentiment.GREED
            elif fear_greed_score >= 40:
                sentiment.sentiment = MarketSentiment.NEUTRAL
            elif fear_greed_score >= 20:
                sentiment.sentiment = MarketSentiment.FEAR
            else:
                sentiment.sentiment = MarketSentiment.EXTREME_FEAR
            
            # 计算市场热度
            sentiment.market_heat = self._calculate_market_heat(overview)
            
            # 分析资金流向
            sentiment.fund_flow_trend = self._analyze_fund_flow(overview)
            
            # 计算量比
            sentiment.volume_ratio = self._calculate_volume_ratio(overview)
            
            logger.info(f"[情绪分析] 恐慌贪婪指数: {fear_greed_score:.1f}, 情绪: {sentiment.sentiment.value}")
            
        except Exception as e:
            logger.error(f"[情绪分析] 分析失败: {e}")
        
        return sentiment
    
    def _calculate_fear_greed_index(self, overview: MarketOverview) -> float:
        """计算恐慌贪婪指数"""
        score = 50.0  # 基础分数
        
        # 基于上证指数涨跌幅调整
        sh_index = next((idx for idx in overview.indices if "000001" in idx.code), None)
        if sh_index:
            # 涨跌幅权重 (±30分)
            score += sh_index.change_pct * 10
            
            # 振幅权重 (高振幅+10分，低振幅-5分)
            if sh_index.amplitude > 2.0:
                score += 10
            elif sh_index.amplitude < 0.5:
                score -= 5
        
        # 涨跌比权重
        if overview.up_count > 0 and overview.down_count > 0:
            up_down_ratio = overview.up_count / (overview.up_count + overview.down_count)
            score += (up_down_ratio - 0.5) * 40  # ±20分
        
        # 涨停跌停比权重
        if overview.limit_up_count > overview.limit_down_count * 2:
            score += 15
        elif overview.limit_down_count > overview.limit_up_count * 2:
            score -= 15
        
        # 成交额权重（相对历史平均）
        if overview.total_amount > 12000:  # 高于1.2万亿
            score += 10
        elif overview.total_amount < 8000:  # 低于8千亿
            score -= 10
        
        return max(0, min(100, score))
    
    def _calculate_market_heat(self, overview: MarketOverview) -> float:
        """计算市场热度"""
        heat = 50.0
        
        # 成交额热度
        if overview.total_amount > 15000:
            heat += 25
        elif overview.total_amount > 12000:
            heat += 15
        elif overview.total_amount < 8000:
            heat -= 15
        
        # 涨停数量热度
        if overview.limit_up_count > 50:
            heat += 20
        elif overview.limit_up_count > 30:
            heat += 10
        elif overview.limit_up_count < 10:
            heat -= 10
        
        return max(0, min(100, heat))
    
    def _analyze_fund_flow(self, overview: MarketOverview) -> str:
        """分析资金流向趋势"""
        if overview.total_amount > 12000 and overview.up_count > overview.down_count:
            return "增量资金入场"
        elif overview.total_amount < 8000:
            return "存量资金博弈"
        elif overview.down_count > overview.up_count * 1.5:
            return "资金谨慎观望"
        else:
            return "资金流向平衡"
    
    def _calculate_volume_ratio(self, overview: MarketOverview) -> float:
        """计算量比（简化版本）"""
        # 这里简化处理，实际应该对比5日平均成交额
        if overview.total_amount > 12000:
            return 1.2
        elif overview.total_amount > 10000:
            return 1.1
        elif overview.total_amount < 8000:
            return 0.8
        else:
            return 1.0
    
    def analyze_sector_hotspots(self, overview: MarketOverview) -> List[SectorHotspot]:
        """
        分析板块热点
        
        Args:
            overview: 市场概览
            
        Returns:
            List[SectorHotspot]: 热点板块列表
        """
        hotspots = []
        
        try:
            # 分析涨幅前5板块
            for sector_data in overview.top_sectors[:5]:
                hotspot = SectorHotspot(
                    name=sector_data['name'],
                    change_pct=sector_data['change_pct']
                )
                
                # 评估持续性
                if hotspot.change_pct > 5:
                    hotspot.sustainability = "强势突破，关注持续性"
                elif hotspot.change_pct > 3:
                    hotspot.sustainability = "温和上涨，可持续关注"
                else:
                    hotspot.sustainability = "涨幅有限，观察为主"
                
                # 添加概念标签（基于板块名称推断）
                hotspot.concept_tags = self._infer_concept_tags(hotspot.name)
                
                # 分析催化剂（基于新闻搜索）
                hotspot.catalyst = self._analyze_sector_catalyst(hotspot.name)
                
                hotspots.append(hotspot)
                
            logger.info(f"[板块分析] 识别到 {len(hotspots)} 个热点板块")
            
        except Exception as e:
            logger.error(f"[板块分析] 分析失败: {e}")
        
        return hotspots
    
    def _infer_concept_tags(self, sector_name: str) -> List[str]:
        """根据板块名称推断概念标签"""
        concept_map = {
            "新能源": ["碳中和", "清洁能源", "政策支持"],
            "芯片": ["科技自主", "国产替代", "高端制造"],
            "医药": ["创新药", "医疗改革", "人口老龄化"],
            "军工": ["国防安全", "装备升级", "自主可控"],
            "房地产": ["政策调控", "基建投资", "城镇化"],
            "银行": ["金融改革", "利率政策", "经济复苏"],
            "白酒": ["消费升级", "品牌价值", "节庆效应"],
            "汽车": ["智能化", "电动化", "产业升级"]
        }
        
        tags = []
        for key, values in concept_map.items():
            if key in sector_name:
                tags.extend(values)
                break
        
        return tags[:3]  # 最多返回3个标签
    
    def _analyze_sector_catalyst(self, sector_name: str) -> str:
        """分析板块催化剂"""
        return "市场资金关注"
    
    def analyze_external_environment(self) -> ExternalEnvironment:
        """
        分析外界环境
        
        Returns:
            ExternalEnvironment: 外界环境分析
        """
        env = ExternalEnvironment()
        
        try:
            # 获取国际市场数据
            env.us_futures = self._get_us_futures()
            env.asia_markets = self._get_asia_markets()
            
            # 分析政策面
            env.policy_impact = self._analyze_policy_impact()
            
            # 分析国际市场
            env.international_market = self._analyze_international_market(env.us_futures, env.asia_markets)
            
            # 分析宏观数据
            env.macro_data = self._analyze_macro_data()
            
            logger.info("[环境分析] 外界环境分析完成")
            
        except Exception as e:
            logger.error(f"[环境分析] 分析失败: {e}")
        
        return env
    
    def _get_us_futures(self) -> Dict[str, float]:
        """获取美股期货数据"""
        # 这里应该调用实际的数据接口
        # 简化版本返回模拟数据
        return {
            "纳指期货": 0.2,
            "标普期货": 0.1,
            "道指期货": 0.15
        }
    
    def _get_asia_markets(self) -> Dict[str, float]:
        """获取亚太市场数据"""
        # 这里应该调用实际的数据接口
        return {
            "日经225": -0.3,
            "韩国综指": 0.1,
            "台湾加权": 0.2
        }
    
    def _analyze_policy_impact(self) -> str:
        """分析政策面影响"""
        # 简化版本返回通用分析，实际可接入新闻搜索
        return "政策面保持稳定，关注后续政策信号"
    
    def _analyze_international_market(self, us_futures: Dict, asia_markets: Dict) -> str:
        """分析国际市场表现"""
        # 简化版本，实际应根据真实数据计算
        return "外围市场走势分化，对A股影响中性"
    
    def _analyze_macro_data(self) -> str:
        """分析宏观经济数据"""
        return "宏观经济数据处于观察期"
    
    def analyze_technical_aspects(self, overview: MarketOverview) -> TechnicalAnalysis:
        """
        技术面分析
        
        Args:
            overview: 市场概览
            
        Returns:
            TechnicalAnalysis: 技术分析结果
        """
        tech = TechnicalAnalysis()
        
        try:
            # 分析上证指数技术面
            sh_index = next((idx for idx in overview.indices if "000001" in idx.code), None)
            if sh_index:
                # 计算关键位置
                tech.key_support = sh_index.current * 0.98  # 简化计算
                tech.key_resistance = sh_index.current * 1.02
                
                # 判断趋势方向
                if sh_index.change_pct > 1:
                    tech.trend_direction = "强势上涨"
                elif sh_index.change_pct > 0:
                    tech.trend_direction = "温和上涨"
                elif sh_index.change_pct > -1:
                    tech.trend_direction = "震荡整理"
                elif sh_index.change_pct > -2:
                    tech.trend_direction = "温和下跌"
                else:
                    tech.trend_direction = "快速下跌"
                
                # 分析量价关系
                tech.volume_price_relation = self._analyze_volume_price(sh_index, overview.total_amount)
                
                # 市场结构分析
                tech.market_structure = self._analyze_market_structure(overview)
            
            logger.info(f"[技术分析] 趋势: {tech.trend_direction}, 支撑: {tech.key_support:.0f}, 阻力: {tech.key_resistance:.0f}")
            
        except Exception as e:
            logger.error(f"[技术分析] 分析失败: {e}")
        
        return tech
    
    def _analyze_volume_price(self, index: MarketIndex, total_amount: float) -> str:
        """分析量价关系"""
        if index.change_pct > 0 and total_amount > 10000:
            return "量价齐升，上涨动能较强"
        elif index.change_pct > 0 and total_amount < 8000:
            return "价涨量缩，上涨动能略显不足"
        elif index.change_pct < 0 and total_amount > 10000:
            return "放量下跌，抛压较重"
        elif index.change_pct < 0 and total_amount < 8000:
            return "缩量下跌，抛压有所减轻"
        else:
            return "量价关系相对平衡"
    
    def _analyze_market_structure(self, overview: MarketOverview) -> str:
        """分析市场结构"""
        up_ratio = overview.up_count / (overview.up_count + overview.down_count) if (overview.up_count + overview.down_count) > 0 else 0.5
        
        if up_ratio > 0.7:
            return "普涨格局，市场结构健康"
        elif up_ratio > 0.6:
            return "多数上涨，结构相对均衡"
        elif up_ratio < 0.3:
            return "普跌格局，市场结构偏弱"
        elif up_ratio < 0.4:
            return "多数下跌，结构承压"
        else:
            return "涨跌参半，结构分化"
    
    def generate_enhanced_report(self, enhanced_data: EnhancedMarketReport) -> str:
        """
        生成增强版复盘报告（适合公众号发布）
        
        Args:
            enhanced_data: 增强版市场数据
            
        Returns:
            str: 格式化的复盘报告
        """
        if not self.analyzer or not self.analyzer.is_available():
            logger.warning("[增强报告] AI分析器未配置，使用模板生成")
            return self._generate_enhanced_template_report(enhanced_data)
        
        # 构建增强版 Prompt
        prompt = self._build_enhanced_prompt(enhanced_data)
        
        logger.info("[增强报告] 调用AI生成增强版复盘报告...")
        report = self.analyzer.generate_text(prompt, max_tokens=3000, temperature=0.7)
        
        if report:
            logger.info(f"[增强报告] 报告生成成功，长度: {len(report)} 字符")
            # 注入结构化数据
            return self._inject_enhanced_data(report, enhanced_data)
        else:
            logger.warning("[增强报告] AI生成失败，使用模板报告")
            return self._generate_enhanced_template_report(enhanced_data)
    
    def _build_enhanced_prompt(self, data: EnhancedMarketReport) -> str:
        """构建增强版报告Prompt"""
        
        # 基础市场数据
        indices_text = ""
        for idx in data.basic_overview.indices:
            direction = "↑" if idx.change_pct > 0 else "↓" if idx.change_pct < 0 else "-"
            indices_text += f"- {idx.name}: {idx.current:.2f} ({direction}{abs(idx.change_pct):.2f}%)\n"
        
        # 情绪分析数据
        sentiment_text = f"""
情绪等级: {data.sentiment_analysis.sentiment.value}
恐慌贪婪指数: {data.sentiment_analysis.fear_greed_index:.1f}/100
市场热度: {data.sentiment_analysis.market_heat:.1f}/100
资金流向: {data.sentiment_analysis.fund_flow_trend}
"""
        
        # 板块热点数据
        hotspots_text = ""
        for hotspot in data.sector_hotspots[:5]:
            hotspots_text += f"- {hotspot.name}: {hotspot.change_pct:+.2f}% ({hotspot.sustainability})\n"
            if hotspot.concept_tags:
                hotspots_text += f"  概念: {', '.join(hotspot.concept_tags)}\n"
        
        # 外界环境数据
        env_text = f"""
政策面: {data.external_environment.policy_impact}
国际市场: {data.external_environment.international_market}
宏观数据: {data.external_environment.macro_data}
"""
        
        # 技术面数据
        tech_text = f"""
趋势方向: {data.technical_analysis.trend_direction}
关键支撑: {data.technical_analysis.key_support:.0f}
关键阻力: {data.technical_analysis.key_resistance:.0f}
量价关系: {data.technical_analysis.volume_price_relation}
市场结构: {data.technical_analysis.market_structure}
"""
        
        return f"""你是一位资深的A股市场分析师，请根据以下全面的市场数据生成一份专业的大盘复盘报告，适合在公众号发布。

【重要】输出要求：
- 必须输出纯 Markdown 文本格式
- 语言风格要专业但易懂，适合普通投资者阅读
- 重点突出市场情绪、板块热点、外界环境等关键信息
- 给出明确的策略建议和风险提示

---

# 今日市场全面数据

## 日期
{data.date}

## 基础指数行情
{indices_text}

## 市场统计
- 上涨: {data.basic_overview.up_count} 家 | 下跌: {data.basic_overview.down_count} 家
- 涨停: {data.basic_overview.limit_up_count} 家 | 跌停: {data.basic_overview.limit_down_count} 家  
- 成交额: {data.basic_overview.total_amount:.0f} 亿元

## 市场情绪分析
{sentiment_text}

## 板块热点分析
{hotspots_text}

## 外界环境分析
{env_text}

## 技术面分析
{tech_text}

---

# 输出格式模板（请严格按此格式输出）

## 📊 {data.date} A股智能复盘

### 🎯 一、市场概况
（用2-3句话概括今日市场整体表现，结合指数涨跌和成交量）

### 📈 二、情绪解读
（基于恐慌贪婪指数和市场热度，分析当前市场情绪状态及其含义）

### 🔥 三、热点聚焦
（深度解读领涨板块，分析背后的逻辑和催化剂，评估持续性）

### 🌍 四、外围影响
（分析政策面、国际市场等外界因素对A股的影响）

### 📊 五、技术研判
（从技术角度分析趋势、关键位置和量价关系）

### 💡 六、策略建议
（给出明确的操作建议，包括仓位配置和重点关注方向）

### ⚠️七、风险提示
（指出需要重点关注的风险点和应对策略）

---

请直接输出复盘报告内容，语言要专业权威，适合公众号读者。
"""
    
    def _inject_enhanced_data(self, report: str, data: EnhancedMarketReport) -> str:
        """向报告中注入结构化数据"""
        import re
        
        # 构建数据块
        stats_block = self._build_enhanced_stats_block(data)
        indices_block = self._build_indices_block(data.basic_overview)
        sentiment_block = self._build_sentiment_block(data.sentiment_analysis)
        hotspots_block = self._build_hotspots_block(data.sector_hotspots)
        
        # 注入数据块到对应章节
        if stats_block:
            report = self._insert_after_section(report, r'###\s*🎯\s*一、市场概况', stats_block)
        
        if sentiment_block:
            report = self._insert_after_section(report, r'###\s*📈\s*二、情绪解读', sentiment_block)
            
        if hotspots_block:
            report = self._insert_after_section(report, r'###\s*🔥\s*三、热点聚焦', hotspots_block)
        
        return report
    
    def _build_enhanced_stats_block(self, data: EnhancedMarketReport) -> str:
        """构建增强版统计数据块"""
        overview = data.basic_overview
        return f"""
> 📊 **市场数据速览**
> 
> 📈 涨跌: **{overview.up_count}** ↑ / **{overview.down_count}** ↓ / **{overview.flat_count}** → | 涨停: **{overview.limit_up_count}** / 跌停: **{overview.limit_down_count}**
> 
> 💰 成交额: **{overview.total_amount:.0f}** 亿 | 情绪指数: **{data.sentiment_analysis.fear_greed_index:.0f}**/100 ({data.sentiment_analysis.sentiment.value})
"""
    
    def _build_sentiment_block(self, sentiment: SentimentAnalysis) -> str:
        """构建情绪分析数据块"""
        return f"""
> 🎭 **情绪指标**
> 
> 恐慌贪婪指数: **{sentiment.fear_greed_index:.0f}**/100 | 市场热度: **{sentiment.market_heat:.0f}**/100
> 
> 资金流向: **{sentiment.fund_flow_trend}** | 量比: **{sentiment.volume_ratio:.1f}**
"""
    
    def _build_hotspots_block(self, hotspots: List[SectorHotspot]) -> str:
        """构建热点板块数据块"""
        if not hotspots:
            return ""
        
        lines = ["> 🔥 **热点板块**", "> "]
        for i, hotspot in enumerate(hotspots[:5], 1):
            emoji = "🚀" if hotspot.change_pct > 5 else "📈" if hotspot.change_pct > 3 else "🔥"
            lines.append(f"> {i}. {emoji} **{hotspot.name}** {hotspot.change_pct:+.2f}% - {hotspot.sustainability}")
            if hotspot.concept_tags:
                lines.append(f">    💡 {' | '.join(hotspot.concept_tags)}")
        
        return "\n".join(lines)
    
    def _generate_enhanced_template_report(self, data: EnhancedMarketReport) -> str:
        """生成增强版模板报告"""
        overview = data.basic_overview
        sentiment = data.sentiment_analysis
        
        # 指数表现
        indices_text = ""
        for idx in overview.indices[:4]:
            emoji = "🔴" if idx.change_pct < 0 else "🟢" if idx.change_pct > 0 else "⚪"
            indices_text += f"- **{idx.name}**: {idx.current:.2f} {emoji} {idx.change_pct:+.2f}%\n"
        
        # 热点板块
        hotspots_text = ""
        for hotspot in data.sector_hotspots[:3]:
            hotspots_text += f"- **{hotspot.name}**: {hotspot.change_pct:+.2f}% ({hotspot.sustainability})\n"
        
        # 综合评级
        risk_level, advice = data.get_overall_rating()
        
        report = f"""## 📊 {data.date} A股智能复盘

### 🎯 一、市场概况

今日A股市场整体呈现**{sentiment.sentiment.value}**态势，{sentiment.get_sentiment_description()}。

{self._build_enhanced_stats_block(data)}

### 📈 二、主要指数

{indices_text}

### 🔥 三、热点聚焦

{self._build_hotspots_block(data.sector_hotspots)}

### 🌍 四、外围影响

**政策面**: {data.external_environment.policy_impact}

**国际市场**: {data.external_environment.international_market}

### 📊 五、技术研判

**趋势方向**: {data.technical_analysis.trend_direction} {data.technical_analysis.get_trend_emoji()}

**关键位置**: 支撑 {data.technical_analysis.key_support:.0f} / 阻力 {data.technical_analysis.key_resistance:.0f}

**量价关系**: {data.technical_analysis.volume_price_relation}

### 💡 六、策略建议

**风险等级**: {risk_level}

**操作建议**: {advice}

**重点关注**: 优质成长股回调机会，关注政策催化板块

### ⚠️ 七、风险提示

市场有风险，投资需谨慎。本分析仅供参考，不构成投资建议。

---

*📝 复盘时间: {datetime.now().strftime('%H:%M')} | 🤖 AI智能分析*
"""
        return report
    
    def run_enhanced_daily_review(self) -> str:
        """
        执行增强版每日复盘流程
        
        Returns:
            str: 增强版复盘报告
        """
        logger.info("========== 开始增强版大盘复盘分析 ==========")
        
        # 1. 获取基础市场概览
        basic_overview = self.get_market_overview()
        
        # 2. 进行情绪分析
        sentiment_analysis = self.analyze_market_sentiment(basic_overview)
        
        # 3. 分析板块热点
        sector_hotspots = self.analyze_sector_hotspots(basic_overview)
        
        # 4. 分析外界环境
        external_environment = self.analyze_external_environment()
        
        # 5. 技术面分析
        technical_analysis = self.analyze_technical_aspects(basic_overview)
        
        # 6. 搜索市场新闻
        market_news = self.search_market_news()
        
        # 7. 组装增强版报告数据
        enhanced_data = EnhancedMarketReport(
            date=basic_overview.date,
            basic_overview=basic_overview,
            sentiment_analysis=sentiment_analysis,
            sector_hotspots=sector_hotspots,
            external_environment=external_environment,
            technical_analysis=technical_analysis,
            market_news=market_news
        )
        
        # 8. 生成最终报告
        report = self.generate_enhanced_report(enhanced_data)
        
        logger.info("========== 增强版大盘复盘分析完成 ==========")
        
        return report


# 测试入口
if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
    )
    
    # 测试增强版分析器
    analyzer = EnhancedMarketAnalyzer()
    report = analyzer.run_enhanced_daily_review()
    
    print("\n" + "="*80)
    print("增强版复盘报告")
    print("="*80)
    print(report)