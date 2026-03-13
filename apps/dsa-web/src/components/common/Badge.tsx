import React from 'react';

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'history';

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  size?: 'sm' | 'md';
  glow?: boolean;
  className?: string;
}

const variantStyles: Record<BadgeVariant, string> = {
  default: 'bg-slate-100 text-slate-600 border-slate-200',
  success: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  warning: 'bg-amber-50 text-amber-700 border-amber-200',
  danger: 'bg-red-50 text-red-700 border-red-200',
  info: 'bg-blue-50 text-blue-700 border-blue-200',
  history: 'bg-purple-50 text-purple-700 border-purple-200',
};

const glowStyles: Record<BadgeVariant, string> = {
  default: '',
  success: 'shadow-emerald-500/20',
  warning: 'shadow-amber-500/20',
  danger: 'shadow-red-500/20',
  info: 'shadow-cyan-500/20',
  history: 'shadow-purple-500/20',
};

/**
 * 标签徽章组件
 * 支持多种变体和发光效果
 */
export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = 'default',
  size = 'sm',
  glow = false,
  className = '',
}) => {
  const sizeStyles = size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-3 py-1 text-sm';

  return (
    <span
      className={`
        inline-flex items-center gap-1 rounded-full font-medium
        border backdrop-blur-sm
        ${sizeStyles}
        ${variantStyles[variant]}
        ${glow ? `shadow-lg ${glowStyles[variant]}` : ''}
        ${className}
      `}
    >
      {children}
    </span>
  );
};
