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
  generated_at: string;
  elapsed_seconds: number;
  error: string;
  history_id?: number | null;
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
}

export interface PickerHistoryListResponse {
  items: PickerHistoryItem[];
  total: number;
}

export async function fetchRecommendations(): Promise<PickerResponse> {
  const res = await apiClient.post<PickerResponse>('/api/v1/picker/recommend', null, {
    timeout: 180_000,
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
