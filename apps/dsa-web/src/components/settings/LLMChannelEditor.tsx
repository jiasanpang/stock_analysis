import { useState, useMemo, useCallback } from 'react';
import type React from 'react';
import type { ParsedApiError } from '../../api/error';
import { getParsedApiError } from '../../api/error';
import { ApiErrorAlert, EyeToggleIcon } from '../common';
import { systemConfigApi } from '../../api/systemConfig';

/** Well-known channel presets for quick-add dropdown. */
const CHANNEL_PRESETS: Record<string, { label: string; baseUrl: string; placeholder: string }> = {
  aihubmix: {
    label: 'AIHubmix（聚合平台）',
    baseUrl: 'https://aihubmix.com/v1',
    placeholder: 'gpt-4o-mini,claude-3-5-sonnet,qwen-plus',
  },
  deepseek: {
    label: 'DeepSeek 官方',
    baseUrl: 'https://api.deepseek.com/v1',
    placeholder: 'deepseek-chat,deepseek-reasoner',
  },
  dashscope: {
    label: '通义千问（Dashscope）',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    placeholder: 'qwen-plus,qwen-turbo',
  },
  zhipu: {
    label: '智谱 GLM',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    placeholder: 'glm-4-flash,glm-4-plus',
  },
  moonshot: {
    label: 'Moonshot（月之暗面）',
    baseUrl: 'https://api.moonshot.cn/v1',
    placeholder: 'moonshot-v1-8k',
  },
  siliconflow: {
    label: '硅基流动（SiliconFlow）',
    baseUrl: 'https://api.siliconflow.cn/v1',
    placeholder: 'deepseek-ai/DeepSeek-V3',
  },
  openrouter: {
    label: 'OpenRouter',
    baseUrl: 'https://openrouter.ai/api/v1',
    placeholder: 'gpt-4o,claude-3.5-sonnet',
  },
  gemini: {
    label: 'Gemini（原生，无需 base_url）',
    baseUrl: '',
    placeholder: 'gemini/gemini-2.5-flash',
  },
  custom: {
    label: '自定义渠道',
    baseUrl: '',
    placeholder: 'model-name-1,model-name-2',
  },
};

interface ChannelConfig {
  /** Channel identifier (used in env var prefix). */
  name: string;
  baseUrl: string;
  apiKey: string;
  models: string;
}

interface LLMChannelEditorProps {
  /** All config items from the server (to read existing channel vars). */
  items: Array<{ key: string; value: string }>;
  /** Current config version for API calls. */
  configVersion: string;
  /** Mask token for secrets. */
  maskToken: string;
  /** Called after successful save to reload config. */
  onSaved: () => void;
  /** Disable interactions while parent is busy. */
  disabled?: boolean;
}

/** Extract `LLM_{NAME}_*` env vars from items and group them by channel. */
function parseChannelsFromItems(items: Array<{ key: string; value: string }>): ChannelConfig[] {
  const itemMap = new Map(items.map((i) => [i.key, i.value]));
  const channelNames = (itemMap.get('LLM_CHANNELS') || '')
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);

  if (channelNames.length === 0) {
    return [];
  }

  return channelNames.map((name) => ({
    name: name.toLowerCase(),
    baseUrl: itemMap.get(`LLM_${name}_BASE_URL`) || '',
    apiKey: itemMap.get(`LLM_${name}_API_KEY`) || itemMap.get(`LLM_${name}_API_KEYS`) || '',
    models: itemMap.get(`LLM_${name}_MODELS`) || '',
  }));
}

/** Build env var update items from channel list. */
function channelsToUpdateItems(
  channels: ChannelConfig[],
  previousChannelNames: string[],
): Array<{ key: string; value: string }> {
  const updates: Array<{ key: string; value: string }> = [];
  const activeNames = channels.map((c) => c.name.toUpperCase());

  // LLM_CHANNELS
  updates.push({ key: 'LLM_CHANNELS', value: channels.map((c) => c.name).join(',') });

  // Per-channel vars
  for (const ch of channels) {
    const prefix = `LLM_${ch.name.toUpperCase()}`;
    updates.push({ key: `${prefix}_BASE_URL`, value: ch.baseUrl });
    // Use API_KEY for single key, API_KEYS for comma-separated multi-key
    const isMultiKey = ch.apiKey.includes(',');
    updates.push({ key: `${prefix}_API_KEY${isMultiKey ? 'S' : ''}`, value: ch.apiKey });
    // Clear the other key variant
    updates.push({ key: `${prefix}_API_KEY${isMultiKey ? '' : 'S'}`, value: '' });
    updates.push({ key: `${prefix}_MODELS`, value: ch.models });
  }

  // Clear removed channel vars
  for (const oldName of previousChannelNames) {
    const upper = oldName.toUpperCase();
    if (!activeNames.includes(upper)) {
      const prefix = `LLM_${upper}`;
      updates.push({ key: `${prefix}_BASE_URL`, value: '' });
      updates.push({ key: `${prefix}_API_KEY`, value: '' });
      updates.push({ key: `${prefix}_API_KEYS`, value: '' });
      updates.push({ key: `${prefix}_MODELS`, value: '' });
    }
  }

  return updates;
}

export const LLMChannelEditor: React.FC<LLMChannelEditorProps> = ({
  items,
  configVersion,
  maskToken,
  onSaved,
  disabled = false,
}) => {
  const initialChannels = useMemo(() => parseChannelsFromItems(items), [items]);
  const initialNames = useMemo(
    () => initialChannels.map((c) => c.name),
    [initialChannels],
  );

  const [channels, setChannels] = useState<ChannelConfig[]>(initialChannels);
  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<
    { type: 'success'; text: string } | { type: 'error'; error: ParsedApiError } | null
  >(null);
  const [visibleKeys, setVisibleKeys] = useState<Record<number, boolean>>({});
  const [isCollapsed, setIsCollapsed] = useState(initialChannels.length === 0);
  const [addPreset, setAddPreset] = useState('aihubmix');

  // Detect if user has unsaved channel changes
  const hasChanges = useMemo(() => {
    if (channels.length !== initialChannels.length) return true;
    return channels.some((ch, idx) => {
      const init = initialChannels[idx];
      if (!init) return true;
      return (
        ch.name !== init.name ||
        ch.baseUrl !== init.baseUrl ||
        ch.apiKey !== init.apiKey ||
        ch.models !== init.models
      );
    });
  }, [channels, initialChannels]);

  const updateChannel = useCallback((index: number, field: keyof ChannelConfig, value: string) => {
    setChannels((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value };
      return next;
    });
  }, []);

  const removeChannel = useCallback((index: number) => {
    setChannels((prev) => prev.filter((_, i) => i !== index));
    setVisibleKeys((prev) => {
      const next = { ...prev };
      delete next[index];
      return next;
    });
  }, []);

  const addChannel = useCallback(() => {
    const preset = CHANNEL_PRESETS[addPreset] || CHANNEL_PRESETS.custom;
    // Determine a unique name
    const baseName = addPreset === 'custom' ? 'custom' : addPreset;
    const existingNames = new Set(channels.map((c) => c.name));
    let name = baseName;
    let counter = 2;
    while (existingNames.has(name)) {
      name = `${baseName}${counter}`;
      counter++;
    }

    setChannels((prev) => [
      ...prev,
      { name, baseUrl: preset.baseUrl, apiKey: '', models: '' },
    ]);
    setIsCollapsed(false);
  }, [addPreset, channels]);

  const handleSave = useCallback(async () => {
    setIsSaving(true);
    setSaveMessage(null);

    try {
      const updateItems = channelsToUpdateItems(channels, initialNames);
      await systemConfigApi.update({
        configVersion,
        maskToken,
        reloadNow: true,
        items: updateItems,
      });
      setSaveMessage({ type: 'success', text: '渠道配置已保存' });
      onSaved();
    } catch (error: unknown) {
      setSaveMessage({ type: 'error', error: getParsedApiError(error) });
    } finally {
      setIsSaving(false);
    }
  }, [channels, configVersion, initialNames, maskToken, onSaved]);

  const toggleKeyVisibility = useCallback((index: number) => {
    setVisibleKeys((prev) => ({ ...prev, [index]: !prev[index] }));
  }, []);

  const busy = disabled || isSaving;

  return (
    <div className="rounded-xl border border-cyan/20 bg-elevated/50 p-4">
      <button
        type="button"
        className="flex w-full items-center justify-between text-left"
        onClick={() => setIsCollapsed((prev) => !prev)}
      >
        <div>
          <h3 className="text-sm font-semibold text-primary">LLM 渠道配置</h3>
          <p className="mt-0.5 text-xs text-muted">
            {channels.length > 0
              ? `已配置 ${channels.length} 个渠道：${channels.map((c) => c.name).join('、')}`
              : '同时使用多个模型平台时启用；只用单个模型可跳过此项'}
          </p>
        </div>
        <span className="text-xs text-muted">{isCollapsed ? '▶ 展开' : '▼ 收起'}</span>
      </button>

      {!isCollapsed && (
        <div className="mt-4 space-y-3">
          {channels.map((channel, index) => (
            <div
              key={`${channel.name}-${index}`}
              className="rounded-lg border border-border bg-card/40 p-3 space-y-2"
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-accent">
                    {CHANNEL_PRESETS[channel.name]?.label || channel.name}
                  </span>
                </div>
                <button
                  type="button"
                  className="text-xs text-red-400 hover:text-red-300 disabled:opacity-40"
                  disabled={busy}
                  onClick={() => removeChannel(index)}
                >
                  删除
                </button>
              </div>

              {/* Channel name */}
              <div>
                <label className="mb-1 block text-xs text-secondary">渠道名称</label>
                <input
                  type="text"
                  className="input-terminal w-full"
                  value={channel.name}
                  disabled={busy}
                  onChange={(e) => updateChannel(index, 'name', e.target.value.replace(/[^a-zA-Z0-9_]/g, '').toLowerCase())}
                  placeholder="如 aihubmix、deepseek"
                />
              </div>

              {/* Base URL */}
              <div>
                <label className="mb-1 block text-xs text-secondary">API 地址（Base URL）</label>
                <input
                  type="text"
                  className="input-terminal w-full"
                  value={channel.baseUrl}
                  disabled={busy}
                  onChange={(e) => updateChannel(index, 'baseUrl', e.target.value)}
                  placeholder="https://api.example.com/v1（Gemini 原生可留空）"
                />
              </div>

              {/* API Key */}
              <div>
                <label className="mb-1 block text-xs text-secondary">API Key（多个用逗号分隔）</label>
                <div className="flex items-center gap-2">
                  <input
                    type={visibleKeys[index] ? 'text' : 'password'}
                    className="input-terminal flex-1"
                    value={channel.apiKey}
                    disabled={busy}
                    onChange={(e) => updateChannel(index, 'apiKey', e.target.value)}
                    placeholder="sk-xxxxxxxxxxxxxxxx"
                  />
                  <button
                    type="button"
                    className="btn-secondary !p-2"
                    onClick={() => toggleKeyVisibility(index)}
                    title={visibleKeys[index] ? '隐藏' : '显示'}
                  >
                    <EyeToggleIcon visible={!!visibleKeys[index]} />
                  </button>
                </div>
              </div>

              {/* Models */}
              <div>
                <label className="mb-1 block text-xs text-secondary">模型列表（逗号分隔）</label>
                <input
                  type="text"
                  className="input-terminal w-full"
                  value={channel.models}
                  disabled={busy}
                  onChange={(e) => updateChannel(index, 'models', e.target.value)}
                  placeholder={CHANNEL_PRESETS[channel.name]?.placeholder || 'model-1,model-2'}
                />
                <p className="mt-1 text-[11px] text-muted">
                  有 Base URL 的渠道无需加 openai/ 前缀，系统自动补全
                </p>
              </div>
            </div>
          ))}

          {/* Add channel */}
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="input-terminal text-xs"
              value={addPreset}
              disabled={busy}
              onChange={(e) => setAddPreset(e.target.value)}
            >
              {Object.entries(CHANNEL_PRESETS).map(([key, preset]) => (
                <option key={key} value={key}>
                  {preset.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-secondary !px-3 !py-1.5 text-xs"
              disabled={busy}
              onClick={addChannel}
            >
              + 添加渠道
            </button>
          </div>

          {/* Save */}
          {hasChanges && (
            <div className="flex items-center gap-3 border-t border-border pt-3">
              <button
                type="button"
                className="btn-primary !px-4 !py-1.5 text-xs"
                disabled={busy}
                onClick={() => void handleSave()}
              >
                {isSaving ? '保存中...' : '保存渠道'}
              </button>
              <button
                type="button"
                className="btn-secondary !px-3 !py-1.5 text-xs"
                disabled={busy}
                onClick={() => setChannels(initialChannels)}
              >
                撤销
              </button>
              <span className="text-[11px] text-muted">渠道配置独立保存，与下方字段互不影响</span>
            </div>
          )}

          {saveMessage && (
            saveMessage.type === 'success'
              ? <p className="text-xs text-green-400">{saveMessage.text}</p>
              : <ApiErrorAlert error={saveMessage.error} />
          )}
        </div>
      )}
    </div>
  );
};
