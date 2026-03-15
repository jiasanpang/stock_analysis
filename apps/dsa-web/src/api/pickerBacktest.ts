import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  PickerBacktestRunRequest,
  PickerBacktestRunResponse,
  PickerBacktestResultItem,
  PickerBacktestSummary,
} from '../types/pickerBacktest';

// Picker backtest can take 5–10+ min (many Tushare API calls)
const PICKER_BACKTEST_TIMEOUT_MS = 600000; // 10 min

export const pickerBacktestApi = {
  run: async (params: PickerBacktestRunRequest): Promise<PickerBacktestRunResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/picker-backtest/run',
      {
        start_date: params.startDate,
        end_date: params.endDate,
        hold_days: params.holdDays,
        top_n: params.topN,
        picker_mode: params.pickerMode,
        picker_leader_bias_exempt_pct: params.pickerLeaderBiasExemptPct,
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
};
