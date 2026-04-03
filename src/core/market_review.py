# -*- coding: utf-8 -*-
"""
===================================
股票智能分析系统 - 大盘复盘模块（支持 A 股 / 美股）
===================================

职责：
1. 根据 MARKET_REVIEW_REGION 配置选择市场区域（cn / us / both）
2. 执行大盘复盘分析并生成复盘报告
3. 保存和发送复盘报告
"""

import logging
from datetime import datetime
from typing import Optional

from src.config import get_config
from src.notification import NotificationService
from src.market_analyzer import MarketAnalyzer
from src.enhanced_market_analyzer import EnhancedMarketAnalyzer
from src.wechat_formatter import WechatFormatter, PublishPlatform
from src.search_service import SearchService
from src.analyzer import GeminiAnalyzer


logger = logging.getLogger(__name__)


def run_market_review(
    notifier: NotificationService,
    analyzer: Optional[GeminiAnalyzer] = None,
    search_service: Optional[SearchService] = None,
    send_notification: bool = True,
    merge_notification: bool = False,
    override_region: Optional[str] = None,
    use_enhanced_analyzer: bool = True,
) -> Optional[str]:
    """
    执行大盘复盘分析

    Args:
        notifier: 通知服务
        analyzer: AI分析器（可选）
        search_service: 搜索服务（可选）
        send_notification: 是否发送通知
        merge_notification: 是否合并推送（跳过本次推送，由 main 层合并个股+大盘后统一发送，Issue #190）
        override_region: 覆盖 config 的 market_review_region（Issue #373 交易日过滤后有效子集）
        use_enhanced_analyzer: 是否使用增强版分析器（适合公众号发布）

    Returns:
        复盘报告文本
    """
    logger.info("开始执行大盘复盘分析...")
    config = get_config()
    region = (
        override_region
        if override_region is not None
        else (getattr(config, 'market_review_region', 'cn') or 'cn')
    )
    if region not in ('cn', 'us', 'both'):
        region = 'cn'

    try:
        # 根据配置选择使用增强版分析器还是标准分析器
        config = get_config()
        use_enhanced = use_enhanced_analyzer and getattr(config, 'use_enhanced_market_review', True)
        
        if region == 'both':
            # 顺序执行 A 股 + 美股，合并报告
            if use_enhanced:
                cn_analyzer = EnhancedMarketAnalyzer(
                    search_service=search_service, analyzer=analyzer, region='cn'
                )
                us_analyzer = EnhancedMarketAnalyzer(
                    search_service=search_service, analyzer=analyzer, region='us'
                )
                logger.info("生成增强版 A 股大盘复盘报告...")
                cn_report = cn_analyzer.run_enhanced_daily_review()
                logger.info("生成增强版美股大盘复盘报告...")
                us_report = us_analyzer.run_enhanced_daily_review()
            else:
                cn_analyzer = MarketAnalyzer(
                    search_service=search_service, analyzer=analyzer, region='cn'
                )
                us_analyzer = MarketAnalyzer(
                    search_service=search_service, analyzer=analyzer, region='us'
                )
                logger.info("生成 A 股大盘复盘报告...")
                cn_report = cn_analyzer.run_daily_review()
                logger.info("生成美股大盘复盘报告...")
                us_report = us_analyzer.run_daily_review()
                
            review_report = ''
            if cn_report:
                review_report = f"# A股大盘复盘\n\n{cn_report}"
            if us_report:
                if review_report:
                    review_report += "\n\n---\n\n> 以下为美股大盘复盘\n\n"
                review_report += f"# 美股大盘复盘\n\n{us_report}"
            if not review_report:
                review_report = None
        else:
            if use_enhanced:
                market_analyzer = EnhancedMarketAnalyzer(
                    search_service=search_service,
                    analyzer=analyzer,
                    region=region,
                )
                logger.info(f"使用增强版分析器生成 {region.upper()} 市场复盘...")
                review_report = market_analyzer.run_enhanced_daily_review()
            else:
                market_analyzer = MarketAnalyzer(
                    search_service=search_service,
                    analyzer=analyzer,
                    region=region,
                )
                review_report = market_analyzer.run_daily_review()
        
        if review_report:
            # 保存报告到文件
            date_str = datetime.now().strftime('%Y%m%d')
            
            # 如果使用增强版分析器，同时生成公众号格式
            if use_enhanced:
                # 保存原始报告
                report_filename = f"market_review_enhanced_{date_str}.md"
                filepath = notifier.save_report_to_file(
                    f"# 🎯 大盘复盘（增强版）\n\n{review_report}", 
                    report_filename
                )
                logger.info(f"增强版大盘复盘报告已保存: {filepath}")
                
                # 生成公众号格式
                try:
                    formatter = WechatFormatter()
                    wechat_report = formatter.format_market_review(
                        review_report, 
                        PublishPlatform.WECHAT
                    )
                    
                    # 保存公众号格式报告
                    wechat_filename = f"market_review_wechat_{date_str}.md"
                    wechat_filepath = notifier.save_report_to_file(
                        wechat_report,
                        wechat_filename
                    )
                    logger.info(f"公众号格式报告已保存: {wechat_filepath}")
                    
                    # 生成标题建议
                    title_suggestions = formatter.create_title_suggestions(review_report)
                    logger.info(f"标题建议: {title_suggestions[:3]}")
                    
                except Exception as e:
                    logger.error(f"生成公众号格式失败: {e}")
            else:
                # 标准报告
                report_filename = f"market_review_{date_str}.md"
                filepath = notifier.save_report_to_file(
                    f"# 🎯 大盘复盘\n\n{review_report}", 
                    report_filename
                )
                logger.info(f"大盘复盘报告已保存: {filepath}")
            
            # 推送通知（合并模式下跳过，由 main 层统一发送）
            if merge_notification and send_notification:
                logger.info("合并推送模式：跳过大盘复盘单独推送，将在个股+大盘复盘后统一发送")
            elif send_notification and notifier.is_available():
                # 添加标题
                report_content = f"🎯 大盘复盘\n\n{review_report}"

                success = notifier.send(report_content, email_send_to_all=True)
                if success:
                    logger.info("大盘复盘推送成功")
                else:
                    logger.warning("大盘复盘推送失败")
            elif not send_notification:
                logger.info("已跳过推送通知 (--no-notify)")
            
            return review_report
        
    except Exception as e:
        logger.error(f"大盘复盘分析失败: {e}")
    
    return None
