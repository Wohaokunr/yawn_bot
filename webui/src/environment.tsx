import {
  DeleteOutlined,
  PlusOutlined,
  RedoOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Col,
  Collapse,
  Empty,
  Input,
  Row,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { CollapseProps } from "antd";
import { useCallback, useMemo, useState } from "react";
import { api, ApiError } from "./api";
import {
  DirtySaveBar,
  EnvironmentDiffPreview,
  EnvironmentSection,
  ModelProfileCard,
  ProviderEditor,
  TaskRoutingPanel,
} from "./environment-components";
import type { EnvironmentDiffRow, LLMProviderDraft, ModelTestState } from "./environment-components";
import {
  enumLabel,
  filterEnvironmentEntries,
  groupEnvironmentEntries,
  LLM_CONFIG_KEYS,
  LLM_PANEL_KEY,
  MODEL_BADGES,
  MODEL_CONFIGS,
  MODEL_PANEL_KEYS,
  PLUGIN_META,
  PROVIDER_PANEL_KEY,
  SECTION_ICONS,
  SOURCE_META,
  TASK_CONFIGS,
  TASK_PANEL_KEY,
  TASK_PANEL_KEYS,
} from "./environment-config";
export { filterEnvironmentEntries, groupEnvironmentEntries } from "./environment-config";
import { PageHeader, QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "./shared";
import type {
  EnvironmentEntry,
  EnvironmentPatchResult,
  EnvironmentSnapshot,
  LLMConnectionTestResult,
} from "./types";

const { Text } = Typography;

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
  const [testResults, setTestResults] = useState<Record<string, ModelTestState>>({});
  const [previewOpen, setPreviewOpen] = useState(false);
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
  useUnsavedChanges(totalChanges > 0);

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

  const profileForRoute = (route: string) => {
    if (route === "light") return MODEL_CONFIGS[1];
    if (route === "vision") return MODEL_CONFIGS[2];
    return MODEL_CONFIGS[0];
  };

  const providerUsage = (providerId: string): string[] => {
    const usage: string[] = [];
    for (const profile of MODEL_CONFIGS) {
      if ((currentValue(profile.providerKey) || "default") === providerId) usage.push(profile.title);
    }
    for (const group of TASK_CONFIGS) {
      for (const [label, profileKey] of group.tasks) {
        const profile = profileForRoute(currentValue(profileKey));
        if ((currentValue(profile.providerKey) || "default") === providerId) usage.push(`${group.plugin} · ${label}`);
      }
    }
    return usage;
  };

  const diffRows: EnvironmentDiffRow[] = changedKeys.map((key) => {
    const item = entryByKey.get(key);
    const next = changes[key];
    const secret = item?.secret ?? false;
    return {
      key,
      area: item?.section ?? "环境配置",
      before: secret ? (item?.effectiveConfigured ? "已配置（值隐藏）" : "未配置") : (item?.value ?? item?.defaultValue ?? "未配置"),
      after: secret ? (next === null ? "待移除" : "待替换（值隐藏）") : (next === null ? "移除根 .env 配置" : next),
      secret,
      restartRequired: true,
    };
  });
  if (providerChanges !== null) {
    diffRows.push({
      key: "AI_PROVIDERS",
      area: "LLM 提供商",
      before: `当前 ${baseProviderDrafts.length} 个提供商（敏感值隐藏）`,
      after: `保存 ${providerDrafts.length} 个提供商（敏感值隐藏）`,
      secret: true,
      restartRequired: true,
    });
  }

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
      setPreviewOpen(false);
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
      setTestResults((current) => ({
        ...current,
        [profile.modelKey]: {
          ok: true,
          latencyMs: data.latencyMs,
          message: `${resolved.providerId} / ${resolved.model}`,
          testedAt: new Date().toLocaleString(),
        },
      }));
      message.success(`连接成功，耗时 ${data.latencyMs.toFixed(1)} ms`);
    } catch (reason) {
      const errorMessage = reason instanceof Error ? reason.message : "连接测试失败";
      setTestResults((current) => ({
        ...current,
        [profile.modelKey]: {
          ok: false,
          message: errorMessage,
          testedAt: new Date().toLocaleString(),
        },
      }));
      message.error(errorMessage);
    } finally {
      setTestingProfile(null);
    }
  };

  const providerCards = (
    <Row gutter={[16, 16]}>
      {providerDrafts.map((provider) => <Col xs={24} lg={12} xl={8} key={provider.draftKey}>
        <ProviderEditor
          provider={provider}
          usage={providerUsage(provider.id)}
          onUpdate={(patch) => updateProvider(provider.draftKey, patch)}
          onDelete={() => deleteProvider(provider.draftKey)}
        />
      </Col>)}
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
            const resolved = resolvedProfile(model);
            return (
              <Col xs={24} xl={8} key={model.modelKey}>
                <ModelProfileCard
                  title={model.title}
                  description={model.description}
                  icon={badge.icon}
                  tone={badge.tone}
                  editors={[
                    providerEditor(model.providerKey),
                    llmEditor(model.modelKey, "模型 ID"),
                    llmEditor(model.thinkingKey, "全局推理"),
                    ...(model.multimodalKey ? [llmEditor(model.multimodalKey, "图片能力")] : []),
                  ]}
                  resolvedRoute={`${resolved.providerId} / ${resolved.model || "未配置"}`}
                  fallback={resolved.fallback}
                  testing={testingProfile === model.modelKey}
                  testResult={testResults[model.modelKey]}
                  onTest={() => void testProfile(model)}
                />
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
                <TaskRoutingPanel
                  plugin={group.plugin}
                  icon={badge.icon}
                  tone={badge.tone}
                  tasks={group.tasks.map(([label, profileKey, thinkingKey]) => {
                    const profile = profileForRoute(currentValue(profileKey));
                    const route = resolvedProfile(profile);
                    return {
                      key: profileKey,
                      label,
                      profileEditor: llmEditor(profileKey, "模型档位"),
                      thinkingEditor: llmEditor(thinkingKey, "推理覆盖"),
                      routeHint: `${profile.title} → ${route.providerId}`,
                    };
                  })}
                />
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
    children: <EnvironmentSection entries={group.entries} columns={columns} loading={query.loading} />,
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
        status={<SaveStatus dirty={totalChanges > 0} saving={saving} />}
        extra={(
          <Button
            type="primary"
            icon={<SaveOutlined />}
            disabled={totalChanges === 0}
            loading={saving}
            onClick={() => setPreviewOpen(true)}
          >
            预览保存 {totalChanges > 0 ? `${totalChanges} 项` : ""}
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
        <DirtySaveBar
          totalChanges={totalChanges}
          saving={saving}
          onDiscard={() => { setChanges({}); setProviderChanges(null); }}
          onPreview={() => setPreviewOpen(true)}
        />
      )}
      <EnvironmentDiffPreview
        open={previewOpen}
        rows={diffRows}
        saving={saving}
        onCancel={() => setPreviewOpen(false)}
        onConfirm={() => void save()}
      />
    </Space>
  );
}
