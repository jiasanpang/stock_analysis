import React from 'react';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'outline' | 'ghost' | 'gradient' | 'danger';
  size?: 'sm' | 'md' | 'lg';
  isLoading?: boolean;
  glow?: boolean;
}

/**
 * 按钮组件
 * 支持多种变体和科技感样式
 */
export const Button: React.FC<ButtonProps> = ({
  children,
  variant = 'primary',
  size = 'md',
  isLoading = false,
  glow = false,
  className = '',
  disabled,
  ...props
}) => {
  const baseStyle = `
    inline-flex items-center justify-center
    font-medium rounded-lg
    transition-all duration-200
    focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-base
    disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none
  `;

  const sizeStyles = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2.5 text-sm',
    lg: 'px-6 py-3 text-base',
  };

  const variantStyles = {
    primary: `
      bg-cyan text-white
      hover:bg-cyan/90
      focus:ring-cyan
      shadow-lg shadow-glow-cyan
    `,
    secondary: `
      bg-elevated text-secondary
      hover:bg-surface-hover
      focus:ring-slate-400
      border border-border
    `,
    outline: `
      bg-transparent text-cyan
      border border-border-accent
      hover:bg-cyan/5 hover:border-cyan
      focus:ring-cyan
    `,
    ghost: `
      bg-transparent text-secondary
      hover:bg-surface-hover hover:text-primary
      focus:ring-gray-400
    `,
    gradient: `
      bg-gradient-to-r from-cyan to-cyan-dim text-white
      hover:from-cyan/90 hover:to-cyan-dim/90
      focus:ring-cyan
      shadow-lg shadow-glow-cyan
    `,
    danger: `
      bg-red-600 text-white
      hover:bg-red-500
      focus:ring-red-500
      shadow-lg shadow-red-500/25
    `,
  };

  const glowStyles = glow
    ? 'shadow-glow-cyan hover:shadow-[0_0_30px_rgba(6,182,212,0.4)]'
    : '';

  return (
    <button
      className={`
        ${baseStyle}
        ${sizeStyles[size]}
        ${variantStyles[variant]}
        ${glowStyles}
        ${className}
      `}
      disabled={disabled || isLoading}
      {...props}
    >
      {isLoading ? (
        <span className="flex items-center justify-center">
          <svg
            className="animate-spin -ml-1 mr-2 h-4 w-4 text-current"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
            />
          </svg>
          处理中...
        </span>
      ) : (
        children
      )}
    </button>
  );
};
