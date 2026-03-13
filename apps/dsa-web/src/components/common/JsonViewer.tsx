import React, { useState } from 'react';

interface JsonViewerProps {
  data: Record<string, unknown> | unknown[] | null | undefined;
  maxHeight?: string;
  className?: string;
}

/**
 * JSON 结构化展示组件
 * 支持语法高亮和折叠
 */
export const JsonViewer: React.FC<JsonViewerProps> = ({
  data,
  maxHeight = '400px',
  className = '',
}) => {
  const [copied, setCopied] = useState(false);

  if (!data) {
    return (
      <div className="text-muted italic py-4 text-center">暂无数据</div>
    );
  }

  const jsonString = JSON.stringify(data, null, 2);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(jsonString);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // 简单的语法高亮
  const highlightJson = (json: string): React.ReactNode => {
    return json.split('\n').map((line, index) => {
      // 高亮 key
      let highlighted = line.replace(
        /"([^"]+)":/g,
        '<span class="text-blue-600">"$1"</span>:'
      );
      // 高亮字符串值
      highlighted = highlighted.replace(
        /: "([^"]*)"/g,
        ': <span class="text-emerald-600">"$1"</span>'
      );
      // 高亮数字
      highlighted = highlighted.replace(
        /: (-?\d+\.?\d*)/g,
        ': <span class="text-amber-600">$1</span>'
      );
      // 高亮布尔值和 null
      highlighted = highlighted.replace(
        /: (true|false|null)/g,
        ': <span class="text-purple-600">$1</span>'
      );

      return (
        <div
          key={index}
          className="leading-relaxed"
          dangerouslySetInnerHTML={{ __html: highlighted }}
        />
      );
    });
  };

  return (
    <div className={`relative ${className}`}>
      {/* 复制按钮 */}
      <button
        onClick={handleCopy}
        className="absolute top-2 right-2 px-2 py-1 text-xs rounded
          bg-elevated hover:bg-surface-hover text-secondary
          transition-colors z-10"
      >
        {copied ? '已复制!' : '复制'}
      </button>

      {/* JSON 内容 */}
      <div
        className="bg-slate-50 rounded-lg p-4 overflow-auto custom-scrollbar
          border border-border font-mono text-sm text-secondary"
        style={{ maxHeight }}
      >
        <pre className="whitespace-pre-wrap break-words">
          {highlightJson(jsonString)}
        </pre>
      </div>
    </div>
  );
};
