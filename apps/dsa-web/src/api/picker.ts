import apiClient from './index';

export interface ScreenStats {
  total_stocks: number;
  after_basic_filter: number;
  after_momentum_filter: number;
  after_volume_filter: number;
  final_pool: number;
}

export interface ScreenedStock {
  code: string;
  name: string;
  price: number;
  change_pct: number;
  volume_ratio: number;
  turnover_rate: number;
  pe: number;
  pb: number;
  market_cap_yi: number;
  amount_yi: number;
  change_pct_60d: number;
  score: number;
  strategies?: string[];
}

export interface StockPick {
  code: string;
  name: string;
  sector: string;
  reason: string;
  catalyst: string;
  attention: 'high' | 'medium' | 'low';
  risk_note: string;
}

export interface PickerResponse {
  success: boolean;
  market_summary: string;
  picks: StockPick[];
  sectors_to_watch: string[];
  risk_warning: string;
  screen_stats: ScreenStats | null;
  screened_pool: ScreenedStock[];
  screened_pool_by_strategy?: Record<string, ScreenedStock[]>;
  generated_at: string;
  elapsed_seconds: number;
  error: string;
  history_id?: number | null;
  picker_mode?: string;
  picker_strategies?: string[];
  picker_leader_bias_exempt_pct?: number | null;
}

export interface PickPreview {
  code: string;
  name: string;
  attention: string;
}

export interface PickerHistoryItem {
  id: number;
  market_summary: string;
  pick_count: number;
  picks_preview: PickPreview[];
  sectors_to_watch: string[];
  elapsed_seconds: number;
  created_at: string | null;
  picker_mode?: string;
  picker_strategies?: string[];
  picker_leader_bias_exempt_pct?: number | null;
}

export interface PickerHistoryListResponse {
  items: PickerHistoryItem[];
  total: number;
}

// Picker runs screening + news + LLM; backend default 300s, client waits longer
const PICKER_REQUEST_TIMEOUT_MS = 600_000; // 10 min

export type PickerMode = 'defensive' | 'balanced' | 'offensive';

export type PickerStrategy = 'buy_pullback' | 'breakout' | 'bottom_reversal' | 'macd_golden_cross';

export interface PickerRecommendParams {
  picker_strategies?: PickerStrategy[];
  picker_mode?: PickerMode;
  picker_leader_bias_exempt_pct?: number;
}

const STRATEGY_LABELS: Record<string, string> = {
  buy_pullback: '买回踩',
  breakout: '突破',
  bottom_reversal: '底部反转',
  macd_golden_cross: 'MACD金叉',
};

export { STRATEGY_LABELS };

export async function fetchRecommendations(params?: PickerRecommendParams): Promise<PickerResponse> {
  const hasParams = params && (
    (params.picker_strategies && params.picker_strategies.length > 0) ||
    params.picker_mode ||
    params.picker_leader_bias_exempt_pct != null
  );
  const body = hasParams
    ? {
        picker_strategies: params?.picker_strategies ?? undefined,
        picker_mode: params?.picker_mode ?? undefined,
        picker_leader_bias_exempt_pct: params?.picker_leader_bias_exempt_pct ?? undefined,
      }
    : undefined;
  const res = await apiClient.post<PickerResponse>('/api/v1/picker/recommend', body ?? null, {
    timeout: PICKER_REQUEST_TIMEOUT_MS,
  });
  return res.data;
}

export async function fetchPickerHistory(limit = 20, offset = 0): Promise<PickerHistoryListResponse> {
  const res = await apiClient.get<PickerHistoryListResponse>('/api/v1/picker/history', {
    params: { limit, offset },
  });
  return res.data;
}

export async function fetchPickerDetail(id: number): Promise<PickerResponse> {
  const res = await apiClient.get<PickerResponse>(`/api/v1/picker/history/${id}`);
  return res.data;
}
