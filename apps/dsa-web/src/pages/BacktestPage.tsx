import type React from 'react';
import { useState, useEffect, useCallback } from 'react';
import { backtestApi } from '../api/backtest';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, Pagination, Spinner } from '../components/common';
import type {
  BacktestResultItem,
  BacktestRunResponse,
  PerformanceMetrics,
} from '../types/backtest';

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

/* ── Main page ───────────────────────────────────────────────── */
const BacktestPage: React.FC = () => {
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

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleFilter();
  };

  const totalPages = Math.ceil(totalResults / pageSize);
  const handlePageChange = (page: number) => {
    const windowDays = evalDays ? parseInt(evalDays, 10) : undefined;
    fetchResults(page, codeFilter.trim() || undefined, windowDays);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-12">

        {/* ─── Hero ─── */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center justify-center w-20 h-20 rounded-3xl
                          bg-gradient-to-br from-emerald-500/15 to-cyan/10 mb-6
                          shadow-[0_0_40px_rgba(16,185,129,0.08)]">
            <svg className="w-10 h-10 text-emerald-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
            </svg>
          </div>
          <h1 className="text-3xl font-bold text-primary mb-3 tracking-tight">分析回测</h1>
          <p className="text-base text-secondary max-w-2xl mx-auto leading-relaxed">
            验证历史 AI 分析的准确性：对比预测方向与实际走势，评估止损止盈触发情况
          </p>
        </div>

        {/* ─── Controls ─── */}
        <div className="bg-card border border-border rounded-2xl p-6 mb-8">
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

      </div>
    </div>
  );
};

export default BacktestPage;
