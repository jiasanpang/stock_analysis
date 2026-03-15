import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import { backtestApi } from '../api/backtest';
import { pickerBacktestApi } from '../api/pickerBacktest';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, Pagination, Spinner } from '../components/common';
import type {
  BacktestResultItem,
  BacktestRunResponse,
  PerformanceMetrics,
} from '../types/backtest';
import type {
  PickerBacktestResultItem,
  PickerBacktestSummary,
  PickerBacktestHistoryItem,
  PickerMode,
} from '../types/pickerBacktest';

const PICKER_LEADER_EXEMPT: Record<PickerMode, number> = {
  defensive: 0,
  balanced: 12,
  offensive: 12,
};

function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(1)}%`;
}

function outcomeBadge(outcome?: string) {
  if (!outcome) return <Badge variant="default">--</Badge>;
  switch (outcome) {
    case 'win':
      return <Badge variant="success" glow>胜</Badge>;
    case 'loss':
      return <Badge variant="danger" glow>负</Badge>;
    case 'neutral':
      return <Badge variant="warning">平</Badge>;
    case 'insufficient':
      return <Badge variant="warning">数据不足</Badge>;
    default:
      return <Badge variant="default">{outcome}</Badge>;
  }
}

function statusBadge(status: string) {
  switch (status) {
    case 'completed':
      return <Badge variant="success">已完成</Badge>;
    case 'insufficient_data':
    case 'insufficient':
      return <Badge variant="warning">数据不足</Badge>;
    case 'error':
      return <Badge variant="danger">错误</Badge>;
    default:
      return <Badge variant="default">{status}</Badge>;
  }
}

function boolIcon(value?: boolean | null) {
  if (value === true) return <span className="text-emerald-500">✓</span>;
  if (value === false) return <span className="text-red-500">✗</span>;
  return <span className="text-muted">--</span>;
}

/* ── Stat cell for the horizontal performance panel ──────────── */
const StatCell: React.FC<{ label: string; value: string; accent?: boolean }> = ({ label, value, accent }) => (
  <div className="flex flex-col items-center px-3 py-2">
    <span className={`text-lg font-bold font-mono tabular-nums ${accent ? 'text-cyan' : 'text-primary'}`}>{value}</span>
    <span className="text-xs text-muted mt-0.5 whitespace-nowrap">{label}</span>
  </div>
);

/* ── Horizontal performance panel (full-width) ──────────────── */
const PerformancePanel: React.FC<{ metrics: PerformanceMetrics; title: string }> = ({ metrics, title }) => (
  <div className="bg-card border border-border rounded-2xl p-6">
    <div className="flex items-center gap-2 mb-5">
      <div className="w-8 h-8 rounded-lg bg-cyan/10 flex items-center justify-center">
        <svg className="w-4 h-4 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
        </svg>
      </div>
      <h2 className="text-sm font-semibold text-primary">{title}</h2>
      <span className="ml-auto text-xs text-muted font-mono">
        {Number(metrics.completedCount)} / {Number(metrics.totalEvaluations)} 已评估
        <span className="mx-2">·</span>
        <span className="text-emerald-500">{metrics.winCount}胜</span>
        {' / '}
        <span className="text-red-500">{metrics.lossCount}负</span>
        {' / '}
        <span className="text-amber-500">{metrics.neutralCount}平</span>
      </span>
    </div>
    <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-7 gap-1 divide-x divide-border/40">
      <StatCell label="方向准确率" value={pct(metrics.directionAccuracyPct)} accent />
      <StatCell label="胜率" value={pct(metrics.winRatePct)} accent />
      <StatCell label="模拟收益" value={pct(metrics.avgSimulatedReturnPct)} />
      <StatCell label="股票收益" value={pct(metrics.avgStockReturnPct)} />
      <StatCell label="止损触发" value={pct(metrics.stopLossTriggerRate)} />
      <StatCell label="止盈触发" value={pct(metrics.takeProfitTriggerRate)} />
      <StatCell label="触发天数" value={metrics.avgDaysToFirstHit != null ? metrics.avgDaysToFirstHit.toFixed(1) : '--'} />
    </div>
  </div>
);

/* ── Run Summary ─────────────────────────────────────────────── */
const RunSummary: React.FC<{ data: BacktestRunResponse }> = ({ data }) => (
  <div className="flex flex-wrap items-center gap-4 px-5 py-3 rounded-xl bg-card border border-border text-sm animate-fade-in">
    <span className="text-secondary">处理: <span className="text-primary font-semibold">{data.processed}</span></span>
    <span className="text-secondary">保存: <span className="text-cyan font-semibold">{data.saved}</span></span>
    <span className="text-secondary">完成: <span className="text-emerald-500 font-semibold">{data.completed}</span></span>
    <span className="text-secondary">数据不足: <span className="text-amber-500 font-semibold">{data.insufficient}</span></span>
    {data.errors > 0 && (
      <span className="text-secondary">错误: <span className="text-red-500 font-semibold">{data.errors}</span></span>
    )}
  </div>
);

/* ── Picker Backtest Panel ───────────────────────────────────── */
const PickerBacktestPanel: React.FC<{ summary: PickerBacktestSummary }> = ({ summary }) => (
  <div className="bg-card border border-border rounded-2xl p-6">
    <div className="flex items-center gap-2 mb-5">
      <div className="w-8 h-8 rounded-lg bg-cyan/10 flex items-center justify-center">
        <svg className="w-4 h-4 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"/>
        </svg>
      </div>
      <h2 className="text-sm font-semibold text-primary">选股回测表现</h2>
      <span className="ml-auto text-xs text-muted font-mono">
        {summary.winCount}胜 / {summary.lossCount}负 / {summary.insufficientCount}数据不足
      </span>
    </div>
    <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-1 divide-x divide-border/40">
      <StatCell label="胜率" value={summary.winRatePct != null ? `${summary.winRatePct.toFixed(1)}%` : '--'} accent />
      <StatCell label="平均收益" value={summary.avgReturnPct != null ? `${summary.avgReturnPct.toFixed(1)}%` : '--'} accent />
      <StatCell label="最大回撤" value={summary.maxDrawdownPct != null ? `${summary.maxDrawdownPct.toFixed(1)}%` : '--'} />
      <StatCell label="盈亏比" value={summary.profitFactor != null ? summary.profitFactor.toFixed(2) : '--'} />
      <StatCell label="超额收益" value={summary.alphaVsBenchmarkPct != null ? `${summary.alphaVsBenchmarkPct.toFixed(1)}%` : '--'} />
      <StatCell label="基准收益" value={summary.benchmarkAvgReturnPct != null ? `${summary.benchmarkAvgReturnPct.toFixed(1)}%` : '--'} />
    </div>
  </div>
);

/* ── Main page ───────────────────────────────────────────────── */
type BacktestTab = 'analysis' | 'picker';

const BacktestPage: React.FC = () => {
  const [activeTab, setActiveTab] = useState<BacktestTab>('analysis');
  const [codeFilter, setCodeFilter] = useState('');
  const [evalDays, setEvalDays] = useState('');
  const [forceRerun, setForceRerun] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [runResult, setRunResult] = useState<BacktestRunResponse | null>(null);
  const [runError, setRunError] = useState<ParsedApiError | null>(null);
  const [pageError, setPageError] = useState<ParsedApiError | null>(null);

  const [results, setResults] = useState<BacktestResultItem[]>([]);
  const [totalResults, setTotalResults] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [isLoadingResults, setIsLoadingResults] = useState(false);
  const pageSize = 20;

  const [overallPerf, setOverallPerf] = useState<PerformanceMetrics | null>(null);
  const [stockPerf, setStockPerf] = useState<PerformanceMetrics | null>(null);
  const [isLoadingPerf, setIsLoadingPerf] = useState(false);

  // Picker backtest state (default: last 3 months)
  const [pickerStartDate, setPickerStartDate] = useState(() => {
    const d = new Date();
    d.setMonth(d.getMonth() - 3);
    return d.toISOString().slice(0, 10);
  });
  const [pickerEndDate, setPickerEndDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [pickerHoldDays, setPickerHoldDays] = useState(10);
  const [pickerTopN, setPickerTopN] = useState(5);
  const [pickerRunning, setPickerRunning] = useState(false);
  const [pickerResult, setPickerResult] = useState<{ results: PickerBacktestResultItem[]; summary: PickerBacktestSummary | null } | null>(null);
  const [pickerError, setPickerError] = useState<ParsedApiError | null>(null);
  const [pickerMode, setPickerMode] = useState<PickerMode>('balanced');
  const [pickerHistory, setPickerHistory] = useState<PickerBacktestHistoryItem[]>([]);

  const fetchResults = useCallback(async (page = 1, code?: string, windowDays?: number) => {
    setIsLoadingResults(true);
    try {
      const response = await backtestApi.getResults({ code: code || undefined, evalWindowDays: windowDays, page, limit: pageSize });
      setResults(response.items);
      setTotalResults(response.total);
      setCurrentPage(response.page);
      setPageError(null);
    } catch (err) {
      setPageError(getParsedApiError(err));
    } finally {
      setIsLoadingResults(false);
    }
  }, []);

  const fetchPerformance = useCallback(async (code?: string, windowDays?: number) => {
    setIsLoadingPerf(true);
    try {
      const overall = await backtestApi.getOverallPerformance(windowDays);
      setOverallPerf(overall);
      if (code) {
        const stock = await backtestApi.getStockPerformance(code, windowDays);
        setStockPerf(stock);
      } else {
        setStockPerf(null);
      }
      setPageError(null);
    } catch (err) {
      setPageError(getParsedApiError(err));
    } finally {
      setIsLoadingPerf(false);
    }
  }, []);

  useEffect(() => {
    const init = async () => {
      try {
        const overall = await backtestApi.getOverallPerformance();
        setOverallPerf(overall);
        const windowDays = overall?.evalWindowDays;
        if (windowDays && !evalDays) setEvalDays(String(windowDays));
        fetchResults(1, undefined, windowDays);
      } catch {
        fetchResults(1);
      }
    };
    init();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshPickerHistory = useCallback(async () => {
    try {
      const hist = await pickerBacktestApi.getHistory({ limit: 10 });
      setPickerHistory(hist.items);
    } catch {
      // Non-critical; ignore
    }
  }, []);

  // Load last picker backtest from DB when picker tab is active (survives refresh)
  useEffect(() => {
    if (activeTab !== 'picker') return;
    const load = async () => {
      try {
        if (pickerResult == null) {
          const data = await pickerBacktestApi.getResults();
          if (data.results.length > 0 || data.summary) {
            setPickerResult({ results: data.results, summary: data.summary });
          }
        }
        await refreshPickerHistory();
      } catch {
        // Non-critical; ignore
      }
    };
    load();
  }, [activeTab, refreshPickerHistory]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleRun = async () => {
    setIsRunning(true);
    setRunResult(null);
    setRunError(null);
    try {
      const code = codeFilter.trim() || undefined;
      const evalWindowDays = evalDays ? parseInt(evalDays, 10) : undefined;
      const response = await backtestApi.run({
        code,
        force: forceRerun || undefined,
        minAgeDays: forceRerun ? 0 : undefined,
        evalWindowDays,
      });
      setRunResult(response);
      fetchResults(1, codeFilter.trim() || undefined, evalWindowDays);
      fetchPerformance(codeFilter.trim() || undefined, evalWindowDays);
    } catch (err) {
      setRunError(getParsedApiError(err));
    } finally {
      setIsRunning(false);
    }
  };

  const handleFilter = () => {
    const code = codeFilter.trim() || undefined;
    const windowDays = evalDays ? parseInt(evalDays, 10) : undefined;
    setCurrentPage(1);
    fetchResults(1, code, windowDays);
    fetchPerformance(code, windowDays);
  };

  const handleRunPicker = async () => {
    setPickerRunning(true);
    setPickerResult(null);
    setPickerError(null);
    try {
      const response = await pickerBacktestApi.run({
        startDate: pickerStartDate,
        endDate: pickerEndDate,
        holdDays: pickerHoldDays,
        topN: pickerTopN,
        pickerMode,
        pickerLeaderBiasExemptPct: PICKER_LEADER_EXEMPT[pickerMode],
      });
      setPickerResult({
        results: response.results,
        summary: response.summary,
      });
      await refreshPickerHistory();
    } catch (err) {
      setPickerError(getParsedApiError(err));
    } finally {
      setPickerRunning(false);
    }
  };

  const handleLoadHistoryDetail = async (id: number) => {
    try {
      const data = await pickerBacktestApi.getHistoryDetail(id);
      setPickerResult({ results: data.results, summary: data.summary });
      setPickerError(null);
    } catch (err) {
      setPickerError(getParsedApiError(err));
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleFilter();
  };

  const totalPages = Math.ceil(totalResults / pageSize);
  const handlePageChange = (page: number) => {
    const windowDays = evalDays ? parseInt(evalDays, 10) : undefined;
    fetchResults(page, codeFilter.trim() || undefined, windowDays);
  };

  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-50 to-slate-100/80">
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="bg-card rounded-2xl shadow-[0_1px_3px_rgba(0,0,0,0.04),0_8px_24px_rgba(0,0,0,0.06)] border border-border/80 overflow-hidden">

        {/* ─── Hero ─── */}
        <div className="relative px-8 pt-10 pb-8 md:px-10 md:pt-12 md:pb-10 bg-gradient-to-br from-cyan/5 via-transparent to-purple/5">
          <div className="text-center">
            <div className="inline-flex items-center justify-center w-14 h-14 rounded-xl
                            bg-gradient-to-br from-cyan/15 to-cyan/5 border border-cyan/20 mb-5">
              <svg className="w-7 h-7 text-cyan" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                      d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/>
              </svg>
            </div>
            <h1 className="text-2xl md:text-3xl font-bold text-primary mb-2 tracking-tight">回测</h1>
            <p className="text-sm text-secondary max-w-2xl mx-auto leading-relaxed">
              {activeTab === 'analysis'
                ? '验证历史 AI 分析的准确性：对比预测方向与实际走势，评估止损止盈触发情况'
                : '验证量化选股策略：按历史日期运行筛选器，统计持仓收益与超额收益'}
            </p>
          </div>
        </div>

        {/* ─── Tabs ─── */}
        <div className="flex justify-center px-6 pt-2 pb-4">
          <div className="inline-flex p-1 rounded-xl bg-elevated border border-border">
            <button
              type="button"
              onClick={() => setActiveTab('analysis')}
              className={`px-5 py-2.5 rounded-lg text-sm font-medium transition-all
                ${activeTab === 'analysis'
                  ? 'bg-cyan text-white shadow-sm'
                  : 'text-secondary hover:text-primary hover:bg-white/60'}`}
            >
              分析回测
            </button>
            <button
              type="button"
              onClick={() => setActiveTab('picker')}
              className={`px-5 py-2.5 rounded-lg text-sm font-medium transition-all
                ${activeTab === 'picker'
                  ? 'bg-cyan text-white shadow-sm'
                  : 'text-secondary hover:text-primary hover:bg-white/60'}`}
            >
              选股回测
            </button>
          </div>
        </div>

        {/* ─── Content area ─── */}
        <div className="px-6 pb-8 md:px-10 md:pb-10">
        {activeTab === 'analysis' && (
        <>
        {/* ─── Controls ─── */}
        <div className="bg-elevated/60 border border-border rounded-2xl p-6 mb-8">
          <div className="flex flex-wrap items-center gap-3">
            <input
              type="text"
              value={codeFilter}
              onChange={(e) => setCodeFilter(e.target.value.toUpperCase())}
              onKeyDown={handleKeyDown}
              placeholder="股票代码（留空查看全部）"
              disabled={isRunning}
              className="flex-1 min-w-[180px] px-4 py-2.5 rounded-xl bg-elevated border border-border
                         text-sm text-primary placeholder:text-muted
                         focus:outline-none focus:border-cyan/40 focus:ring-1 focus:ring-cyan/20 transition-all"
            />
            <div className="flex items-center gap-2">
              <span className="text-sm text-secondary">窗口</span>
              <input
                type="number"
                min={1}
                max={120}
                value={evalDays}
                onChange={(e) => setEvalDays(e.target.value)}
                placeholder="10"
                disabled={isRunning}
                className="w-16 px-3 py-2.5 rounded-xl bg-elevated border border-border
                           text-sm text-primary text-center
                           focus:outline-none focus:border-cyan/40 focus:ring-1 focus:ring-cyan/20 transition-all"
              />
              <span className="text-xs text-muted">天</span>
            </div>
            <button
              type="button"
              onClick={() => setForceRerun(!forceRerun)}
              disabled={isRunning}
              className={`flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium
                transition-all border cursor-pointer
                ${forceRerun
                  ? 'border-cyan/40 bg-cyan/10 text-cyan'
                  : 'border-border bg-elevated text-muted hover:border-border-accent hover:text-secondary'
                }
                disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              <span className={`w-2 h-2 rounded-full transition-colors
                ${forceRerun ? 'bg-cyan' : 'bg-muted/30'}`} />
              强制重算
            </button>
            <button
              type="button"
              onClick={handleFilter}
              disabled={isLoadingResults}
              className="px-5 py-2.5 rounded-xl bg-elevated border border-border text-sm font-medium
                         text-secondary hover:text-primary hover:border-border-accent transition-all"
            >
              筛选
            </button>
            <button
              type="button"
              onClick={handleRun}
              disabled={isRunning}
              className="px-6 py-2.5 bg-cyan text-white text-sm font-semibold rounded-xl
                         hover:bg-cyan/90 disabled:opacity-60 disabled:cursor-not-allowed
                         transition-all shadow-glow-cyan flex items-center gap-2"
            >
              {isRunning ? (
                <>
                  <Spinner size="sm" className="border-white/30 border-t-white" />
                  <span>回测中...</span>
                </>
              ) : (
                <>
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                  </svg>
                  <span>运行回测</span>
                </>
              )}
            </button>
          </div>
          {runResult && <div className="mt-4"><RunSummary data={runResult} /></div>}
          {runError && <ApiErrorAlert error={runError} className="mt-4" />}
        </div>

        {/* ─── Performance ─── */}
        {(overallPerf || isLoadingPerf) && (
          <div className="space-y-4 mb-8">
            {isLoadingPerf ? (
              <div className="flex items-center justify-center py-12">
                <Spinner size="lg" />
              </div>
            ) : (
              <>
                {overallPerf && <PerformancePanel metrics={overallPerf} title="整体表现" />}
                {stockPerf && <PerformancePanel metrics={stockPerf} title={stockPerf.code || codeFilter} />}
              </>
            )}
          </div>
        )}

        {/* ─── Error ─── */}
        {pageError && <ApiErrorAlert error={pageError} className="mb-6" />}

        {/* ─── Results Table ─── */}
        {isLoadingResults ? (
          <div className="flex flex-col items-center py-20">
            <Spinner size="lg" />
            <p className="mt-6 text-sm text-secondary">加载回测结果...</p>
          </div>
        ) : results.length === 0 ? (
          <div className="flex flex-col items-center py-20 text-center">
            <div className="w-16 h-16 mb-4 rounded-2xl bg-elevated flex items-center justify-center">
              <svg className="w-7 h-7 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
              </svg>
            </div>
            <h3 className="text-lg font-semibold text-primary mb-2">暂无回测数据</h3>
            <p className="text-sm text-muted max-w-md">
              系统会对历史分析记录进行回测验证。点击"运行回测"开始评估，或等待分析记录积累足够天数后自动生成。
            </p>
          </div>
        ) : (
          <div className="space-y-4 animate-fade-in">
            <div className="bg-card border border-border rounded-2xl overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border bg-elevated/50">
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">代码</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">名称</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">分析日期</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">建议</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">方向</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">结果</th>
                      <th className="px-4 py-3 text-right text-xs text-muted font-medium">收益率</th>
                      <th className="px-4 py-3 text-center text-xs text-muted font-medium">止损</th>
                      <th className="px-4 py-3 text-center text-xs text-muted font-medium">止盈</th>
                      <th className="px-4 py-3 text-left text-xs text-muted font-medium">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {results.map((row) => (
                      <tr
                        key={row.analysisHistoryId}
                        className="border-t border-border/50 hover:bg-surface-hover/50 transition-colors"
                      >
                        <td className="px-4 py-2.5 font-mono text-cyan text-xs">{row.code}</td>
                        <td className="px-4 py-2.5 text-sm text-primary font-medium truncate max-w-[120px]" title={row.name || ''}>{row.name || '--'}</td>
                        <td className="px-4 py-2.5 text-xs text-secondary">{row.analysisDate || '--'}</td>
                        <td className="px-4 py-2.5 text-sm text-primary truncate max-w-[100px]" title={row.operationAdvice || ''}>
                          {row.operationAdvice || '--'}
                        </td>
                        <td className="px-4 py-2.5 text-sm">
                          <span className="flex items-center gap-1.5">
                            {boolIcon(row.directionCorrect)}
                            <span className="text-muted text-xs">{row.directionExpected || ''}</span>
                          </span>
                        </td>
                        <td className="px-4 py-2.5">{outcomeBadge(row.outcome)}</td>
                        <td className="px-4 py-2.5 text-sm font-mono text-right">
                          <span className={
                            row.simulatedReturnPct != null
                              ? row.simulatedReturnPct > 0 ? 'text-red-600' : row.simulatedReturnPct < 0 ? 'text-emerald-600' : 'text-secondary'
                              : 'text-muted'
                          }>
                            {pct(row.simulatedReturnPct)}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 text-center">{boolIcon(row.hitStopLoss)}</td>
                        <td className="px-4 py-2.5 text-center">{boolIcon(row.hitTakeProfit)}</td>
                        <td className="px-4 py-2.5">{statusBadge(row.evalStatus)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="flex items-center justify-between">
              <span className="text-sm text-muted">共 {totalResults} 条记录</span>
              <Pagination
                currentPage={currentPage}
                totalPages={totalPages}
                onPageChange={handlePageChange}
              />
            </div>
          </div>
        )}
        </>
        )}

        {/* ─── Picker Backtest ─── */}
        {activeTab === 'picker' && (
          <>
        {/* ─── Controls ─── */}
        <div className="bg-elevated/60 border border-border rounded-2xl p-6 mb-8">
          <p className="text-xs text-muted mb-4">
            选股回测需逐日调用 Tushare 等数据源，日期较多时可能需 5–15 分钟，请耐心等待。
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 items-end">
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted">开始</label>
              <input
                type="date"
                value={pickerStartDate}
                onChange={(e) => setPickerStartDate(e.target.value)}
                disabled={pickerRunning}
                className="w-full px-3 py-2.5 rounded-lg bg-card border border-border
                           text-sm text-primary
                           focus:outline-none focus:border-cyan/50 focus:ring-2 focus:ring-cyan/20 transition-all"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted">结束</label>
              <input
                type="date"
                value={pickerEndDate}
                onChange={(e) => setPickerEndDate(e.target.value)}
                disabled={pickerRunning}
                className="w-full px-3 py-2.5 rounded-lg bg-card border border-border
                           text-sm text-primary
                           focus:outline-none focus:border-cyan/50 focus:ring-2 focus:ring-cyan/20 transition-all"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted">持仓天数</label>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min={1}
                  max={60}
                  value={pickerHoldDays}
                  onChange={(e) => setPickerHoldDays(parseInt(e.target.value, 10) || 10)}
                  disabled={pickerRunning}
                  className="flex-1 px-3 py-2.5 rounded-lg bg-card border border-border
                             text-sm text-primary text-center
                             focus:outline-none focus:border-cyan/50 focus:ring-2 focus:ring-cyan/20 transition-all"
                />
                <span className="text-xs text-muted shrink-0">天</span>
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted">每日只数</label>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={pickerTopN}
                  onChange={(e) => setPickerTopN(parseInt(e.target.value, 10) || 5)}
                  disabled={pickerRunning}
                  className="flex-1 px-3 py-2.5 rounded-lg bg-card border border-border
                             text-sm text-primary text-center
                             focus:outline-none focus:border-cyan/50 focus:ring-2 focus:ring-cyan/20 transition-all"
                />
                <span className="text-xs text-muted shrink-0">只</span>
              </div>
            </div>
            <div className="flex flex-col gap-1.5 min-w-0">
              <label className="text-xs font-medium text-muted">模式</label>
              <select
                value={pickerMode}
                onChange={(e) => setPickerMode(e.target.value as PickerMode)}
                disabled={pickerRunning}
                title={pickerMode === 'defensive' ? '严进 (龙头不豁免)' : pickerMode === 'balanced' ? '平衡 (龙头可放宽12%)' : '进攻 (龙头可放宽12%)'}
                className="w-full min-w-0 px-3 py-2.5 rounded-lg bg-card border border-border text-sm text-primary
                           focus:outline-none focus:border-cyan/50 focus:ring-2 focus:ring-cyan/20 transition-all"
              >
                <option value="defensive">严进</option>
                <option value="balanced">平衡</option>
                <option value="offensive">进攻</option>
              </select>
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-medium text-muted invisible">操作</label>
              <button
                type="button"
                onClick={handleRunPicker}
                disabled={pickerRunning}
                className="w-full min-h-[42px] px-5 py-2.5 bg-cyan text-white text-sm font-semibold rounded-lg
                           hover:opacity-95 disabled:opacity-60 disabled:cursor-not-allowed
                           transition-all shadow-glow-cyan flex items-center justify-center gap-2 whitespace-nowrap"
              >
                {pickerRunning ? (
                  <>
                    <Spinner size="sm" className="border-white/30 border-t-white shrink-0" />
                    <span title="约 5–15 分钟">回测中…</span>
                  </>
                ) : (
                  <>
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/>
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
                    </svg>
                    <span>运行选股回测</span>
                  </>
                )}
              </button>
            </div>
          </div>
          {pickerError && <ApiErrorAlert error={pickerError} className="mt-4" />}
        </div>

        {/* ─── History ─── */}
        {pickerHistory.length > 0 && (
          <div className="mb-8">
            <h3 className="text-sm font-medium text-muted mb-3">历史记录</h3>
            <div className="flex flex-wrap gap-2">
              {pickerHistory.map((h) => (
                <button
                  key={h.id}
                  type="button"
                  onClick={() => handleLoadHistoryDetail(h.id)}
                  className="px-3 py-2 rounded-lg bg-elevated border border-border text-xs text-secondary
                             hover:border-cyan/40 hover:text-primary transition-all text-left"
                >
                  <span className="font-mono">{h.startDate}–{h.endDate}</span>
                  <span className="mx-1.5 text-muted">|</span>
                  <span>{h.holdDays}d×{h.topN}</span>
                  {h.winRatePct != null && (
                    <span className="ml-1.5 text-cyan">{h.winRatePct.toFixed(1)}%</span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* ─── Performance (same pattern as Analysis) ─── */}
        {pickerResult?.summary && (
          <div className="mb-8">
            <PickerBacktestPanel summary={pickerResult.summary} />
          </div>
        )}

        {/* ─── Results Table ─── */}
        {pickerResult?.results && pickerResult.results.length > 0 && (
          <div className="space-y-4 animate-fade-in">
              <div className="bg-card border border-border rounded-2xl overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border bg-elevated/50">
                        <th className="px-4 py-3 text-left text-xs text-muted font-medium">日期</th>
                        <th className="px-4 py-3 text-left text-xs text-muted font-medium">代码</th>
                        <th className="px-4 py-3 text-left text-xs text-muted font-medium">名称</th>
                        <th className="px-4 py-3 text-right text-xs text-muted font-medium">买入价</th>
                        <th className="px-4 py-3 text-right text-xs text-muted font-medium">收益率</th>
                        <th className="px-4 py-3 text-left text-xs text-muted font-medium">结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pickerResult.results.map((row, idx) => (
                        <tr
                          key={`${row.tradeDate}-${row.code}-${idx}`}
                          className="border-t border-border/50 hover:bg-surface-hover/50 transition-colors"
                        >
                          <td className="px-4 py-2.5 text-xs text-secondary">{row.tradeDate}</td>
                          <td className="px-4 py-2.5 font-mono text-cyan text-xs">{row.code}</td>
                          <td className="px-4 py-2.5 text-sm text-primary truncate max-w-[120px]" title={row.name || ''}>{row.name || '--'}</td>
                          <td className="px-4 py-2.5 text-sm font-mono text-right">{row.entryPrice?.toFixed(2) ?? '--'}</td>
                          <td className="px-4 py-2.5 text-sm font-mono text-right">
                            <span className={
                              row.returnPct != null
                                ? row.returnPct > 0 ? 'text-red-600' : row.returnPct < 0 ? 'text-emerald-600' : 'text-secondary'
                                : 'text-muted'
                            }>
                              {row.returnPct != null ? `${row.returnPct.toFixed(1)}%` : '--'}
                            </span>
                          </td>
                          <td className="px-4 py-2.5">{outcomeBadge(row.outcome)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
          </div>
        )}
        </>
        )}

        </div>
        </div>
      </div>
    </div>
  );
};

export default BacktestPage;
