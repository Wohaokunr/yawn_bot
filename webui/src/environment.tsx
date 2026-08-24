import {
  DeleteOutlined,
  ExperimentOutlined,
  PlusOutlined,
  RedoOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { CollapseProps } from "antd";
import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type {
  EnvironmentEntry,
  EnvironmentPatchResult,
  EnvironmentSnapshot,
  EnvironmentValueSource,
  LLMConnectionTestResult,
  LLMProviderSnapshot,
} from "./types";

const { Text } = Typography;

const PROVIDER_PANEL_KEY = "llm-providers";
const LLM_PANEL_KEY = "llm-models";
const TASK_PANEL_KEY = "task-routing";

const MODEL_CONFIGS = [
  {
    title: "默认模型",
    description: "复杂对话、KP 与狼人杀决策",
    modelKey: "AI_MODEL",
    providerKey: "AI_DEFAULT_PROVIDER",
    thinkingKey: "AI_DEFAULT_THINKING",
    multimodalKey: "AI_DEFAULT_MULTIMODAL",
  },
  {
    title: "轻量模型",
    description: "高频短文本与结构化任务；留空回退默认模型",
    modelKey: "AI_LIGHT_MODEL",
    providerKey: "AI_LIGHT_PROVIDER",
    thinkingKey: "AI_LIGHT_THINKING",
    multimodalKey: "AI_LIGHT_MULTIMODAL",
  },
  {
    title: "识图模型",
    description: "图片描述与不支持图片时的转述降级",
    modelKey: "AI_VISION_MODEL",
    providerKey: "AI_VISION_PROVIDER",
    thinkingKey: "AI_VISION_THINKING",
    multimodalKey: undefined,
  },
] as const;

/* 模型档位徽章,与 MODEL_CONFIGS 按序对应,复用 tone-* 渐变色板 */
const MODEL_BADGES = [
  { icon: "🌸", tone: "tone-sakura" },
  { icon: "🌱", tone: "tone-mint" },
  { icon: "📷", tone: "tone-sky" },
] as const;

const TASK_CONFIGS = [
  {
    plugin: "Agent",
    tasks: [
      ["普通对话 / 工具", "AGENT_DIALOGUE_LLM_PROFILE", "AGENT_DIALOGUE_THINKING"],
      ["主动发言", "AGENT_PROACTIVE_LLM_PROFILE", "AGENT_PROACTIVE_THINKING"],
      ["记忆整理", "AGENT_MEMORY_LLM_PROFILE", "AGENT_MEMORY_THINKING"],
      ["图片描述", "AGENT_IMAGE_LLM_PROFILE", "AGENT_IMAGE_THINKING"],
    ],
  },
  {
    plugin: "RPG",
    tasks: [
      ["KP 叙事 / 工具", "RPG_KP_LLM_PROFILE", "RPG_KP_THINKING"],
      ["NPC 路由", "RPG_NPC_ROUTER_LLM_PROFILE", "RPG_NPC_ROUTER_THINKING"],
      ["NPC 短对白", "RPG_NPC_LLM_PROFILE", "RPG_NPC_THINKING"],
    ],
  },
  {
    plugin: "狼人杀",
    tasks: [
      ["AI 行动决策", "WW_DECISION_LLM_PROFILE", "WW_DECISION_THINKING"],
      ["AI 短发言", "WW_SPEECH_LLM_PROFILE", "WW_SPEECH_THINKING"],
    ],
  },
] as const;

const PLUGIN_META: Record<string, { icon: string; tone: string }> = {
  Agent: { icon: "🌸", tone: "tone-lavender" },
  RPG: { icon: "🎲", tone: "tone-mint" },
  "狼人杀": { icon: "🐺", tone: "tone-sky" },
};

/* 常见分组的小图标,未知分组回落到樱花 */
const SECTION_ICONS: Record<string, string> = {
  "NoneBot 运行时": "🤖",
  "本地数据与 SQLite/ORM": "💾",
  "OneBot V11 连接": "📡",
  "Sentry 可选错误上报": "🛟",
  "OpenAI-compatible 服务": "🧠",
  "Core / Agent 管理 WebUI": "🖥️",
  "子插件 LLM 任务路由、Agent 媒体和文件工具": "🔀",
  "Core/Agent AI 开关": "🔆",
  "Agent 全局默认人设": "🎀",
  "番茄小说插件（可选）": "📚",
  "游戏插件常用覆盖": "🎮",
  "维护提示": "🔧",
  "自定义配置": "✨",
  "其他配置": "🌸",
};

const MODEL_PANEL_KEYS = MODEL_CONFIGS.flatMap((item) => [
  item.modelKey,
  item.providerKey,
  item.thinkingKey,
  ...(item.multimodalKey ? [item.multimodalKey] : []),
]);
const TASK_PANEL_KEYS = TASK_CONFIGS.flatMap((group) =>
  group.tasks.flatMap(([, profileKey, thinkingKey]) => [profileKey, thinkingKey]),
);

const LLM_CONFIG_KEYS = new Set<string>([
  ...MODEL_PANEL_KEYS,
  ...TASK_PANEL_KEYS,
  "AI_BASE_URL",
  "AI_API_KEY",
  "AI_PROVIDERS",
  "AI_PROVIDER_API_KEYS",
]);

interface LLMProviderDraft extends LLMProviderSnapshot {
  draftKey: string;
  apiKey?: string | null;
  isNew?: boolean;
}

const SOURCE_META: Record<EnvironmentValueSource, { label: string; color: string }> = {
  process: { label: "进程环境覆盖", color: "red" },
  environment: { label: "环境文件覆盖", color: "orange" },
  env: { label: "根 .env", color: "green" },
  default: { label: "默认值", color: "default" },
};

const ENUM_LABELS: Record<string, string> = {
  default: "默认模型",
  light: "轻量模型",
  vision: "识图模型",
  inherit: "继承模型档位",
  auto: "自动",
  enabled: "开启推理",
  disabled: "关闭推理",
  supported: "支持图片",
  unsupported: "不支持图片",
};

function enumLabel(item: EnvironmentEntry, value: string): string {
  if (value === "auto") {
    return item.key.endsWith("_MULTIMODAL")
      ? "auto（自动探测图片能力）"
      : "auto（不发送推理参数）";
  }
  return `${value}（${ENUM_LABELS[value] ?? value}）`;
}

export function filterEnvironmentEntries(
  entries: EnvironmentEntry[],
  search: string,
): EnvironmentEntry[] {
  const needle = search.trim().toLocaleLowerCase();
  if (!needle) return entries;
  return entries.filter((item) =>
    [item.key, item.section, item.description].some((value) =>
      value.toLocaleLowerCase().includes(needle),
    ),
  );
}

export function groupEnvironmentEntries(
  entries: EnvironmentEntry[],
): Array<{ section: string; entries: EnvironmentEntry[] }> {
  const groups = new Map<string, EnvironmentEntry[]>();
  for (const item of entries) {
    const group = groups.get(item.section) ?? [];
    group.push(item);
    groups.set(item.section, group);
  }
  return [...groups].map(([section, grouped]) => ({ section, entries: grouped }));
}

export function EnvironmentPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const load = useCallback(
    () => api<EnvironmentSnapshot>("/environment").then((response) => response.data),
    [],
  );
  const query = useApiQuery(load);
  const [search, setSearch] = useState("");
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [providerChanges, setProviderChanges] = useState<LLMProviderDraft[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingProfile, setTestingProfile] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string[]>([
    PROVIDER_PANEL_KEY,
    LLM_PANEL_KEY,
    TASK_PANEL_KEY,
  ]);

  const nonLlmEntries = useMemo(
    () => (query.data?.entries ?? []).filter((item) => !LLM_CONFIG_KEYS.has(item.key)),
    [query.data?.entries],
  );
  const filtered = useMemo(
    () => filterEnvironmentEntries(nonLlmEntries, search),
    [nonLlmEntries, search],
  );
  const entryByKey = useMemo(
    () => new Map((query.data?.entries ?? []).map((item) => [item.key, item])),
    [query.data?.entries],
  );
  const groups = useMemo(() => groupEnvironmentEntries(filtered), [filtered]);
  const changedKeys = Object.keys(changes);
  const baseProviderDrafts = useMemo<LLMProviderDraft[]>(
    () => (query.data?.llmProviders ?? []).map((provider) => ({
      ...provider,
      draftKey: provider.id,
    })),
    [query.data?.llmProviders],
  );
  const providerDrafts = providerChanges ?? baseProviderDrafts;
  const totalChanges = changedKeys.length + (providerChanges === null ? 0 : 1);

  const countDirty = (keys: readonly string[]): number =>
    changedKeys.filter((key) => keys.includes(key)).length;

  const setValue = (key: string, value: string | null) => {
    setChanges((current) => ({ ...current, [key]: value }));
  };
  const undo = (key: string) => {
    setChanges((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const currentValue = (key: string): string => {
    if (Object.hasOwn(changes, key)) return changes[key] ?? "";
    const item = entryByKey.get(key);
    return item?.value ?? item?.defaultValue ?? "";
  };

  const changeProviders = (mutate: (drafts: LLMProviderDraft[]) => LLMProviderDraft[]) => {
    setProviderChanges((current) => mutate(current ?? baseProviderDrafts));
  };

  const updateProvider = (draftKey: string, patch: Partial<LLMProviderDraft>) => {
    changeProviders((drafts) => drafts.map((provider) => (
      provider.draftKey === draftKey ? { ...provider, ...patch } : provider
    )));
  };

  const addProvider = () => {
    const existing = new Set(providerDrafts.map((provider) => provider.id));
    let index = 1;
    while (existing.has(`provider${index}`)) index += 1;
    const id = `provider${index}`;
    changeProviders((drafts) => [...drafts, {
      id,
      draftKey: `new-${Date.now()}-${index}`,
      baseUrl: "https://example.com/v1",
      builtIn: false,
      apiKeyConfigured: false,
      apiKeyRootConfigured: false,
      baseUrlSource: "env",
      apiKeySource: "env",
      overridden: false,
      isNew: true,
    }]);
  };

  const providerInUse = (providerId: string): boolean =>
    MODEL_CONFIGS.some((profile) => currentValue(profile.providerKey) === providerId);

  const deleteProvider = (draftKey: string) => {
    const provider = providerDrafts.find((item) => item.draftKey === draftKey);
    if (!provider) return;
    if (providerInUse(provider.id)) {
      message.error("请先把使用该提供商的模型档位切换到其他提供商");
      return;
    }
    changeProviders((drafts) => drafts.filter((item) => item.draftKey !== draftKey));
  };

  const save = async () => {
    if (!query.data || totalChanges === 0) return;
    setSaving(true);
    try {
      const { data } = await api<EnvironmentPatchResult>("/environment", {
        method: "PATCH",
        body: JSON.stringify({
          version: query.data.version,
          changes: changedKeys.map((key) => ({ key, value: changes[key] })),
          ...(providerChanges === null ? {} : {
            providers: providerDrafts.map((provider) => ({
              id: provider.id,
              baseUrl: provider.baseUrl,
              ...(Object.hasOwn(provider, "apiKey") ? { apiKey: provider.apiKey } : {}),
            })),
          }),
        }),
      });
      setChanges({});
      setProviderChanges(null);
      query.reload();
      if (data.restartRequired) message.success("环境配置已保存，重启 YawnBot 后生效");
      if (data.updatedKeys.includes("WEBUI_ADMIN_TOKEN")) {
        message.warning("重启后当前管理会话将失效，请使用新 Token 登录");
      }
      if (changes.WEBUI_ENABLED === "false") {
        message.warning("重启后 WebUI 将关闭");
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        setChanges({});
        setProviderChanges(null);
        query.reload();
        message.error("配置文件已变化，页面已刷新，请重新修改");
      } else {
        message.error(reason instanceof Error ? reason.message : "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  /* 搜索命中哪个分组就自动展开哪个,避免命中内容被折叠藏住 */
  const handleSearchChange = (value: string) => {
    setSearch(value);
    if (!value.trim()) return;
    const matched = new Set(
      filterEnvironmentEntries(nonLlmEntries, value).map((item) => item.section),
    );
    if (matched.size === 0) return;
    setExpanded((current) => {
      const next = new Set(current);
      for (const section of matched) next.add(section);
      return next.size === current.length ? current : [...next];
    });
  };

  const editor = (item: EnvironmentEntry): React.ReactNode => {
    const dirty = Object.hasOwn(changes, item.key);
    const draft = dirty ? changes[item.key] : item.value;
    if (draft === null && dirty) return <Tag color="red">保存后移除根 .env 配置</Tag>;
    const common = {
      value: draft ?? "",
      onChange: (value: string) => setValue(item.key, value),
    };
    if (item.kind === "enum") {
      return (
        <Select
          value={draft ?? undefined}
          placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
          onChange={(value) => setValue(item.key, value)}
          style={{ width: "100%" }}
          options={item.options.map((value) => ({
            value,
            label: enumLabel(item, value),
          }))}
        />
      );
    }
    if (item.kind === "boolean") {
      return (
        <Select
          value={draft ?? undefined}
          placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
          onChange={(value) => setValue(item.key, value)}
          style={{ width: "100%" }}
          options={[
            { value: "true", label: "true（开启）" },
            { value: "false", label: "false（关闭）" },
          ]}
        />
      );
    }
    if (item.secret) {
      return (
        <Input.Password
          value={dirty ? draft ?? "" : ""}
          placeholder={item.effectiveConfigured ? "已配置，输入新值以替换" : "未配置"}
          onChange={(event) => setValue(item.key, event.target.value)}
          visibilityToggle={false}
        />
      );
    }
    return (
      <Input
        value={common.value}
        placeholder={item.defaultValue === null ? "未配置" : `默认：${item.defaultValue}`}
        onChange={(event) => common.onChange(event.target.value)}
      />
    );
  };

  const columns: ColumnsType<EnvironmentEntry> = [
    {
      title: "配置项",
      dataIndex: "key",
      width: 260,
      render: (key: string, item) => (
        <Space orientation="vertical" size={2}>
          <Text code copyable>{key}</Text>
          {item.description && <Text type="secondary">{item.description}</Text>}
        </Space>
      ),
    },
    {
      title: "来源",
      width: 150,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
          <Tag color={SOURCE_META[item.source].color}>{SOURCE_META[item.source].label}</Tag>
          {item.secret && item.effectiveConfigured && <Text type="secondary">已配置（值已隐藏）</Text>}
          {item.overridden && <Text type="danger">根 .env 修改当前不生效</Text>}
        </Space>
      ),
    },
    { title: "根 .env 值", render: (_, item) => editor(item) },
    {
      title: "操作",
      width: 110,
      render: (_, item) => (
        <Space>
          <Button
            aria-label={`移除 ${item.key}`}
            title="移除根 .env 配置"
            icon={<DeleteOutlined />}
            disabled={!item.configured && !Object.hasOwn(changes, item.key)}
            onClick={() => setValue(item.key, null)}
          />
          <Button
            aria-label={`撤销 ${item.key}`}
            title="撤销未保存修改"
            icon={<RedoOutlined />}
            disabled={!Object.hasOwn(changes, item.key)}
            onClick={() => undo(item.key)}
          />
        </Space>
      ),
    },
  ];

  const llmEditor = (key: string, label: string): React.ReactNode => {
    const item = entryByKey.get(key);
    if (!item) return null;
    return (
      <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        <Space size={4} wrap>
          <Text strong>{label}</Text>
          <Text code>{key}</Text>
          {item.overridden && <Tag color="red">当前被外部环境覆盖</Tag>}
        </Space>
        {editor(item)}
      </Space>
    );
  };

  const providerEditor = (key: string): React.ReactNode => {
    const item = entryByKey.get(key);
    if (!item) return null;
    return (
      <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        <Space size={4} wrap>
          <Text strong>提供商</Text>
          <Text code>{key}</Text>
          {item.overridden && <Tag color="red">当前被外部环境覆盖</Tag>}
        </Space>
        <Select
          value={currentValue(key) || "default"}
          style={{ width: "100%" }}
          onChange={(value) => setValue(key, value)}
          options={providerDrafts.map((provider) => ({
            value: provider.id,
            label: provider.id === "default" ? "default（内置）" : provider.id,
          }))}
        />
      </Space>
    );
  };

  const resolvedProfile = (profile: typeof MODEL_CONFIGS[number]) => {
    const configuredModel = currentValue(profile.modelKey).trim();
    if (profile.modelKey !== "AI_MODEL" && !configuredModel) {
      return {
        providerId: currentValue("AI_DEFAULT_PROVIDER") || "default",
        model: currentValue("AI_MODEL").trim(),
        fallback: true,
      };
    }
    return {
      providerId: currentValue(profile.providerKey) || "default",
      model: configuredModel,
      fallback: false,
    };
  };

  const testProfile = async (profile: typeof MODEL_CONFIGS[number]) => {
    const resolved = resolvedProfile(profile);
    if (profile.modelKey === "AI_VISION_MODEL" && resolved.fallback) {
      message.warning("识图模型未配置，当前没有独立识图档位可测试");
      return;
    }
    const provider = providerDrafts.find((item) => item.id === resolved.providerId);
    if (!provider || !resolved.model) {
      message.error("请先完整填写提供商、模型 ID 和 Base URL");
      return;
    }
    setTestingProfile(profile.modelKey);
    try {
      const { data } = await api<LLMConnectionTestResult>("/llm/test", {
        method: "POST",
        body: JSON.stringify({
          providerId: provider.id,
          baseUrl: provider.baseUrl,
          model: resolved.model,
          ...(Object.hasOwn(provider, "apiKey") ? { apiKey: provider.apiKey } : {}),
        }),
      });
      message.success(`连接成功，耗时 ${data.latencyMs.toFixed(1)} ms`);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "连接测试失败");
    } finally {
      setTestingProfile(null);
    }
  };

  const providerCards = (
    <Row gutter={[16, 16]}>
      {providerDrafts.map((provider) => {
        const replacement = typeof provider.apiKey === "string" && provider.apiKey.length > 0;
        const removingKey = provider.apiKey === null;
        return (
          <Col xs={24} lg={12} xl={8} key={provider.draftKey}>
            <Card
              size="small"
              title={(
                <Space size={8}>
                  <span className="env-badge tone-sky" aria-hidden>🔌</span>
                  {provider.id || "未命名提供商"}
                  {provider.builtIn && <Tag>内置</Tag>}
                  {provider.overridden && <Tag color="red">外部环境覆盖</Tag>}
                </Space>
              )}
              extra={!provider.builtIn && (
                <Popconfirm
                  title="删除这个提供商？"
                  description="若模型档位仍在使用它，需要先切换档位。"
                  onConfirm={() => deleteProvider(provider.draftKey)}
                >
                  <Button danger type="text" icon={<DeleteOutlined />} aria-label={`删除提供商 ${provider.id}`} />
                </Popconfirm>
              )}
            >
              <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                <div>
                  <Text strong>提供商 ID</Text>
                  <Input
                    value={provider.id}
                    disabled={!provider.isNew}
                    onChange={(event) => updateProvider(provider.draftKey, { id: event.target.value })}
                    placeholder="例如 fast"
                  />
                </div>
                <div>
                  <Text strong>Base URL</Text>
                  <Input
                    value={provider.baseUrl}
                    onChange={(event) => updateProvider(provider.draftKey, { baseUrl: event.target.value })}
                    placeholder="https://example.com/v1"
                  />
                </div>
                <div>
                  <Space size={6} wrap>
                    <Text strong>API Key</Text>
                    <Tag color={removingKey ? "red" : replacement ? "orange" : provider.apiKeyConfigured ? "green" : "default"}>
                      {removingKey ? "待移除" : replacement ? "待替换" : provider.apiKeyConfigured ? "已配置" : "未配置"}
                    </Tag>
                  </Space>
                  <Input.Password
                    value={replacement ? provider.apiKey ?? "" : ""}
                    visibilityToggle={false}
                    autoComplete="new-password"
                    placeholder={provider.apiKeyConfigured ? "已配置，输入新值以替换" : "输入 API Key"}
                    onChange={(event) => updateProvider(provider.draftKey, {
                      apiKey: event.target.value || undefined,
                    })}
                  />
                </div>
                {provider.builtIn && provider.apiKeyConfigured && (
                  <Popconfirm title="移除 default 提供商的 API Key？" onConfirm={() => updateProvider(provider.draftKey, { apiKey: null })}>
                    <Button danger size="small">移除密钥</Button>
                  </Popconfirm>
                )}
                {provider.overridden && (
                  <Text type="danger">保存只修改根 .env，重启后仍会被进程环境或环境文件覆盖。</Text>
                )}
              </Space>
            </Card>
          </Col>
        );
      })}
      <Col xs={24} lg={12} xl={8}>
        <Button className="env-add-provider" block icon={<PlusOutlined />} onClick={addProvider}>
          添加提供商
        </Button>
      </Col>
    </Row>
  );

  /* 折叠面板头部:标题 + 提示 + 条目数 + 未保存角标(折叠时也可见) */
  const panelLabel = (icon: string, title: string): React.ReactNode => (
    <Space size={10}>
      <span aria-hidden>{icon}</span>
      <span>{title}</span>
    </Space>
  );
  const panelExtra = (
    dirty: number,
    hint?: string,
    count?: number,
  ): React.ReactNode => (
    <Space size={12}>
      {hint && <Text type="secondary">{hint}</Text>}
      {count !== undefined && <span className="env-count-chip">{count} 项</span>}
      {dirty > 0 && <span className="env-dirty-badge">{dirty} 项未保存</span>}
    </Space>
  );

  const llmItems: CollapseProps["items"] = [
    {
      key: PROVIDER_PANEL_KEY,
      label: panelLabel("🔌", "LLM 提供商"),
      extra: panelExtra(providerChanges === null ? 0 : 1, "Base URL 与密钥集中管理"),
      children: providerCards,
    },
    {
      key: LLM_PANEL_KEY,
      label: panelLabel("🧠", "LLM 模型档位"),
      extra: panelExtra(countDirty(MODEL_PANEL_KEYS), "保存后重启生效"),
      children: (
        <Row gutter={[16, 16]}>
          {MODEL_CONFIGS.map((model, index) => {
            const badge = MODEL_BADGES[index];
            return (
              <Col xs={24} xl={8} key={model.modelKey}>
                <Card
                  size="small"
                  title={(
                    <Space size={8}>
                      <span className={`env-badge ${badge.tone}`} aria-hidden>{badge.icon}</span>
                      {model.title}
                    </Space>
                  )}
                >
                  <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                    <Text type="secondary">{model.description}</Text>
                    {providerEditor(model.providerKey)}
                    {llmEditor(model.modelKey, "模型 ID")}
                    {llmEditor(model.thinkingKey, "全局推理")}
                    {model.multimodalKey && llmEditor(model.multimodalKey, "图片能力")}
                    <Text type="secondary">
                      实际路由：{resolvedProfile(model).providerId} / {resolvedProfile(model).model || "未配置"}
                      {resolvedProfile(model).fallback ? "（继承默认档位）" : ""}
                    </Text>
                    <Button
                      icon={<ExperimentOutlined />}
                      loading={testingProfile === model.modelKey}
                      onClick={() => void testProfile(model)}
                    >
                      测试此档位
                    </Button>
                  </Space>
                </Card>
              </Col>
            );
          })}
        </Row>
      ),
    },
    {
      key: TASK_PANEL_KEY,
      label: panelLabel("🔀", "子插件任务路由"),
      extra: panelExtra(
        countDirty(TASK_PANEL_KEYS),
        "任务推理选择 inherit 时继承模型档位",
      ),
      children: (
        <Row gutter={[16, 16]}>
          {TASK_CONFIGS.map((group) => {
            const badge = PLUGIN_META[group.plugin] ?? { icon: "✨", tone: "tone-sakura" };
            return (
              <Col xs={24} xl={8} key={group.plugin}>
                <Card
                  size="small"
                  title={(
                    <Space size={8}>
                      <span className={`env-badge ${badge.tone}`} aria-hidden>{badge.icon}</span>
                      {group.plugin}
                    </Space>
                  )}
                >
                  <Space orientation="vertical" size="large" style={{ width: "100%" }}>
                    {group.tasks.map(([label, profileKey, thinkingKey]) => (
                      <Card size="small" key={profileKey} title={label}>
                        <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                          {llmEditor(profileKey, "模型档位")}
                          {llmEditor(thinkingKey, "推理覆盖")}
                        </Space>
                      </Card>
                    ))}
                  </Space>
                </Card>
              </Col>
            );
          })}
        </Row>
      ),
    },
  ];

  const groupItems: CollapseProps["items"] = groups.map((group) => ({
    key: group.section,
    label: panelLabel(SECTION_ICONS[group.section] ?? "🌸", group.section),
    extra: panelExtra(countDirty(group.entries.map((item) => item.key)), undefined, group.entries.length),
    children: (
      <Table
        rowKey="key"
        loading={query.loading}
        columns={columns}
        dataSource={group.entries}
        pagination={false}
        scroll={{ x: 900 }}
      />
    ),
  }));

  const allPanelKeys = [
    PROVIDER_PANEL_KEY,
    LLM_PANEL_KEY,
    TASK_PANEL_KEY,
    ...groups.map((group) => group.section),
  ];

  return (
    <Space
      orientation="vertical"
      size="large"
      style={{ width: "100%", paddingBottom: totalChanges > 0 ? 96 : undefined }}
    >
      <PageHeader
        title="环境配置"
        subtitle="集中修改根 .env；全部变更仅在重启 YawnBot 后生效"
        extra={(
          <Button
            type="primary"
            icon={<SaveOutlined />}
            disabled={totalChanges === 0}
            loading={saving}
            onClick={() => void save()}
          >
            保存 {totalChanges > 0 ? `${totalChanges} 项` : ""}
          </Button>
        )}
      />
      <Alert
        type="warning"
        showIcon
        title="敏感配置只允许替换，不会从服务端回显"
        description={`当前环境：${query.data?.environment ?? "—"}。进程环境变量或 ${query.data?.environmentFile ?? ".env.<ENVIRONMENT>"} 的值优先于根 .env。`}
      />
      <Collapse
        ghost
        className="env-collapse"
        activeKey={expanded}
        onChange={setExpanded}
        items={llmItems}
      />
      <Space size={12} wrap>
        <Input.Search
          className="env-search"
          allowClear
          placeholder="搜索配置名、分组或说明"
          value={search}
          onChange={(event) => handleSearchChange(event.target.value)}
        />
        <Button size="small" onClick={() => setExpanded(allPanelKeys)}>全部展开</Button>
        <Button size="small" onClick={() => setExpanded([])}>全部收起</Button>
      </Space>
      {query.error && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {!query.loading && groups.length === 0 && <Empty description="没有匹配的配置项" />}
      {groups.length > 0 && (
        <Collapse
          ghost
          className="env-collapse"
          activeKey={expanded}
          onChange={setExpanded}
          items={groupItems}
        />
      )}
      {totalChanges > 0 && (
        <div className="env-save-bar liquid-glass" role="status">
          <span className="env-save-bar-text">未保存修改 {totalChanges} 项</span>
          <Button onClick={() => { setChanges({}); setProviderChanges(null); }}>撤销全部</Button>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={saving}
            onClick={() => void save()}
          >
            保存全部修改
          </Button>
        </div>
      )}
    </Space>
  );
}
