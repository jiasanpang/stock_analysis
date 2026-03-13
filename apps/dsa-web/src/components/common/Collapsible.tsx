import React, { useState } from 'react';

interface CollapsibleProps {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
  icon?: React.ReactNode;
  className?: string;
}

/**
 * 可折叠面板组件
 * 支持动画展开/收起
 */
export const Collapsible: React.FC<CollapsibleProps> = ({
  title,
  children,
  defaultOpen = false,
  icon,
  className = '',
}) => {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div
      className={`
        rounded-xl overflow-hidden
        bg-card
        border border-border hover:border-border-accent
        transition-all duration-300
        ${className}
      `}
    >
      {/* 标题栏 */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-4 py-3 text-left
          hover:bg-surface-hover transition-colors"
      >
        <div className="flex items-center gap-3">
          {icon && <span className="text-cyan">{icon}</span>}
          <span className="font-medium text-primary">{title}</span>
        </div>
        <svg
          className={`w-5 h-5 text-muted transition-transform duration-300 ${
            isOpen ? 'rotate-180' : ''
          }`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* 内容区 */}
      <div
        className={`
          overflow-hidden transition-all duration-300 ease-in-out
          ${isOpen ? 'max-h-[2000px] opacity-100' : 'max-h-0 opacity-0'}
        `}
      >
        <div className="px-4 pb-4 pt-2 border-t border-border">
          {children}
        </div>
      </div>
    </div>
  );
};
