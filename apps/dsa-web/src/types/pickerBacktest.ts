/**
 * Picker backtest API types
 * Mirrors api/v1/schemas/picker_backtest.py
 */

export type PickerMode = 'defensive' | 'balanced' | 'offensive';

export interface PickerBacktestRunRequest {
  startDate: string;
  endDate: string;
  holdDays?: number;
  topN?: number;
  pickerMode?: PickerMode;
  pickerLeaderBiasExemptPct?: number;
}

export interface PickerBacktestResultItem {
  tradeDate: string;
  code: string;
  name?: string;
  entryPrice: number;
  exitPrice?: number;
  returnPct?: number;
  outcome: string;
  score?: number;
}

export interface PickerBacktestSummary {
  startDate: string;
  endDate: string;
  holdDays: number;
  topN: number;
  totalPicks: number;
  winCount: number;
  lossCount: number;
  insufficientCount: number;
  winRatePct?: number;
  avgReturnPct?: number;
  maxDrawdownPct?: number;
  profitFactor?: number;
  alphaVsBenchmarkPct?: number;
  benchmarkAvgReturnPct?: number;
}

export interface PickerBacktestRunResponse {
  success: boolean;
  results: PickerBacktestResultItem[];
  summary: PickerBacktestSummary | null;
  tradeDatesCount: number;
}

export interface PickerBacktestHistoryItem {
  id: number;
  startDate: string;
  endDate: string;
  holdDays: number;
  topN: number;
  pickerMode: string;
  tradeDatesCount: number;
  winRatePct?: number;
  avgReturnPct?: number;
  createdAt?: string;
}
