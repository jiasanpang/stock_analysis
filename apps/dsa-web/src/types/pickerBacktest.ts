/**
 * Picker backtest API types
 * Mirrors api/v1/schemas/picker_backtest.py
 */

export type PickerStrategy = 'buy_pullback' | 'breakout' | 'bottom_reversal' | 'eod_buyback';

export interface PickerBacktestRunRequest {
  startDate: string;
  endDate: string;
  holdDays?: number;
  topN?: number;
  pickerStrategies?: PickerStrategy[];
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
  tradeDatesWithPicks?: number;  // days that had candidates
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
  pickerStrategies?: string[];
  tradeDatesCount: number;
  winRatePct?: number;
  avgReturnPct?: number;
  createdAt?: string;
}
