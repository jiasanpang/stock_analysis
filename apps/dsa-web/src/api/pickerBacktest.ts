import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  PickerBacktestRunRequest,
  PickerBacktestRunResponse,
  PickerBacktestResultItem,
  PickerBacktestSummary,
  PickerBacktestHistoryItem,
} from '../types/pickerBacktest';

// Picker backtest: ~6 Tushare calls/day + forward returns. 10 days ≈ 100+ calls, rate-limited.
const PICKER_BACKTEST_TIMEOUT_MS = 900000; // 15 min

export const pickerBacktestApi = {
  run: async (params: PickerBacktestRunRequest): Promise<PickerBacktestRunResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/picker-backtest/run',
      {
        start_date: params.startDate,
        end_date: params.endDate,
        hold_days: params.holdDays,
        top_n: params.topN,
        picker_strategies: params.pickerStrategies,
      },
      { timeout: PICKER_BACKTEST_TIMEOUT_MS },
    );
    return toCamelCase<PickerBacktestRunResponse>(response.data);
  },

  getPerformance: async (): Promise<PickerBacktestSummary | null> => {
    try {
      const response = await apiClient.get<Record<string, unknown>>(
        '/api/v1/picker-backtest/performance',
      );
      return toCamelCase<PickerBacktestSummary>(response.data);
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { status?: number } };
        if (axiosErr.response?.status === 404) return null;
      }
      throw err;
    }
  },

  getResults: async (): Promise<{ results: PickerBacktestResultItem[]; summary: PickerBacktestSummary | null }> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/picker-backtest/results',
    );
    const data = toCamelCase<{ results: Record<string, unknown>[]; summary: Record<string, unknown> | null }>(
      response.data,
    );
    return {
      results: (data.results || []).map((r) => toCamelCase<PickerBacktestResultItem>(r)),
      summary: data.summary ? toCamelCase<PickerBacktestSummary>(data.summary) : null,
    };
  },

  getHistory: async (params?: { limit?: number; offset?: number }): Promise<{ items: PickerBacktestHistoryItem[]; total: number }> => {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/picker-backtest/history',
      { params: { limit: params?.limit ?? 20, offset: params?.offset ?? 0 } },
    );
    const data = toCamelCase<{ items: Record<string, unknown>[]; total: number }>(response.data);
    return {
      items: (data.items || []).map((r) => toCamelCase<PickerBacktestHistoryItem>(r)),
      total: data.total ?? 0,
    };
  },

  getHistoryDetail: async (id: number): Promise<{ results: PickerBacktestResultItem[]; summary: PickerBacktestSummary | null }> => {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/picker-backtest/history/${id}`,
    );
    const data = toCamelCase<Record<string, unknown>>(response.data);
    const rawResults = Array.isArray(data.results) ? data.results : [];
    return {
      results: rawResults.map((r: Record<string, unknown>) => toCamelCase<PickerBacktestResultItem>(r)),
      summary: data.summary ? toCamelCase<PickerBacktestSummary>(data.summary) : null,
    };
  },
};
