import React, { useEffect, useState } from 'react';
import {
  fetchRecommendations,
  fetchPickerHistory,
  fetchPickerDetail,
  type PickerResponse,
  type PickerMode,
  type StockPick,
  type ScreenStats,
  type ScreenedStock,
  type PickerHistoryItem,
} from '../api/picker';
import { Spinner } from '../components/common';

const PICKER_MODE_LABELS: Record<string, string> = {
  defensive: '严进',
  balanced: '平衡',
  offensive: '进攻',
};

const ATTENTION_CFG: Record<string, { dot: string; badge: string; label: string }> = {
  high:   { dot: 'bg-red-500',    badge: 'bg-red-50 text-red-700 ring-red-200',       label: '强烈关注' },
  medium: { dot: 'bg-amber-500',  badge: 'bg-amber-50 text-amber-700 ring-amber-200', label: '适度关注' },
  low:    { dot: 'bg-sky-500',    badge: 'bg-sky-50 text-sky-700 ring-sky-200',        label: '跟踪观察' },
};

/* ── Screening funnel ────────────────────────────────────────── */
const ScreenFunnel: React.FC<{ stats: ScreenStats }> = ({ stats }) => {
  const steps = [
    { label: '全市场', value: stats.total_stocks, color: 'bg-slate-300' },
    { label: '基本面', value: stats.after_basic_filter, color: 'bg-blue-400' },
    { label: '动量趋势', value: stats.after_momentum_filter, color: 'bg-cyan' },
    { label: '量价活跃', value: stats.after_volume_filter, color: 'bg-emerald-500' },
    { label: '精选池', value: stats.final_pool, color: 'bg-amber-500' },
  ];

  return (
    <div className="bg-card border border-border rounded-2xl p-8">
      <div className="flex items-center gap-2 mb-6">
        <div className="w-8 h-8 rounded-lg bg-slate-100 flex items-center justify-center">
          <svg className="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2.586a1 1 0 01-.293.707l-6.414 6.414a1 1 0 00-.293.707V17l-4 4v-6.586a1 1 0 00-.293-.707L3.293 7.293A1 1 0 013 6.586V4z"/>
          </svg>
        </div>
        <h2 className="text-sm font-semibold text-primary">量化筛选漏斗</h2>
      </div>
      <div className="flex items-end gap-3">
        {steps.map((step, i) => {
          const maxVal = steps[0].value || 1;
          const ratio = Math.max(step.value / maxVal, 0.06);
          return (
            <React.Fragment key={step.label}>
              <div className="flex-1 flex flex-col items-center gap-2">
                <span className="text-lg font-bold text-primary tabular-nums">
                  {step.value.toLocaleString()}
                </span>
                <div
                  className={`w-full rounded-lg ${step.color} transition-all`}
                  style={{ height: `${Math.round(ratio * 120)}px`, minHeight: '8px' }}
                />
                <span className="text-xs font-medium text-secondary">{step.label}</span>
              </div>
              {i < steps.length - 1 && (
                <svg className="w-4 h-4 text-muted/40 shrink-0 mb-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/>
                </svg>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
};

/* ── Screened pool table (collapsible) ───────────────────────── */
const ScreenedPoolTable: React.FC<{ pool: ScreenedStock[] }> = ({ pool }) => {
  const [expanded, setExpanded] = useState(false);
  if (!pool.length) return null;

  const shown = expanded ? pool : pool.slice(0, 10);

  return (
    <div className="bg-card border border-border rounded-2xl overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-6 py-4 hover:bg-surface-hover transition-colors"
      >
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-cyan/10 flex items-center justify-center">
            <svg className="w-4 h-4 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16"/>
            </svg>
          </div>
          <h2 className="text-sm font-semibold text-primary">
            量化候选池 <span className="text-muted font-normal">({pool.length} 只)</span>
          </h2>
        </div>
        <svg
          className={`w-5 h-5 text-muted transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7"/>
        </svg>
      </button>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-t border-border bg-elevated/50">
              <th className="px-4 py-2.5 text-left text-xs text-muted font-medium">代码</th>
              <th className="px-4 py-2.5 text-left text-xs text-muted font-medium">名称</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">现价</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">涨跌%</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">量比</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">换手%</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">PE</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">市值(亿)</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">60日%</th>
              <th className="px-4 py-2.5 text-right text-xs text-muted font-medium">评分</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((s) => (
              <tr key={s.code} className="border-t border-border/50 hover:bg-surface-hover/50">
                <td className="px-4 py-2 font-mono text-muted">{s.code}</td>
                <td className="px-4 py-2 text-primary font-medium">{s.name}</td>
                <td className="px-4 py-2 text-right tabular-nums">{s.price.toFixed(2)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${s.change_pct > 0 ? 'text-red-600' : s.change_pct < 0 ? 'text-emerald-600' : 'text-muted'}`}>
                  {s.change_pct > 0 ? '+' : ''}{s.change_pct.toFixed(2)}%
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${s.volume_ratio > 1.5 ? 'text-red-600 font-medium' : ''}`}>
                  {s.volume_ratio.toFixed(1)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums">{s.turnover_rate.toFixed(1)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{s.pe.toFixed(0)}</td>
                <td className="px-4 py-2 text-right tabular-nums">{s.market_cap_yi.toFixed(0)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${s.change_pct_60d > 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                  {s.change_pct_60d > 0 ? '+' : ''}{s.change_pct_60d.toFixed(1)}%
                </td>
                <td className="px-4 py-2 text-right tabular-nums font-semibold text-cyan">{s.score.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pool.length > 10 && !expanded && (
        <button
          onClick={() => setExpanded(true)}
          className="w-full py-3 text-sm text-cyan hover:text-cyan/80 border-t border-border font-medium"
        >
          展开全部 {pool.length} 只 ↓
        </button>
      )}
    </div>
  );
};

/* ── Pick card ───────────────────────────────────────────────── */
const PickCard: React.FC<{ pick: StockPick; index: number }> = ({ pick, index }) => {
  const cfg = ATTENTION_CFG[pick.attention] || ATTENTION_CFG.medium;
  return (
    <div className="group relative bg-card border border-border rounded-2xl p-6
                    hover:border-border-accent hover:shadow-md transition-all">
      <span className={`absolute left-0 top-5 bottom-5 w-1 rounded-r-full ${cfg.dot} opacity-80`} />

      <div className="flex items-start justify-between mb-4 pl-4">
        <div className="flex items-center gap-4">
          <span className="text-3xl font-bold text-cyan/60 tabular-nums leading-none select-none">
            {String(index + 1).padStart(2, '0')}
          </span>
          <div>
            <div className="flex items-center gap-2.5">
              <h3 className="font-bold text-primary text-lg">{pick.name}</h3>
              <span className="text-sm text-muted font-mono">{pick.code}</span>
            </div>
            {pick.sector && (
              <span className="inline-block mt-0.5 text-xs text-cyan bg-cyan/8 px-2 py-0.5 rounded">
                {pick.sector}
              </span>
            )}
          </div>
        </div>
        <span className={`text-xs font-semibold px-3 py-1.5 rounded-full ring-1 ${cfg.badge}`}>
          {cfg.label}
        </span>
      </div>

      <p className="text-sm text-secondary pl-4 mb-4 leading-relaxed">{pick.reason}</p>

      <div className="flex flex-wrap gap-4 pl-4">
        {pick.catalyst && (
          <div className="flex items-center gap-2 text-sm bg-emerald-50 rounded-lg px-3 py-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
            <span className="text-emerald-700 font-medium">催化</span>
            <span className="text-emerald-600">{pick.catalyst}</span>
          </div>
        )}
        {pick.risk_note && (
          <div className="flex items-center gap-2 text-sm bg-red-50 rounded-lg px-3 py-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-red-400 shrink-0" />
            <span className="text-red-600 font-medium">风险</span>
            <span className="text-red-500">{pick.risk_note}</span>
          </div>
        )}
      </div>
    </div>
  );
};

/* ── Sector pill ─────────────────────────────────────────────── */
const SectorPill: React.FC<{ name: string }> = ({ name }) => (
  <span className="inline-flex items-center gap-1.5 text-sm font-medium
                   bg-cyan/8 text-cyan border border-cyan/15 px-4 py-1.5 rounded-full">
    {name}
  </span>
);

/* ── History list row ────────────────────────────────────────── */
const HistoryRow: React.FC<{ item: PickerHistoryItem; onSelect: (id: number) => void }> = ({ item, onSelect }) => {
  const ts = item.created_at ? new Date(item.created_at) : null;
  const dateStr = ts ? `${ts.getFullYear()}-${String(ts.getMonth() + 1).padStart(2, '0')}-${String(ts.getDate()).padStart(2, '0')}` : '';
  const timeStr = ts ? `${String(ts.getHours()).padStart(2, '0')}:${String(ts.getMinutes()).padStart(2, '0')}` : '';

  return (
    <button
      onClick={() => onSelect(item.id)}
      className="w-full text-left bg-card border border-border rounded-xl px-5 py-4
                 hover:border-border-accent hover:shadow-sm transition-all group"
    >
      <div className="flex items-center justify-between gap-4 mb-2">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-lg bg-cyan/10 flex items-center justify-center shrink-0">
            <svg className="w-4 h-4 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
            </svg>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-semibold text-primary">{dateStr}</span>
              <span className="text-xs text-muted">{timeStr}</span>
              <span className="text-xs bg-cyan/10 text-cyan px-2 py-0.5 rounded font-medium">
                {item.pick_count} 只推荐
              </span>
              <span className="text-xs text-muted border border-border px-2 py-0.5 rounded">
                {PICKER_MODE_LABELS[item.picker_mode ?? 'balanced'] ?? '平衡'}
              </span>
            </div>
            <p className="text-sm text-secondary truncate mt-0.5">{item.market_summary || '—'}</p>
          </div>
        </div>
        <svg className="w-5 h-5 text-muted group-hover:text-cyan shrink-0 transition-colors" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/>
        </svg>
      </div>

      {item.picks_preview.length > 0 && (
        <div className="flex flex-wrap gap-1.5 ml-12">
          {item.picks_preview.map((p) => {
            const cfg = ATTENTION_CFG[p.attention] || ATTENTION_CFG.medium;
            return (
              <span key={p.code} className={`text-xs px-2 py-0.5 rounded-full ring-1 ${cfg.badge}`}>
                {p.name}
              </span>
            );
          })}
          {item.pick_count > 5 && (
            <span className="text-xs text-muted px-2 py-0.5">+{item.pick_count - 5}</span>
          )}
        </div>
      )}
    </button>
  );
};

/* ── Pipeline step card (empty state) ────────────────────────── */
const PIPELINE_STEPS = [
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

/* ── Result detail view ──────────────────────────────────────── */
const ResultView: React.FC<{ result: PickerResponse; onBack?: () => void }> = ({ result, onBack }) => {
  const highPicks = result.picks.filter(p => p.attention === 'high');
  const otherPicks = result.picks.filter(p => p.attention !== 'high');

  return (
    <div className="space-y-8">
      {onBack && (
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-muted hover:text-primary transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7"/>
          </svg>
          返回列表
        </button>
      )}

      {result.screen_stats && <ScreenFunnel stats={result.screen_stats} />}

      {result.market_summary && (
        <div className="bg-card border border-border rounded-2xl p-8">
          <div className="flex items-start justify-between gap-6">
            <div>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-8 h-8 rounded-lg bg-emerald-100 flex items-center justify-center">
                  <svg className="w-4 h-4 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>
                  </svg>
                </div>
                <h2 className="text-sm font-semibold text-primary">今日市场</h2>
              </div>
              <p className="text-base text-secondary leading-relaxed">{result.market_summary}</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {result.picker_mode && (
                <span className="text-xs font-medium text-muted border border-border px-2 py-1 rounded-lg">
                  {PICKER_MODE_LABELS[result.picker_mode] ?? result.picker_mode}
                </span>
              )}
              <span className="text-sm text-muted whitespace-nowrap bg-elevated px-3 py-1 rounded-lg">
                {result.generated_at}
              </span>
            </div>
          </div>
        </div>
      )}

      {result.sectors_to_watch.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-primary mb-4 flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-purple/10 flex items-center justify-center">
              <svg className="w-4 h-4 text-purple" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"/>
              </svg>
            </div>
            关注板块
          </h2>
          <div className="flex flex-wrap gap-2.5">
            {result.sectors_to_watch.map((s) => <SectorPill key={s} name={s} />)}
          </div>
        </div>
      )}

      {highPicks.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-primary mb-4 flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-red-50 flex items-center justify-center">
              <svg className="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z"/>
              </svg>
            </div>
            重点推荐
            <span className="text-muted font-normal">({highPicks.length})</span>
          </h2>
          <div className="grid gap-4">
            {highPicks.map((p, i) => <PickCard key={p.code} pick={p} index={i} />)}
          </div>
        </div>
      )}

      {otherPicks.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-primary mb-4 flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-amber-50 flex items-center justify-center">
              <svg className="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
              </svg>
            </div>
            {highPicks.length > 0 ? '其他关注' : '推荐标的'}
            <span className="text-muted font-normal">({otherPicks.length})</span>
          </h2>
          <div className="grid gap-4">
            {otherPicks.map((p, i) => <PickCard key={p.code} pick={p} index={highPicks.length + i} />)}
          </div>
        </div>
      )}

      {result.screened_pool.length > 0 && (
        <ScreenedPoolTable pool={result.screened_pool} />
      )}

      {result.risk_warning && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50/60 p-6 flex items-start gap-4">
          <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center shrink-0">
            <svg className="w-5 h-5 text-amber-600" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd"/>
            </svg>
          </div>
          <div>
            <p className="text-sm font-semibold text-amber-800 mb-1">风险提示</p>
            <p className="text-sm text-amber-700 leading-relaxed">{result.risk_warning}</p>
          </div>
        </div>
      )}

      <div className="flex items-center justify-between text-sm text-muted py-4 border-t border-border">
        <span>共 {result.picks.length} 只推荐 · 耗时 {result.elapsed_seconds}s</span>
        <span>以上内容由 AI 生成，仅供参考，不构成投资建议</span>
      </div>
    </div>
  );
};

/* ── Main page ───────────────────────────────────────────────── */
const MODE_OPTIONS: { value: PickerMode; label: string }[] = [
  { value: 'defensive', label: '严进 (PE≤50, 乖离率6%, 龙头不豁免)' },
  { value: 'balanced', label: '平衡 (PE≤100, 乖离率8%, 龙头可放宽至12%)' },
  { value: 'offensive', label: '进攻 (PE放宽, 乖离率10%, 龙头可放宽至12%)' },
];

// 各模式默认龙头豁免%：严进不豁免，平衡/进攻允许板块龙头放宽
const MODE_LEADER_EXEMPT: Record<PickerMode, number> = {
  defensive: 0,
  balanced: 12,
  offensive: 12,
};

const PickerPage: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PickerResponse | null>(null);
  const [error, setError] = useState('');
  const [pickerMode, setPickerMode] = useState<PickerMode>('balanced');

  const [history, setHistory] = useState<PickerHistoryItem[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [viewingHistoryId, setViewingHistoryId] = useState<number | null>(null);

  const loadHistory = async () => {
    setHistoryLoading(true);
    try {
      const data = await fetchPickerHistory(20, 0);
      setHistory(data.items);
      setHistoryTotal(data.total);
    } catch {
      // silently fail — history is non-critical
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => { loadHistory(); }, []);

  const handleRun = async () => {
    setLoading(true);
    setError('');
    setResult(null);
    setViewingHistoryId(null);
    try {
      const params = {
        picker_mode: pickerMode,
        picker_leader_bias_exempt_pct: MODE_LEADER_EXEMPT[pickerMode],
      };
      const data = await fetchRecommendations(params);
      if (data.success) {
        setResult(data);
        loadHistory();
      } else {
        setError(data.error || 'AI 选股失败');
      }
    } catch (e: any) {
      if (e?.response?.status === 504) {
        setError('连接上游服务超时：服务端访问外部依赖时超时，请稍后重试，或检查当前网络与代理设置。');
      } else {
        setError(e.message || '网络错误');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleViewHistory = async (id: number) => {
    setDetailLoading(true);
    setError('');
    setResult(null);
    setViewingHistoryId(id);
    try {
      const data = await fetchPickerDetail(id);
      setResult(data);
    } catch {
      setError('加载历史记录失败');
      setViewingHistoryId(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const handleBackToList = () => {
    setResult(null);
    setViewingHistoryId(null);
    setError('');
  };

  const showingResult = result && !loading && !detailLoading;
  const showEmptyState = !loading && !detailLoading && !result && !error;

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-12">

        {/* ─── Hero ─── */}
        <div className="text-center mb-12">
          <div className="inline-flex items-center justify-center w-20 h-20 rounded-3xl
                          bg-gradient-to-br from-cyan/15 to-blue-500/10 mb-6
                          shadow-[0_0_40px_rgba(37,99,235,0.08)]">
            <svg className="w-10 h-10 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
            </svg>
          </div>
          <h1 className="text-3xl font-bold text-primary mb-3 tracking-tight">AI 智能选股</h1>
          <p className="text-base text-secondary max-w-2xl mx-auto leading-relaxed">
            从全市场 5000+ 只 A 股中，经过多层量化筛选缩小范围，再结合板块轮动与新闻热点，<br className="hidden sm:block"/>
            由 AI 从候选池中精选最值得关注的标的
          </p>
        </div>

        {/* ─── Mode selector ─── */}
        <div className="flex justify-center mb-8">
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium text-secondary">选股模式</label>
            <select
              value={pickerMode}
              onChange={(e) => setPickerMode(e.target.value as PickerMode)}
              disabled={loading}
              className="px-4 py-2.5 rounded-xl border border-border bg-card text-primary text-sm min-w-[280px]
                         focus:ring-2 focus:ring-cyan/30 focus:border-cyan disabled:opacity-60"
            >
              {MODE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
        </div>

        {/* ─── Action button ─── */}
        <div className="flex justify-center mb-14">
          <button
            onClick={handleRun}
            disabled={loading}
            className="group relative px-10 py-4 bg-cyan text-white text-base font-semibold rounded-2xl
                       hover:bg-cyan/90 disabled:opacity-60 disabled:cursor-not-allowed
                       transition-all shadow-glow-cyan hover:shadow-[0_8px_30px_rgba(37,99,235,0.25)]
                       flex items-center gap-3"
          >
            {loading ? (
              <>
                <Spinner size="sm" className="border-white/30 border-t-white" />
                <span>正在分析市场...</span>
              </>
            ) : (
              <>
                <svg className="w-5 h-5 transition-transform group-hover:rotate-12 group-hover:scale-110" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
                <span>开始选股</span>
              </>
            )}
          </button>
        </div>

        {/* ─── Loading ─── */}
        {(loading || detailLoading) && (
          <div className="flex flex-col items-center py-20">
            <div className="relative">
              <div className="w-24 h-24 rounded-full border-2 border-cyan/10 flex items-center justify-center">
                <Spinner size="lg" />
              </div>
              {loading && <div className="absolute -inset-3 rounded-full border border-cyan/5 animate-ping" style={{ animationDuration: '2s' }} />}
            </div>
            <p className="mt-8 text-base text-primary font-semibold">
              {detailLoading ? '加载历史记录...' : '两阶段分析进行中'}
            </p>
            {loading && (
              <>
                <div className="mt-4 flex gap-8 text-sm text-muted">
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-cyan animate-pulse" />
                    <span>量化筛选</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-purple/50" />
                    <span>AI 精选</span>
                  </div>
                </div>
                <p className="mt-6 text-xs text-muted">全市场扫描 + 多层过滤 + 新闻检索 + AI 综合分析，预计 30-90 秒</p>
              </>
            )}
          </div>
        )}

        {/* ─── Error ─── */}
        {error && !loading && !detailLoading && (
          <div className="rounded-2xl border border-red-200 bg-red-50 p-6 mb-8 text-center">
            <p className="text-base text-red-700 font-medium">{error}</p>
            <button onClick={handleRun} className="mt-3 text-sm text-red-600 underline hover:no-underline font-medium">
              重试
            </button>
          </div>
        )}

        {/* ─── Results ─── */}
        {showingResult && (
          <ResultView
            result={result}
            onBack={viewingHistoryId ? handleBackToList : undefined}
          />
        )}

        {/* ─── Empty state ─── */}
        {showEmptyState && (
          <div className="space-y-12">

            {/* History list */}
            {history.length > 0 && (
              <div>
                <div className="flex items-center gap-2 mb-4">
                  <div className="w-8 h-8 rounded-lg bg-slate-100 flex items-center justify-center">
                    <svg className="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                  </div>
                  <h2 className="text-sm font-semibold text-primary">
                    历史筛选记录
                    <span className="text-muted font-normal ml-1">({historyTotal})</span>
                  </h2>
                </div>
                {historyLoading ? (
                  <div className="flex justify-center py-8">
                    <Spinner size="md" />
                  </div>
                ) : (
                  <div className="space-y-3">
                    {history.map((item) => (
                      <HistoryRow key={item.id} item={item} onSelect={handleViewHistory} />
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Pipeline visualization */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {PIPELINE_STEPS.map((step) => (
                <div key={step.title}
                     className="relative bg-card border border-border rounded-2xl p-8
                                hover:border-border-accent hover:shadow-md transition-all group overflow-hidden">
                  <div className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${step.color} opacity-60
                                   group-hover:opacity-100 transition-opacity`} />
                  <div className="flex items-center gap-3 mb-5">
                    <div className={`w-12 h-12 rounded-2xl bg-gradient-to-br ${step.color} text-white
                                    flex items-center justify-center shadow-sm`}>
                      {step.icon}
                    </div>
                    <div>
                      <span className="text-xs font-semibold text-cyan uppercase tracking-wider">{step.stage}</span>
                      <h3 className="text-lg font-bold text-primary">{step.title}</h3>
                    </div>
                  </div>
                  <ul className="space-y-2.5">
                    {step.items.map((item) => (
                      <li key={item} className="flex items-center gap-2.5 text-sm text-secondary">
                        <svg className="w-4 h-4 text-cyan shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7"/>
                        </svg>
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>

            {/* Flow diagram */}
            <div className="flex items-center justify-center gap-4 flex-wrap">
              {[
                { label: '5000+', sub: '全 A 股' },
                { label: '~200', sub: '基本面' },
                { label: '~80', sub: '动量' },
                { label: '~30', sub: '量价' },
                { label: '5-10', sub: 'AI 精选' },
              ].map((item, i, arr) => (
                <React.Fragment key={item.sub}>
                  <div className="text-center">
                    <div className="text-xl font-bold text-primary">{item.label}</div>
                    <div className="text-xs text-muted mt-0.5">{item.sub}</div>
                  </div>
                  {i < arr.length - 1 && (
                    <svg className="w-5 h-5 text-muted/30 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7"/>
                    </svg>
                  )}
                </React.Fragment>
              ))}
            </div>

            <p className="text-center text-sm text-muted">
              点击上方按钮，开始两阶段智能选股分析
            </p>
          </div>
        )}

      </div>
    </div>
  );
};

export default PickerPage;
