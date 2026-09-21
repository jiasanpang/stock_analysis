import type React from 'react';
import { Badge } from '../../components/common';

export function pct(value?: number | null): string {
  if (value == null) return '--';
  return `${value.toFixed(1)}%`;
}

export function outcomeBadge(outcome?: string): React.ReactElement {
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

export function statusBadge(status: string): React.ReactElement {
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

export function boolIcon(value?: boolean | null): React.ReactElement {
  if (value === true) return <span className="text-emerald-500">✓</span>;
  if (value === false) return <span className="text-red-500">✗</span>;
  return <span className="text-muted">--</span>;
}

export const STRATEGY_OPTIONS: { value: 'buy_pullback'; label: string }[] = [
  { value: 'buy_pullback', label: '买回踩' },
];
