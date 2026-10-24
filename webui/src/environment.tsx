import { PlusOutlined, SaveOutlined, SearchOutlined } from "@ant-design/icons";
import {
  App as AntApp,
  Button,
  Col,
  Empty,
  Input,
  Row,
  Segmented,
  Select,
  Skeleton,
  Space,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { InputRef } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "./api";
import {
  CategoryNav,
  DiagnosticsPanel,
  DirtySaveBar,
  EnvironmentDiffPreview,
  ModelProfileCard,
  ProviderEditor,
  RestartBanner,
  TaskRoutingTable,
} from "./environment-components";
import type {
  CategoryStat,
  EnvironmentDiffRow,
  LLMProviderDraft,
  ModelTestState,
  TaskRouteRow,
} from "./environment-components";
import {
  LLM_CONFIG_KEYS,
  MODEL_BADGES,
  MODEL_CONFIGS,
  MODEL_PANEL_KEYS,
  PLUGIN_META,
  TASK_CONFIGS,
  TASK_PANEL_KEYS,
} from "./environment-config";
import { FieldEditor, FieldRow } from "./environment-editors";
import {
  categoryByKey,
  categoryOf,
  compactJson,
  dangerousChanges,
  diagnosticKeySet,
  draftErrors,
  ENV_CATEGORIES,
  FILTER_OPTIONS,
  isFilter,
  matchesFilter,
  MODELS_CATEGORY,
  PROVIDER_STORAGE_KEYS,
  searchEntries,
  sortDiagnostics,
} from "./environment-fields";
import type { EnvironmentFilter } from "./environment-fields";
export { filterEnvironmentEntries, groupEnvironmentEntries } from "./environment-config";
import { PageHeader, QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "./shared";
import type {
  EnvironmentDiagnostic,
  EnvironmentEntry,
  EnvironmentPatchResult,
  EnvironmentSnapshot,
  LLMConnectionTestResult,
  SystemStatus,
} from "./types";

const { Text } = Typography;

const PROFILE_PROVIDER_KEYS = new Set<string>(MODEL_CONFIGS.map((item) => item.providerKey));
const MODEL_KEY_SET = new Set<string>(MODEL_PANEL_KEYS);
const TASK_KEY_SET = new Set<string>(TASK_PANEL_KEYS);
type ModelTab = "providers" | "profiles" | "routing" | "other";
const MODEL_TABS: ModelTab[] = ["providers", "profiles", "routing", "other"];
const RESTART_POLL_MS = 2000;
const RESTART_TIMEOUT_MS = 120_000;
const HIGHLIGHT_MS = 2400;

function modelTabFor(key: string): ModelTab {
  if (PROVIDER_STORAGE_KEYS.has(key)) return "providers";
  if (MODEL_KEY_SET.has(key)) return "profiles";
  if (TASK_KEY_SET.has(key)) return "routing";
  return "other";
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export function EnvironmentPage(): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const load = useCallback(
    () => api<EnvironmentSnapshot>("/environment").then((response) => response.data),
    [],
  );
  const query = useApiQuery(load);
  const [params, setParams] = useSearchParams();
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [providerChanges, setProviderChanges] = useState<LLMProviderDraft[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingProfile, setTestingProfile] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ModelTestState>>({});
  const [previewOpen, setPreviewOpen] = useState(false);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const searchRef = useRef<InputRef>(null);

  // URL 状态：分类 / 模型子页 / 搜索 / 筛选，刷新和分享链接都能还原
  const category = categoryByKey(params.get("env.cat")).key;
  const rawTab = params.get("env.tab");
  const modelTab: ModelTab = MODEL_TABS.includes(rawTab as ModelTab) ? rawTab as ModelTab : "providers";
  const search = params.get("env.q") ?? "";
  const rawFilter = params.get("env.filter");
  const filter: EnvironmentFilter = isFilter(rawFilter) ? rawFilter : "all";
  const updateParams = useCallback((patch: Record<string, string | null>) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      return next;
    }, { replace: true });
  }, [setParams]);

  const entries = useMemo(() => query.data?.entries ?? [], [query.data?.entries]);
  const entryByKey = useMemo(() => new Map(entries.map((item) => [item.key, item])), [entries]);
  const baseProviderDrafts = useMemo<LLMProviderDraft[]>(
    () => (query.data?.llmProviders ?? []).map((provider) => ({ ...provider, draftKey: provider.id })),
    [query.data?.llmProviders],
  );
  const providerDrafts = providerChanges ?? baseProviderDrafts;
  const changedKeys = Object.keys(changes);
  const totalChanges = changedKeys.length + (providerChanges === null ? 0 : 1);
  const errors = useMemo(() => draftErrors(entryByKey, changes), [entryByKey, changes]);
  const errorCount = Object.keys(errors).length;
  const diagnostics = useMemo(() => sortDiagnostics(query.data?.diagnostics ?? []), [query.data?.diagnostics]);
  const diagnosticKeys = useMemo(() => diagnosticKeySet(diagnostics), [diagnostics]);
  useUnsavedChanges(totalChanges > 0);

  // 档位提供商字段在任何位置都渲染成提供商下拉
  const decorate = useCallback((entry: EnvironmentEntry): EnvironmentEntry => {
    if (!PROFILE_PROVIDER_KEYS.has(entry.key)) return entry;
    return { ...entry, kind: "enum", options: providerDrafts.map((provider) => provider.id) };
  }, [providerDrafts]);

  // 普通字段：提供商存储键只能走提供商表单
  const fieldEntries = useMemo(
    () => entries.filter((item) => !PROVIDER_STORAGE_KEYS.has(item.key)),
    [entries],
  );
  const filterContext = useMemo(
    () => ({ changes, errors, diagnosticKeys }),
    [changes, errors, diagnosticKeys],
  );
  const resultsMode = search.trim() !== "" || filter !== "all";
  const results = useMemo(
    () => searchEntries(fieldEntries, search).filter((item) => matchesFilter(item, filter, filterContext)),
    [fieldEntries, search, filter, filterContext],
  );

  const stats = useMemo(() => {
    const next: Record<string, CategoryStat> = {};
    for (const item of fieldEntries) {
      const key = categoryOf(item.section);
      const stat = next[key] ?? { total: 0, customized: 0, dirty: 0, issues: 0 };
      stat.total += 1;
      if (item.configured) stat.customized += 1;
      if (Object.hasOwn(changes, item.key)) stat.dirty += 1;
      if (item.issue || errors[item.key] || diagnosticKeys.has(item.key)) stat.issues += 1;
      next[key] = stat;
    }
    const models = next[MODELS_CATEGORY] ?? { total: 0, customized: 0, dirty: 0, issues: 0 };
    if (providerChanges !== null) models.dirty += 1;
    if (diagnostics.some((item) => item.keys.some((key) => PROVIDER_STORAGE_KEYS.has(key)) && item.level !== "info")) models.issues += 1;
    next[MODELS_CATEGORY] = models;
    return next;
  }, [fieldEntries, changes, errors, diagnosticKeys, diagnostics, providerChanges]);

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

  const resolvedProfile = (profile: typeof MODEL_CONFIGS[number]) => {
    const configuredModel = currentValue(profile.modelKey).trim();
    if (profile.modelKey !== "AI_MODEL" && !configuredModel) {
      return { providerId: currentValue("AI_DEFAULT_PROVIDER") || "default", model: currentValue("AI_MODEL").trim(), fallback: true };
    }
    return { providerId: currentValue(profile.providerKey) || "default", model: configuredModel, fallback: false };
  };

  const providerUsage = (providerId: string): string[] => {
    const usage: string[] = [];
    for (const profile of MODEL_CONFIGS) {
      if ((currentValue(profile.providerKey) || "default") === providerId) usage.push(profile.title);
    }
    for (const group of TASK_CONFIGS) {
      for (const [label, profileKey] of group.tasks) {
        const profile = profileForRoute(currentValue(profileKey));
        if (resolvedProfile(profile).providerId === providerId) usage.push(`${group.plugin} · ${label}`);
      }
    }
    return usage;
  };

  const areaOf = (key: string): string => {
    const item = entryByKey.get(key);
    if (!item) return "其他";
    return `${categoryByKey(categoryOf(item.section)).label} · ${item.section}`;
  };

  const diffRows: EnvironmentDiffRow[] = changedKeys.map((key) => {
    const item = entryByKey.get(key);
    const next = changes[key];
    const secret = item?.secret ?? false;
    return {
      key,
      area: areaOf(key),
      before: secret ? (item?.effectiveConfigured ? "已配置（值隐藏）" : "未配置") : (item?.value ?? (item?.defaultValue ? `默认 ${item.defaultValue}` : "未配置")),
      after: secret ? (next === null ? "移除" : "替换为新值（隐藏）") : (next === null ? `恢复默认${item?.defaultValue ? `（${item.defaultValue}）` : ""}` : next),
      secret,
      restartRequired: true,
    };
  });
  if (providerChanges !== null) {
    diffRows.push({
      key: "AI_PROVIDERS",
      area: "模型与提供商 · LLM 提供商",
      before: `当前 ${baseProviderDrafts.length} 个提供商（敏感值隐藏）`,
      after: `保存 ${providerDrafts.length} 个提供商（敏感值隐藏）`,
      secret: true,
      restartRequired: true,
    });
  }
  diffRows.sort((left, right) => left.area.localeCompare(right.area, "zh-CN"));

  const changeProviders = (mutate: (drafts: LLMProviderDraft[]) => LLMProviderDraft[]) => {
    setProviderChanges((current) => mutate(current ?? baseProviderDrafts));
  };
  const updateProvider = (draftKey: string, patch: Partial<LLMProviderDraft>) => {
    changeProviders((drafts) => drafts.map((provider) => (provider.draftKey === draftKey ? { ...provider, ...patch } : provider)));
  };
  const addProvider = () => {
    const existing = new Set(providerDrafts.map((provider) => provider.id));
    let index = 1;
    while (existing.has(`provider${index}`)) index += 1;
    changeProviders((drafts) => [...drafts, {
      id: `provider${index}`,
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
  const deleteProvider = (draftKey: string) => {
    const provider = providerDrafts.find((item) => item.draftKey === draftKey);
    if (!provider) return;
    if (MODEL_CONFIGS.some((profile) => currentValue(profile.providerKey) === provider.id)) {
      message.error("请先把使用该提供商的模型档位切换到其他提供商");
      return;
    }
    changeProviders((drafts) => drafts.filter((item) => item.draftKey !== draftKey));
  };

  const discardAll = () => {
    setChanges({});
    setProviderChanges(null);
  };

  const openPreview = useCallback(() => {
    if (totalChanges === 0) return;
    if (errorCount > 0) {
      message.error(`还有 ${errorCount} 项填写有误，修正后才能保存`);
      return;
    }
    setPreviewOpen(true);
  }, [totalChanges, errorCount, message]);

  const save = async () => {
    if (!query.data || totalChanges === 0) return;
    setSaving(true);
    // .env 只能单行：JSON 草稿统一压成紧凑格式再提交
    const payloadChanges = changedKeys.map((key) => {
      const value = changes[key];
      const kind = entryByKey.get(key)?.kind;
      return { key, value: kind === "json" && value ? compactJson(value) ?? value : value };
    });
    try {
      const { data } = await api<EnvironmentPatchResult>("/environment", {
        method: "PATCH",
        body: JSON.stringify({
          version: query.data.version,
          changes: payloadChanges,
          ...(providerChanges === null ? {} : {
            providers: providerDrafts.map((provider) => ({
              id: provider.id,
              baseUrl: provider.baseUrl,
              ...(Object.hasOwn(provider, "apiKey") ? { apiKey: provider.apiKey } : {}),
            })),
          }),
        }),
      });
      discardAll();
      setPreviewOpen(false);
      setJustSaved(true);
      query.reload();
      message.success(`已保存 ${data.updatedKeys.length} 项，重启 YawnBot 后生效`);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        // 文件在别处被改：刷新基线但保留草稿，让人确认后再存
        query.reload();
        setPreviewOpen(false);
        message.warning("配置文件刚被其他操作修改，已刷新到最新版本；你的修改还在，请检查后再保存");
      } else {
        message.error(reason instanceof Error ? reason.message : "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const restart = () => {
    modal.confirm({
      title: "重启 YawnBot？",
      content: "Bot 与管理台会断开十几秒到一分钟。若刚修改了运维 Token，重启后需要用新 Token 登录。",
      okText: "立即重启",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await api<{ scheduled: boolean; startedAt: number }>("/system/restart", { method: "POST" });
          setRestarting(true);
          const startedAt = data.startedAt;
          const deadline = Date.now() + RESTART_TIMEOUT_MS;
          const poll = async () => {
            if (Date.now() > deadline) {
              setRestarting(false);
              message.warning("等待超时：YawnBot 可能还在启动，请稍后刷新页面或到服务器上查看日志");
              return;
            }
            try {
              const status = await api<SystemStatus>("/system/status");
              if (status.data.startedAt !== startedAt) {
                setRestarting(false);
                setJustSaved(false);
                query.reload();
                message.success("YawnBot 已重启，新配置已生效");
                return;
              }
            } catch {
              // 重启期间请求失败是正常的，继续等
            }
            window.setTimeout(() => void poll(), RESTART_POLL_MS);
          };
          window.setTimeout(() => void poll(), RESTART_POLL_MS);
        } catch (reason) {
          message.error(reason instanceof Error ? reason.message : "重启失败");
        }
      },
    });
  };

  // 快捷键：Ctrl/⌘+S 保存预览，/ 聚焦搜索
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        openPreview();
      } else if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openPreview]);

  // 定位后滚动并短暂高亮目标字段
  useEffect(() => {
    if (!highlight) return undefined;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(`env-field-${highlight}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    const timer = window.setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [highlight, category, modelTab]);

  const locate = (item: EnvironmentDiagnostic) => {
    if (item.keys.length > 1) {
      const overridden = item.keys.every((key) => entryByKey.get(key)?.overridden);
      updateParams({ "env.q": null, "env.filter": overridden ? "overridden" : "customized" });
      return;
    }
    const key = item.keys[0];
    const entry = entryByKey.get(key);
    const target = entry ? categoryOf(entry.section) : MODELS_CATEGORY;
    updateParams({
      "env.q": null,
      "env.filter": null,
      "env.cat": target,
      "env.tab": target === MODELS_CATEGORY ? modelTabFor(key) : null,
    });
    setHighlight(key);
  };

  const fieldRow = (entry: EnvironmentEntry) => {
    const decorated = decorate(entry);
    return (
      <FieldRow
        key={entry.key}
        entry={decorated}
        draft={changes[entry.key]}
        dirty={Object.hasOwn(changes, entry.key)}
        error={errors[entry.key]}
        highlighted={highlight === entry.key}
        onChange={(value) => setValue(entry.key, value)}
        onUndo={() => undo(entry.key)}
      />
    );
  };

  const sectionBlocks = (list: EnvironmentEntry[]) => {
    const sections = new Map<string, EnvironmentEntry[]>();
    for (const item of list) {
      const group = sections.get(item.section) ?? [];
      group.push(item);
      sections.set(item.section, group);
    }
    return [...sections].map(([section, items]) => {
      // 已弃用的项沉到分组末尾
      const sorted = [...items].sort((left, right) => Number(Boolean(left.meta?.deprecated)) - Number(Boolean(right.meta?.deprecated)));
      return (
        <section key={section} className="envx-section">
          <h3 className="envx-section-title">
            {section}
            <span className="envx-section-count">{items.length} 项</span>
          </h3>
          <div className="envx-fields">{sorted.map(fieldRow)}</div>
        </section>
      );
    });
  };

  // ── 模型与提供商 ──

  const compactEditor = (key: string, label: string): React.ReactNode => {
    const item = entryByKey.get(key);
    if (!item) return null;
    const decorated = decorate(item);
    const dirty = Object.hasOwn(changes, key);
    return (
      <div className={`envx-mini${dirty ? " is-dirty" : ""}${highlight === key ? " is-highlighted" : ""}`} id={`env-field-${key}`}>
        <div className="envx-mini-label">
          <span>{label}</span>
          {item.overridden && <Tag color="red" className="envx-tag">外部覆盖</Tag>}
        </div>
        <FieldEditor
          entry={decorated}
          value={dirty ? changes[key] ?? "" : item.value ?? ""}
          dirty={dirty}
          onChange={(value) => setValue(key, value)}
        />
        {errors[key] && <div className="envx-error">{errors[key]}</div>}
      </div>
    );
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
        [profile.modelKey]: { ok: true, latencyMs: data.latencyMs, message: `${resolved.providerId} / ${resolved.model}`, testedAt: new Date().toLocaleString() },
      }));
      message.success(`连接成功，耗时 ${data.latencyMs.toFixed(1)} ms`);
    } catch (reason) {
      const errorMessage = reason instanceof Error ? reason.message : "连接测试失败";
      setTestResults((current) => ({
        ...current,
        [profile.modelKey]: { ok: false, message: errorMessage, testedAt: new Date().toLocaleString() },
      }));
      message.error(errorMessage);
    } finally {
      setTestingProfile(null);
    }
  };

  const providersView = (
    <Row gutter={[16, 16]}>
      {providerDrafts.map((provider) => (
        <Col xs={24} lg={12} xl={8} key={provider.draftKey}>
          <div id={provider.builtIn ? "env-field-AI_API_KEY" : undefined} className={provider.builtIn && highlight === "AI_API_KEY" ? "envx-mini is-highlighted" : undefined}>
            <ProviderEditor
              provider={provider}
              usage={providerUsage(provider.id)}
              onUpdate={(patch) => updateProvider(provider.draftKey, patch)}
              onDelete={() => deleteProvider(provider.draftKey)}
            />
          </div>
        </Col>
      ))}
      <Col xs={24} lg={12} xl={8}>
        <Button className="env-add-provider" block icon={<PlusOutlined />} onClick={addProvider}>添加提供商</Button>
      </Col>
    </Row>
  );

  const profilesView = (
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
                compactEditor(model.providerKey, "提供商"),
                compactEditor(model.modelKey, "模型 ID"),
                compactEditor(model.thinkingKey, "推理"),
                compactEditor(model.effortKey, "推理等级"),
                ...(model.multimodalKey ? [compactEditor(model.multimodalKey, "图片能力")] : []),
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
  );

  const taskRows: TaskRouteRow[] = TASK_CONFIGS.flatMap((group) =>
    group.tasks.map(([label, profileKey, thinkingKey]) => {
      const profile = profileForRoute(currentValue(profileKey));
      const route = resolvedProfile(profile);
      return {
        key: profileKey,
        plugin: group.plugin,
        pluginIcon: PLUGIN_META[group.plugin]?.icon ?? "✨",
        label,
        profileEditor: compactEditor(profileKey, ""),
        thinkingEditor: compactEditor(thinkingKey, ""),
        route: `${profile.title} → ${route.providerId} / ${route.model || "未配置"}`,
        dirty: Object.hasOwn(changes, profileKey) || Object.hasOwn(changes, thinkingKey),
      };
    }),
  );

  const otherModelEntries = fieldEntries.filter(
    (item) => categoryOf(item.section) === MODELS_CATEGORY && !LLM_CONFIG_KEYS.has(item.key),
  );
  const countDirty = (keys: readonly string[]) => keys.filter((key) => Object.hasOwn(changes, key)).length;
  const tabLabel = (label: string, dirty: number) => (
    <span>{label}{dirty > 0 && <span className="envx-pill is-dirty">{dirty}</span>}</span>
  );

  const modelsView = (
    <Tabs
      activeKey={modelTab}
      onChange={(key) => updateParams({ "env.tab": key === "providers" ? null : key })}
      items={[
        { key: "providers", label: tabLabel("提供商", providerChanges === null ? 0 : 1), children: providersView },
        { key: "profiles", label: tabLabel("模型档位", countDirty(MODEL_PANEL_KEYS)), children: profilesView },
        {
          key: "routing",
          label: tabLabel("任务路由", countDirty(TASK_PANEL_KEYS)),
          children: (
            <Space orientation="vertical" size={10} style={{ width: "100%" }}>
              <Text type="secondary" className="envx-small">每个任务选用哪个模型档位；推理选「inherit」时沿用档位自己的推理设置。</Text>
              <TaskRoutingTable rows={taskRows} />
            </Space>
          ),
        },
        {
          key: "other",
          label: tabLabel("其他参数", countDirty(otherModelEntries.map((item) => item.key))),
          children: otherModelEntries.length ? <div className="envx-fields">{otherModelEntries.map(fieldRow)}</div> : <Empty description="没有其他参数" />,
        },
      ]}
    />
  );

  // ── 主体 ──

  const activeCategory = categoryByKey(category);
  const categoryEntries = fieldEntries.filter((item) => categoryOf(item.section) === category);

  let body: React.ReactNode;
  if (query.loading && !query.data) {
    body = <Skeleton active paragraph={{ rows: 8 }} />;
  } else if (resultsMode) {
    const grouped = ENV_CATEGORIES.map((item) => ({
      category: item,
      list: results.filter((entry) => categoryOf(entry.section) === item.key),
    })).filter((group) => group.list.length > 0);
    body = (
      <div className="envx-results">
        <div className="envx-results-head">
          <Text strong>找到 {results.length} 项</Text>
          <Button size="small" onClick={() => updateParams({ "env.q": null, "env.filter": null })}>清除搜索和筛选</Button>
        </div>
        {grouped.length === 0 && <Empty description="没有匹配的配置项" />}
        {grouped.map((group) => (
          <div key={group.category.key} className="envx-result-group">
            <div className="envx-result-category">{group.category.icon} {group.category.label}</div>
            {sectionBlocks(group.list)}
          </div>
        ))}
      </div>
    );
  } else {
    body = (
      <>
        <div className="envx-category-head">
          <h2>{activeCategory.icon} {activeCategory.label}</h2>
          <Text type="secondary">{activeCategory.hint}</Text>
        </div>
        {category === MODELS_CATEGORY
          ? modelsView
          : categoryEntries.length
            ? sectionBlocks(categoryEntries)
            : <Empty description="这个分类下没有配置项" />}
      </>
    );
  }

  const dangers = dangerousChanges(changes);
  const showRestart = Boolean(query.data?.pendingRestart) || justSaved || restarting;

  return (
    <div className="envx" style={{ paddingBottom: totalChanges > 0 ? 96 : undefined }}>
      <PageHeader
        title="环境配置"
        subtitle={`编辑 ${query.data?.file ?? ".env"}（环境：${query.data?.environment ?? "—"}）；敏感值只能替换，不会回显。`}
        status={<SaveStatus dirty={totalChanges > 0} saving={saving} />}
        onRefresh={totalChanges === 0 ? query.reload : undefined}
        refreshing={query.loading}
        extra={(
          <Button type="primary" icon={<SaveOutlined />} disabled={totalChanges === 0 || errorCount > 0} loading={saving} onClick={openPreview}>
            保存{totalChanges > 0 ? ` ${totalChanges} 项` : ""}
          </Button>
        )}
      />
      {query.error && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {showRestart && (
        <RestartBanner supported={Boolean(query.data?.restartSupported)} restarting={restarting} onRestart={restart} />
      )}
      {query.data && <DiagnosticsPanel items={diagnostics} onLocate={locate} />}
      <div className="envx-toolbar">
        <Input
          ref={searchRef}
          className="envx-search"
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索配置名、中文名或说明（按 / 聚焦）"
          value={search}
          onChange={(event) => updateParams({ "env.q": event.target.value })}
          aria-label="搜索配置"
        />
        <Segmented
          className="envx-filter"
          value={filter}
          onChange={(value) => updateParams({ "env.filter": value === "all" ? null : String(value) })}
          options={FILTER_OPTIONS.map((item) => ({ value: item.value, label: item.label }))}
        />
        <Select
          className="envx-filter-select"
          value={filter}
          onChange={(value: EnvironmentFilter) => updateParams({ "env.filter": value === "all" ? null : value })}
          options={FILTER_OPTIONS}
          aria-label="筛选"
        />
      </div>
      <div className="envx-layout">
        <CategoryNav
          categories={ENV_CATEGORIES.filter((item) => item.key !== "other" || (stats.other?.total ?? 0) > 0)}
          active={resultsMode ? null : category}
          stats={stats}
          onSelect={(key) => updateParams({ "env.cat": key === MODELS_CATEGORY ? null : key, "env.q": null, "env.filter": null, "env.tab": null })}
        />
        <main className="envx-main">{body}</main>
      </div>
      {totalChanges > 0 && (
        <DirtySaveBar
          totalChanges={totalChanges}
          errorCount={errorCount}
          saving={saving}
          onDiscard={discardAll}
          onPreview={openPreview}
        />
      )}
      <EnvironmentDiffPreview
        open={previewOpen}
        rows={diffRows}
        dangers={dangers}
        saving={saving}
        onCancel={() => setPreviewOpen(false)}
        onConfirm={() => void save()}
      />
    </div>
  );
}
