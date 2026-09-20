import type React from 'react';
import type { PickerStrategy } from '../../api/picker';

export const STRATEGY_OPTIONS: { value: PickerStrategy; label: string }[] = [
  { value: 'buy_pullback', label: '买回踩' },
  { value: 'bottom_reversal', label: '底部反转' },
];

export const ATTENTION_CFG: Record<string, { dot: string; badge: string; label: string }> = {
  high:   { dot: 'bg-red-500',    badge: 'bg-red-50 text-red-700 ring-red-200',       label: '强烈关注' },
  medium: { dot: 'bg-amber-500',  badge: 'bg-amber-50 text-amber-700 ring-amber-200', label: '适度关注' },
  low:    { dot: 'bg-sky-500',    badge: 'bg-sky-50 text-sky-700 ring-sky-200',        label: '跟踪观察' },
};

export interface PipelineStep {
  stage: string;
  title: string;
  color: string;
  items: string[];
  icon: React.ReactNode;
}

export const PIPELINE_STEPS: PipelineStep[] = [
  {
    stage: '阶段一',
    title: '量化筛选',
    color: 'from-blue-500 to-cyan',
    items: ['基本面过滤', '动量趋势验证', '量价活跃度检测'],
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/>
      </svg>
    ),
  },
  {
    stage: '阶段二',
    title: 'AI 精选',
    color: 'from-purple to-cyan',
    items: ['板块轮动分析', '新闻热点挖掘', '候选池综合评分'],
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
      </svg>
    ),
  },
];

export const FLOW_STEPS = [
  { label: '5000+', sub: '全 A 股' },
  { label: '~200', sub: '基本面' },
  { label: '~80', sub: '动量' },
  { label: '~30', sub: '量价' },
  { label: '5-10', sub: 'AI 精选' },
];
