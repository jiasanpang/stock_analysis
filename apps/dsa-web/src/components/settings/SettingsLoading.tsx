import type React from 'react';

export const SettingsLoading: React.FC = () => {
  return (
    <div className="space-y-4 animate-fade-in">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="rounded-xl border border-border bg-elevated/60 p-4">
          <div className="h-3 w-32 rounded bg-black/5" />
          <div className="mt-3 h-10 rounded-lg bg-black/4" />
        </div>
      ))}
    </div>
  );
};
