import type React from 'react';
import type { ParsedApiError } from '../../api/error';

interface ApiErrorAlertProps {
  error: ParsedApiError;
  className?: string;
  actionLabel?: string;
  onAction?: () => void;
}

export const ApiErrorAlert: React.FC<ApiErrorAlertProps> = ({
  error,
  className = '',
  actionLabel,
  onAction,
}) => {
  const showDetails = error.rawMessage.trim() && error.rawMessage.trim() !== error.message.trim();

  return (
    <div
      className={`rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-red-700 ${className}`}
      role="alert"
    >
      <p className="text-sm font-semibold">{error.title}</p>
      <p className="mt-1 text-xs opacity-90">{error.message}</p>
      {showDetails ? (
        <details className="mt-3 rounded-lg border border-red-200 bg-red-50/50 px-3 py-2">
          <summary className="cursor-pointer text-xs text-red-600">查看详情</summary>
          <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] leading-5 text-red-600/85">
            {error.rawMessage}
          </pre>
        </details>
      ) : null}
      {actionLabel && onAction ? (
        <button type="button" className="mt-3 btn-secondary !px-3 !py-1.5 !text-xs" onClick={onAction}>
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
};
