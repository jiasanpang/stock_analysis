import React from 'react';

interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  label?: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * 下拉选择器组件
 * 科技感样式
 */
export const Select: React.FC<SelectProps> = ({
  value,
  onChange,
  options,
  label,
  placeholder = '请选择',
  disabled = false,
  className = '',
}) => {
  return (
    <div className={`flex flex-col ${className}`}>
      {label && (
        <label className="mb-2 text-sm font-medium text-secondary">
          {label}
        </label>
      )}
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          className={`
            w-full appearance-none px-4 py-2.5 pr-10 rounded-lg
            bg-elevated border border-border
            text-primary placeholder-muted
            focus:outline-none focus:ring-2 focus:ring-cyan/20 focus:border-border-accent
            hover:border-border-accent
            disabled:opacity-50 disabled:cursor-not-allowed
            transition-all duration-200
            cursor-pointer
          `}
        >
          {placeholder && (
            <option value="" disabled>
              {placeholder}
            </option>
          )}
          {options.map((option) => (
            <option key={option.value} value={option.value} className="bg-card">
              {option.label}
            </option>
          ))}
        </select>

        {/* 下拉箭头 */}
        <div className="absolute inset-y-0 right-0 flex items-center pr-3 pointer-events-none">
          <svg
            className="w-4 h-4 text-cyan"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </div>
    </div>
  );
};
