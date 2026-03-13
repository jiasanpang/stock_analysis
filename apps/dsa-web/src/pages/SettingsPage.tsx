import type React from 'react';
import { useEffect } from 'react';
import { useAuth, useSystemConfig } from '../hooks';
import { ApiErrorAlert } from '../components/common';
import {
  ChangePasswordCard,
  IntelligentImport,
  LLMChannelEditor,
  SettingsAlert,
  SettingsField,
  SettingsLoading,
} from '../components/settings';
import { getCategoryTitleZh } from '../utils/systemConfigI18n';

const SettingsPage: React.FC = () => {
  const { passwordChangeable } = useAuth();
  const {
    categories,
    itemsByCategory,
    issueByKey,
    activeCategory,
    setActiveCategory,
    hasDirty,
    dirtyCount,
    toast,
    clearToast,
    isLoading,
    isSaving,
    loadError,
    saveError,
    retryAction,
    load,
    retry,
    save,
    setDraftValue,
    configVersion,
    maskToken,
  } = useSystemConfig();

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!toast) {
      return;
    }

    const timer = window.setTimeout(() => {
      clearToast();
    }, 3200);

    return () => {
      window.clearTimeout(timer);
    };
  }, [clearToast, toast]);

  const rawActiveItems = itemsByCategory[activeCategory] || [];

  // Hide per-channel LLM_*_ env vars from the normal field list;
  // they are managed by the LLMChannelEditor component instead.
  const LLM_CHANNEL_KEY_RE = /^LLM_[A-Z0-9]+_(BASE_URL|API_KEY|API_KEYS|MODELS|EXTRA_HEADERS)$/;
  const activeItems =
    activeCategory === 'ai_model'
      ? rawActiveItems.filter((item) => !LLM_CHANNEL_KEY_RE.test(item.key))
      : rawActiveItems;

  return (
    <div className="min-h-screen px-6 py-6 max-w-6xl mx-auto">
      {/* Hero 标题区 */}
      <div className="text-center mb-6">
        <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-purple/15 to-indigo-500/10 mb-3 shadow-sm">
          <svg className="w-7 h-7 text-purple" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </div>
        <h1 className="text-2xl font-bold text-primary mb-1.5 tracking-tight">系统设置</h1>
        <p className="text-sm text-secondary max-w-md mx-auto">配置 AI 模型、数据源、通知渠道等系统参数</p>
      </div>

      {/* 操作按钮 */}
      <div className="flex justify-center gap-3 mb-6">
        <button type="button" className="btn-secondary text-sm" onClick={() => void load()} disabled={isLoading || isSaving}>
          重置
        </button>
        <button
          type="button"
          className="btn-primary text-sm"
          onClick={() => void save()}
          disabled={!hasDirty || isSaving || isLoading}
        >
          {isSaving ? '保存中...' : `保存配置${dirtyCount ? ` (${dirtyCount})` : ''}`}
        </button>
      </div>

      {saveError ? (
        <ApiErrorAlert
          className="mb-4 max-w-2xl mx-auto"
          error={saveError}
          actionLabel={retryAction === 'save' ? '重试保存' : undefined}
          onAction={retryAction === 'save' ? () => void retry() : undefined}
        />
      ) : null}

      {loadError ? (
        <ApiErrorAlert
          error={loadError}
          actionLabel={retryAction === 'load' ? '重试加载' : '重新加载'}
          onAction={() => void retry()}
          className="mb-4 max-w-2xl mx-auto"
        />
      ) : null}

      {isLoading ? (
        <SettingsLoading />
      ) : (
        <>
          {/* 水平分类 Tabs */}
          <div className="mb-6 overflow-x-auto">
            <div className="flex gap-2 justify-center flex-wrap">
              {categories.map((category) => {
                const isActive = category.category === activeCategory;
                const title = getCategoryTitleZh(category.category, category.title);

                return (
                  <button
                    key={category.category}
                    type="button"
                    className={`px-4 py-2 rounded-lg text-sm font-medium transition whitespace-nowrap ${
                      isActive
                        ? 'bg-cyan text-white shadow-sm'
                        : 'bg-elevated/60 text-secondary hover:bg-surface-hover hover:text-primary border border-border/50'
                    }`}
                    onClick={() => setActiveCategory(category.category)}
                  >
                    {title}
                  </button>
                );
              })}
            </div>
          </div>

          {/* 配置内容 */}
          <section className="space-y-3 rounded-2xl border border-border bg-card/60 p-5 backdrop-blur-sm">
            {activeCategory === 'base' ? (
              <div className="space-y-3">
                <IntelligentImport
                  stockListValue={
                    (activeItems.find((i) => i.key === 'STOCK_LIST')?.value as string) ?? ''
                  }
                  configVersion={configVersion}
                  maskToken={maskToken}
                  onMerged={() => void load()}
                  disabled={isSaving || isLoading}
                />
              </div>
            ) : null}
            {activeCategory === 'ai_model' ? (
              <LLMChannelEditor
                items={rawActiveItems}
                configVersion={configVersion}
                maskToken={maskToken}
                onSaved={() => void load()}
                disabled={isSaving || isLoading}
              />
            ) : null}
            {activeCategory === 'system' && passwordChangeable ? (
              <div className="space-y-3">
                <ChangePasswordCard />
              </div>
            ) : null}
            {activeItems.length ? (
              activeItems.map((item) => (
                <SettingsField
                  key={item.key}
                  item={item}
                  value={item.value}
                  disabled={isSaving}
                  onChange={setDraftValue}
                  issues={issueByKey[item.key] || []}
                />
              ))
            ) : (
              <div className="rounded-xl border border-border bg-elevated/40 p-5 text-sm text-secondary">
                当前分类下暂无配置项。
              </div>
            )}
          </section>
        </>
      )}

      {toast ? (
        <div className="fixed bottom-5 right-5 z-50 w-[320px] max-w-[calc(100vw-24px)]">
          {toast.type === 'success'
            ? <SettingsAlert title="操作成功" message={toast.message} variant="success" />
            : <ApiErrorAlert error={toast.error} />}
        </div>
      ) : null}
    </div>
  );
};

export default SettingsPage;
