import type React from 'react';
import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiErrorAlert, Spinner } from '../components/common';
import { getParsedApiError } from '../api/error';
import type { HistoryItem, AnalysisReport, TaskInfo } from '../types/analysis';
import { historyApi } from '../api/history';
import { analysisApi, DuplicateTaskError } from '../api/analysis';
import { validateStockCode } from '../utils/validation';
import { getRecentStartDate, getTodayInShanghai } from '../utils/format';
import { useAnalysisStore } from '../stores/analysisStore';
import { ReportSummary } from '../components/report';
import { HistoryList } from '../components/history';
import { TaskPanel } from '../components/tasks';
import { useTaskStream } from '../hooks';

/**
 * 首页 - 单页设计
 * 顶部输入 + 左侧历史 + 右侧报告
 */
const HomePage: React.FC = () => {
  const {
    error: analysisError,
    setLoading,
    setError: setStoreError,
  } = useAnalysisStore();
  const navigate = useNavigate();

  // 输入状态
  const [stockCode, setStockCode] = useState('');
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [inputError, setInputError] = useState<string>();

// 历史列表状态
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 20;

  // 报告详情状态
  const [selectedReport, setSelectedReport] = useState<AnalysisReport | null>(null);
  const [isLoadingReport, setIsLoadingReport] = useState(false);

  // 任务队列状态
  const [activeTasks, setActiveTasks] = useState<TaskInfo[]>([]);
  const [duplicateError, setDuplicateError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // 用于跟踪当前分析请求，避免竞态条件
  const analysisRequestIdRef = useRef<number>(0);

  // 更新任务列表中的任务
  const updateTask = useCallback((updatedTask: TaskInfo) => {
    setActiveTasks((prev) => {
      const index = prev.findIndex((t) => t.taskId === updatedTask.taskId);
      if (index >= 0) {
        const newTasks = [...prev];
        newTasks[index] = updatedTask;
        return newTasks;
      }
      return prev;
    });
  }, []);

  // 移除已完成/失败的任务
  const removeTask = useCallback((taskId: string) => {
    setActiveTasks((prev) => prev.filter((t) => t.taskId !== taskId));
  }, []);

  // SSE 任务流
  useTaskStream({
    onTaskCreated: (task) => {
      setActiveTasks((prev) => {
        // 避免重复添加
        if (prev.some((t) => t.taskId === task.taskId)) return prev;
        return [...prev, task];
      });
    },
    onTaskStarted: updateTask,
    onTaskCompleted: (task) => {
      // 刷新历史列表
      fetchHistory();
      // 延迟移除任务，让用户看到完成状态
      setTimeout(() => removeTask(task.taskId), 2000);
    },
    onTaskFailed: (task) => {
      updateTask(task);
      // 显示错误提示
      setStoreError(getParsedApiError(task.error || '分析失败'));
      // 延迟移除任务
      setTimeout(() => removeTask(task.taskId), 5000);
    },
    onError: () => {
      console.warn('SSE 连接断开，正在重连...');
    },
    enabled: true,
  });

// 用 ref 追踪易变状态，避免 fetchHistory 频繁重建导致 effect 循环
  const currentPageRef = useRef(currentPage);
  currentPageRef.current = currentPage;
  const historyItemsRef = useRef(historyItems);
  historyItemsRef.current = historyItems;
  const selectedReportRef = useRef(selectedReport);
  selectedReportRef.current = selectedReport;

  // 加载历史列表
  const fetchHistory = useCallback(async (autoSelectFirst = false, reset = true, silent = false) => {
    if (!silent) {
      if (reset) {
        setIsLoadingHistory(true);
        setCurrentPage(1);
      } else {
        setIsLoadingMore(true);
      }
    }

    // page is always 1 when reset=true, regardless of currentPageRef; the ref
    // is only used for load-more (reset=false) to get the next page number.
    const page = reset ? 1 : currentPageRef.current + 1;

    try {
      const response = await historyApi.getList({
        startDate: getRecentStartDate(30),
        endDate: getTodayInShanghai(),
        page,
        limit: pageSize,
      });

      if (silent && reset) {
        // 后台刷新：合并新增项到列表顶部，保留已加载的分页数据和滚动位置
        setHistoryItems(prev => {
          const existingIds = new Set(prev.map(item => item.id));
          const newItems = response.items.filter(item => !existingIds.has(item.id));
          return newItems.length > 0 ? [...newItems, ...prev] : prev;
        });
      } else if (reset) {
        setHistoryItems(response.items);
        setCurrentPage(1);
      } else {
        setHistoryItems(prev => [...prev, ...response.items]);
        setCurrentPage(page);
      }

      // 判断是否还有更多数据
      if (!silent) {
        const totalLoaded = reset ? response.items.length : historyItemsRef.current.length + response.items.length;
        setHasMore(totalLoaded < response.total);
      }

      // 如果需要自动选择第一条，且有数据，且当前没有选中报告
      if (autoSelectFirst && response.items.length > 0 && !selectedReportRef.current) {
        const firstItem = response.items[0];
        setIsLoadingReport(true);
        try {
          const report = await historyApi.getDetail(firstItem.id);
          setStoreError(null);
          setSelectedReport(report);
        } catch (err) {
          console.error('Failed to fetch first report:', err);
          setStoreError(getParsedApiError(err));
        } finally {
          setIsLoadingReport(false);
        }
      }
    } catch (err) {
      console.error('Failed to fetch history:', err);
      setStoreError(getParsedApiError(err));
    } finally {
      setIsLoadingHistory(false);
      setIsLoadingMore(false);
    }
  }, [pageSize, setStoreError]);

  // 加载更多历史记录
  const handleLoadMore = useCallback(() => {
    if (!isLoadingMore && hasMore) {
      fetchHistory(false, false);
    }
  }, [fetchHistory, isLoadingMore, hasMore]);

  // 初始加载 - 自动选择第一条（仅挂载时执行一次）
  useEffect(() => {
    fetchHistory(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Background polling: re-fetch history every 30s for CLI-initiated analyses
  useEffect(() => {
    const interval = setInterval(() => {
      fetchHistory(false, true, true);
    }, 30_000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Refresh when tab regains visibility (e.g. user ran main.py in another terminal)
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        fetchHistory(false, true, true);
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 点击历史项加载报告
  const handleHistoryClick = async (recordId: number) => {
    // Increment request ID to cancel any in-flight auto-select result.
    const requestId = ++analysisRequestIdRef.current;

    // Keep the current report visible while
    // the new one loads so the right panel doesn't flash a blank spinner on
    // every click. isLoadingReport is only used for the initial empty state.
    try {
      const report = await historyApi.getDetail(recordId);
      // Ignore result if a newer click has already been issued.
      if (requestId === analysisRequestIdRef.current) {
        setStoreError(null);
        setSelectedReport(report);
      }
    } catch (err) {
      console.error('Failed to fetch report:', err);
      setStoreError(getParsedApiError(err));
    }
  };

  // 分析股票（异步模式）
  const handleAnalyze = async () => {
    const { valid, message, normalized } = validateStockCode(stockCode);
    if (!valid) {
      setInputError(message);
      return;
    }

    setInputError(undefined);
    setDuplicateError(null);
    setIsAnalyzing(true);
    setLoading(true);
    setStoreError(null);

    // 记录当前请求的 ID
    const currentRequestId = ++analysisRequestIdRef.current;

    try {
      // 使用异步模式提交分析
      const response = await analysisApi.analyzeAsync({
        stockCode: normalized,
        reportType: 'detailed',
      });

      // 清空输入框
      if (currentRequestId === analysisRequestIdRef.current) {
        setStockCode('');
      }

      // 任务已提交，SSE 会推送更新
      console.log('Task submitted:', response.taskId);
    } catch (err) {
      console.error('Analysis failed:', err);
      if (currentRequestId === analysisRequestIdRef.current) {
        if (err instanceof DuplicateTaskError) {
          // 显示重复任务错误
          setDuplicateError(`股票 ${err.stockCode} 正在分析中，请等待完成`);
        } else {
          setStoreError(getParsedApiError(err));
        }
      }
    } finally {
      setIsAnalyzing(false);
      setLoading(false);
    }
  };

  // 回车提交
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && stockCode && !isAnalyzing) {
      handleAnalyze();
    }
  };

  const sidebarContent = (
    <div className="flex flex-col gap-3 overflow-hidden min-h-0 h-full">
      <TaskPanel tasks={activeTasks} />
      <HistoryList
        items={historyItems}
        isLoading={isLoadingHistory}
        isLoadingMore={isLoadingMore}
        hasMore={hasMore}
        selectedId={selectedReport?.meta.id}
        onItemClick={(id) => { handleHistoryClick(id); setSidebarOpen(false); }}
        onLoadMore={handleLoadMore}
        className="max-h-[62vh] md:max-h-[62vh] flex-1 overflow-hidden"
      />
    </div>
  );

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-6 py-6 flex flex-col min-h-screen">
        {/* Hero 标题区 */}
        <div className="text-center mb-6 flex-shrink-0">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-cyan/15 to-blue-500/10 mb-3 shadow-sm">
            <svg className="w-7 h-7 text-cyan" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
          </div>
          <h1 className="text-2xl font-bold text-primary mb-1.5 tracking-tight">个股分析</h1>
          <p className="text-sm text-secondary max-w-md mx-auto">输入股票代码获取 AI 智能分析报告，支持 A股、港股、美股</p>
        </div>

        {/* 主内容区：左侧边栏 + 右侧报告 */}
        <div className="flex-1 flex gap-6 min-h-0">
          {/* Desktop sidebar */}
          <div className="hidden md:flex w-64 flex-shrink-0 flex-col gap-3 overflow-hidden">
            {sidebarContent}
          </div>

          {/* Mobile sidebar overlay */}
          {sidebarOpen && (
            <div className="fixed inset-0 z-40 md:hidden" onClick={() => setSidebarOpen(false)}>
              <div className="absolute inset-0 bg-black/60" />
              <div
                className="absolute left-0 top-0 bottom-0 w-72 flex flex-col glass-card overflow-hidden border-r border-border shadow-2xl p-3"
                onClick={(e) => e.stopPropagation()}
              >
                {sidebarContent}
              </div>
            </div>
          )}

          {/* 右侧：输入框 + 报告详情 */}
          <section className="flex-1 flex flex-col overflow-hidden min-w-0">
            {/* 输入栏 + 追问按钮 */}
            <div className="mb-4 flex-shrink-0">
              <div className="flex items-center gap-3">
                <button
                  onClick={() => setSidebarOpen(true)}
                  className="md:hidden p-2 rounded-lg hover:bg-surface-hover transition-colors text-secondary hover:text-primary flex-shrink-0 border border-border"
                  title="历史记录"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                  </svg>
                </button>
                <div className="flex-1 relative min-w-0">
                  <input
                    type="text"
                    value={stockCode}
                    onChange={(e) => {
                      setStockCode(e.target.value.toUpperCase());
                      setInputError(undefined);
                    }}
                    onKeyDown={handleKeyDown}
                    placeholder="输入股票代码，如 600519、00700、AAPL"
                    disabled={isAnalyzing}
                    className={`input-terminal w-full ${inputError ? 'border-danger/50' : ''}`}
                  />
                  {inputError && (
                    <p className="absolute -bottom-5 left-0 text-xs text-danger">{inputError}</p>
                  )}
                  {duplicateError && (
                    <p className="absolute -bottom-5 left-0 text-xs text-warning">{duplicateError}</p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={handleAnalyze}
                  disabled={!stockCode || isAnalyzing}
                  className="h-[42px] px-5 rounded-lg bg-gradient-to-r from-cyan to-cyan-dim text-white font-semibold text-[13px] hover:shadow-lg transition-all whitespace-nowrap flex-shrink-0 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
                >
                  {isAnalyzing ? (
                    <>
                      <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                      </svg>
                      分析中
                    </>
                  ) : (
                    '分析'
                  )}
                </button>
                {selectedReport && (
                  <button
                    disabled={selectedReport.meta.id === undefined}
                    onClick={() => {
                      const code = selectedReport.meta.stockCode;
                      const name = selectedReport.meta.stockName;
                      const rid = selectedReport.meta.id!;
                      navigate(`/chat?stock=${encodeURIComponent(code)}&name=${encodeURIComponent(name)}&recordId=${rid}`);
                    }}
                    className="h-[42px] px-5 rounded-lg bg-purple/10 border border-purple/30 text-purple font-semibold text-[13px] hover:bg-purple/20 transition-all whitespace-nowrap flex-shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    追问
                  </button>
                )}
              </div>
            </div>

            {/* 报告内容 */}
            <div className="flex-1 overflow-y-auto overflow-x-auto min-h-0">
            {analysisError ? (
              <ApiErrorAlert
                error={analysisError}
                className="mb-3"
              />
            ) : null}
            {isLoadingReport ? (
              <div className="flex flex-col items-center justify-center h-full">
                <Spinner size="xl" />
                <p className="mt-3 text-secondary text-sm">加载报告中...</p>
              </div>
            ) : selectedReport ? (
              <div>
                <ReportSummary data={selectedReport} isHistory />
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center h-full text-center">
                <div className="w-12 h-12 mb-3 rounded-xl bg-elevated flex items-center justify-center">
                  <svg className="w-6 h-6 text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                  </svg>
                </div>
                <h3 className="text-base font-medium text-primary mb-1.5">开始分析</h3>
                <p className="text-xs text-muted max-w-xs">
                  输入股票代码进行分析，或从左侧选择历史报告查看
                </p>
              </div>
            )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
};

export default HomePage;
