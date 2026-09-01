import {
  Alert,
  App as AntApp,
  AutoComplete,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { AgentAuditTable } from "./agent-audit-table";
import { api, ApiError } from "./api";
import { nodeDisplayName, relationTypeColor } from "./relation-meta";
import {
  AdminEmpty,
  confirmDiscardChanges,
  DangerActionButton,
  formatTime,
  PageHeader,
  QueryErrorAlert,
  SaveStatus,
  TablePagination,
  useApiQuery,
  useUnsavedChanges,
} from "./shared";
import type {
  AgentAudit,
  AgentCapabilities,
  AgentConfig,
  AgentDebugMode,
  AgentDebugResponse,
  AgentDiagnostics,
  AgentExecutionTrace,
  AgentMemoryStatus,
  AgentMessageItem,
  AgentRelationGraph,
  AgentRelationItem,
  GroupSummary,
  MemoryItem,
  MemorySubjectItem,
  Persona,
  PersonaBehavior,
  PersonaEmotion,
  PersonaProfile,
  PersonaPreset,
  PrivacyItem,
} from "./types";

const { Text, Paragraph } = Typography;

const LazyRelationGraphView = lazy(() =>
  import("./relation-graph").then(({ RelationGraphView }) => ({ default: RelationGraphView })),
);

// 记忆类型标签：与后端 memory_type 口径对齐（summary/profile 为整理任务产出，core 为反复确认后晋升的不过期事实，manual 为运维手填）。
export const MEMORY_TYPE_META: Record<string, { label: string; color: string }> = {
  summary: { label: "群摘要", color: "geekblue" },
  profile: { label: "成员画像", color: "purple" },
  core: { label: "核心记忆", color: "red" },
  manual: { label: "置顶事实", color: "gold" },
};

export function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_META[type]?.label ?? type;
}

// 画像键中文标签：与后端 memory.py 的 _FACT_KEYS 对齐（多值键内容以「、」连接），
// 手工新增的自定义键原样展示。
export const PROFILE_KEY_META: Record<string, string> = {
  display_name: "昵称/自称",
  preferred_address: "偏好称呼",
  hobby: "爱好",
  preference: "偏好",
  skill: "技能",
  recurring_topic: "常聊话题",
};

export function profileKeyLabel(key: string): string {
  return PROFILE_KEY_META[key] ?? key;
}

type PersonaTraitKey =
  | "warmth"
  | "humor"
  | "directness"
  | "verbosity"
  | "expressiveness"
  | "sociability"
  | "followupTendency"
  | "reactionTendency";

interface PersonaFormValues {
  mode: "inherit" | "custom";
  profile: PersonaProfile;
}

const PERSONA_TRAIT_META: Record<PersonaTraitKey, { label: string; help: string; levels: string[] }> = {
  warmth: { label: "温和程度", help: "控制措辞的温度，不改变事实与安全边界。", levels: ["偏冷淡", "较克制", "自然", "温和", "很温暖"] },
  humor: { label: "幽默程度", help: "控制玩梗与轻松表达的频率。", levels: ["不玩梗", "偶尔", "适度", "会接梗", "很会接梗"] },
  directness: { label: "直接程度", help: "控制结论是委婉表达还是直接说明。", levels: ["很委婉", "偏委婉", "适中", "较直接", "很直接"] },
  verbosity: { label: "回复详略", help: "控制通常回复的展开程度；复杂问题仍可按需说明。", levels: ["极简", "简洁", "适中", "较详细", "很详细"] },
  expressiveness: { label: "表现力", help: "控制感叹、语气变化与情绪表现的明显程度。", levels: ["很淡", "克制", "自然", "明显", "很强"] },
  sociability: { label: "社交活跃度", help: "描述角色愿不愿意参与群聊；不会突破运行配置的主动发言上限。", levels: ["很少参与", "偏安静", "平衡", "较主动", "很活跃"] },
  followupTendency: { label: "续聊倾向", help: "控制回答后是否倾向自然延展话题。", levels: ["不续聊", "很少", "适度", "较愿意", "很愿意"] },
  reactionTendency: { label: "接梗 / 反应", help: "控制对群友玩笑、表情和气氛变化的回应倾向。", levels: ["几乎不接", "较少", "自然", "较常", "很爱接"] },
};

const PERSONA_STYLE_TRAITS: PersonaTraitKey[] = ["warmth", "humor", "directness", "verbosity", "expressiveness"];
const PERSONA_SOCIAL_TRAITS: PersonaTraitKey[] = ["sociability", "followupTendency", "reactionTendency"];

const PERSONA_TRIAL_SCENARIOS = [
  { value: "ordinary", label: "普通问题", mode: "dialogue", text: "今天适合做什么？" },
  { value: "joke", label: "群友玩梗", mode: "active", text: "你又来晚了，罚你讲个冷笑话。" },
  { value: "cold", label: "群聊冷场", mode: "warmup", text: "群里安静半天了。" },
  { value: "followup", label: "自然续聊", mode: "followup", text: "刚才的话题还有一点可以接。" },
  { value: "challenge", label: "成员质疑", mode: "dialogue", text: "你刚才是不是在瞎说？" },
  { value: "custom", label: "自定义", mode: "dialogue", text: "" },
] as const;

export function personaDraftSummary(profile: PersonaProfile, presets: PersonaPreset[]): string {
  const preset = presets.find((item) => item.id === profile.presetId);
  const meta = PERSONA_TRAIT_META;
  return [
    profile.name,
    preset?.label ?? profile.presetId,
    meta.warmth.levels[profile.warmth],
    meta.humor.levels[profile.humor],
    meta.verbosity.levels[profile.verbosity],
    meta.sociability.levels[profile.sociability],
  ].filter(Boolean).join(" · ");
}

export function mergePersonaPreset(profile: PersonaProfile, preset: PersonaPreset): PersonaProfile {
  return {
    ...profile,
    presetId: preset.id,
    identity: preset.identity,
    groupRole: preset.groupRole,
    warmth: preset.warmth,
    humor: preset.humor,
    directness: preset.directness,
    verbosity: preset.verbosity,
    expressiveness: preset.expressiveness,
    sociability: preset.sociability,
    followupTendency: preset.followupTendency,
    reactionTendency: preset.reactionTendency,
  };
}

export function personaBehaviorPreview(profile: PersonaProfile): PersonaBehavior {
  const probabilityScales = [0.15, 0.45, 0.75, 0.9, 1] as const;
  const maxTurns = [1, 2, 3, 4, 4] as const;
  const reactionModes = ["off", "restrained", "normal", "expressive", "high"] as const;
  const sociability = Math.max(0, Math.min(4, profile.sociability));
  const followup = Math.max(0, Math.min(4, profile.followupTendency));
  const reaction = Math.max(0, Math.min(4, profile.reactionTendency));
  return {
    source: "draft",
    sociability,
    followupTendency: followup,
    reactionTendency: reaction,
    warmupProbabilityScale: probabilityScales[sociability],
    activeProbabilityScale: probabilityScales[sociability],
    maxFollowupBotTurns: maxTurns[followup],
    allowSpontaneousReaction: reaction >= 2,
    reactionMode: reactionModes[reaction],
  };
}

export function personaEmotionExpressionPreview(
  emotion: PersonaEmotion,
  expressiveness: number,
): number {
  const scales = [0.2, 0.4, 0.62, 0.82, 1] as const;
  const level = Math.max(0, Math.min(4, Math.round(expressiveness)));
  return Math.max(0, Math.min(1, emotion.intensity * scales[level]));
}

// 画像成员的展示名：群名片优先、全局昵称兜底，解析失败回退 QQ 号（与关系图谱同口径）。
export function memberDisplayName(
  groupNickname: string | null | undefined,
  nickname: string | null | undefined,
  userId: string,
): string {
  return (groupNickname || nickname || "").trim() || userId;
}

export function debugMessageLabel(row: AgentMessageItem): string {
  const actor = (row.senderName || row.userId).trim();
  const text = row.text.trim() || "[媒体消息]";
  return `${actor} · ${text.slice(0, 52)}`;
}

// 关系类型与来源口径：与后端 memory.py 的枚举/别名表对齐，自定义类型原样展示。
export const RELATION_TYPE_PRESETS = ["好友", "死党", "情侣", "伴侣", "亲属", "师徒", "同事", "同学", "搭子", "对立"];
const RELATION_SOURCE_META: Record<string, { label: string; color: string }> = {
  manual: { label: "手工", color: "gold" },
  auto: { label: "自动", color: "default" },
  mention: { label: "提及", color: "blue" },
  agent: { label: "Agent", color: "green" },
};

const MEMORY_ROLE_OPTIONS = [
  { value: "", label: "全部角色" },
  { value: "member", label: "成员" },
  { value: "admin", label: "管理员" },
  { value: "owner", label: "群主" },
  { value: "bot", label: "Bot" },
];

export function AgentGroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const load = useCallback(() => api<GroupSummary[]>(`/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search]);
  const query = useApiQuery(load, { resources: ["agent_config"] });
  return <><PageHeader title="Agent 管理" subtitle="选择群组配置触发、人设、记忆和工具策略" onRefresh={query.reload} refreshing={query.refreshing} extra={<Input.Search placeholder="搜索群组" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table rowKey="groupId" loading={query.loading} dataSource={query.data?.rows ?? []} locale={{ emptyText: <AdminEmpty description="暂无可管理群组" /> }} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} columns={[{ title: "群组", render: (_, row: GroupSummary) => <>{row.groupName || "未命名群"}<br /><Text type="secondary">{row.groupId}</Text></> }, { title: "成员", dataIndex: "memberCount" }, { title: "状态", render: (_, row: GroupSummary) => <Tag color={row.agentEnabled ? "green" : "default"}>{row.agentEnabled ? "开启" : "关闭"}</Tag> }, { title: "操作", render: (_, row: GroupSummary) => <Link to={`/agent/${row.groupId}`}>进入管理</Link> }]} />
  }</Card></>;
}

export function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") ?? "overview";
  const changeTab = (key: string) => {
    if (!confirmDiscardChanges()) return;
    setSearchParams(key === "overview" ? {} : { tab: key }, { replace: true });
  };
  return <><PageHeader title={`Agent · ${groupId}`} subtitle="群级运行状态、配置、人设、记忆与数据治理" extra={<Link to="/agent">返回 Agent 列表</Link>} /><Tabs destroyOnHidden activeKey={tab} onChange={changeTab} items={[
    { key: "overview", label: "运行诊断", children: <AgentOverviewPanel groupId={groupId} /> },
    { key: "config", label: "运行配置", children: <AgentConfigPanel groupId={groupId} /> },
    { key: "persona", label: "人设", children: <PersonaPanel groupId={groupId} /> },
    { key: "memories", label: "记忆", children: <MemoriesPanel groupId={groupId} /> },
    { key: "profiles", label: "成员画像", children: <MemberProfilesPanel groupId={groupId} /> },
    { key: "relations", label: "关系边", children: <RelationsPanel groupId={groupId} /> },
    { key: "messages", label: "消息记录", children: <AgentMessagesPanel groupId={groupId} /> },
    { key: "debug", label: "对话调试", children: <AgentDebugPanel groupId={groupId} /> },
    { key: "privacy", label: "隐私退出", children: <PrivacyPanel groupId={groupId} /> },
    { key: "audit", label: "工具审计", children: <AgentAuditsPanel groupId={groupId} /> },
  ]} /></>;
}

const LLM_TASK_LABELS: Record<string, string> = {
  agent_dialogue: "群聊对话",
  agent_proactive: "主动发言",
  agent_memory: "记忆整理",
  agent_image: "图片理解",
};

function AgentOverviewPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const load = useCallback(
    () => api<AgentDiagnostics>(`/agent/groups/${groupId}/diagnostics`).then((r) => r.data),
    [groupId],
  );
  const loadCapabilities = useCallback(
    () => api<AgentCapabilities>(`/agent/groups/${groupId}/capabilities`).then((r) => r.data),
    [groupId],
  );
  const query = useApiQuery(load, { resources: ["agent_config", "agent_memory", "agent_group_data"] });
  const capabilityQuery = useApiQuery(loadCapabilities, { resources: ["agent_config"] });
  const [capabilityRefreshing, setCapabilityRefreshing] = useState(false);
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  const effective = data.effective;
  const memory = data.memory;
  const conversation = effective.shortConversation;
  const capabilities = capabilityQuery.data;
  const refreshCapabilities = async (): Promise<void> => {
    setCapabilityRefreshing(true);
    try {
      await api<AgentCapabilities>(`/agent/groups/${groupId}/capabilities/refresh`, { method: "POST" });
      await capabilityQuery.reload();
    } finally {
      setCapabilityRefreshing(false);
    }
  };
  return <Space orientation="vertical" size="large" style={{ width: "100%" }}>
    <Card
      title="实际生效配置"
      extra={<Button onClick={query.reload} loading={query.refreshing}>刷新诊断</Button>}
    >
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}><Statistic title="Agent" value={effective.enabled ? "开启" : "关闭"} /></Col>
        <Col xs={12} md={6}><Statistic title="叫名唤醒" value={effective.explicitWakeupEnabled ? "开启" : "关闭"} /></Col>
        <Col xs={12} md={6}><Statistic title="主动参与" value={effective.proactiveEnabled ? "开启" : "关闭"} /></Col>
        <Col xs={12} md={6}><Statistic title="今日主动额度" value={effective.dailyRemaining} suffix={`/ ${effective.dailyLimit}`} /></Col>
        <Col xs={12} md={6}><Statistic title="主动冷却剩余" value={effective.cooldownRemainingMinutes} suffix="分钟" /></Col>
      </Row>
      <Row gutter={[16, 16]} className="section-row">
        <Col xs={24} lg={8}>
          <Card size="small" title="主动参与">
            <Space orientation="vertical" size={6}>
              <Text>功能：<Tag color={effective.proactiveEnabled ? "green" : "default"}>{effective.proactiveEnabled ? "开启" : "关闭"}</Tag></Text>
              <Text>活跃聊天：{effective.proactiveActiveEnabled ? "允许自然加入" : "仅冷场参与"}</Text>
              <Text>上次参与：{formatTime(effective.lastProactiveAt)}</Text>
              <Text type="secondary">当前话题：{effective.activeTopic || "—"}</Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card size="small" title="短会话">
            <Space orientation="vertical" size={6}>
              <Text>功能：<Tag color={conversation.enabled ? "green" : "default"}>{conversation.enabled ? "开启" : "关闭"}</Tag></Text>
              <Text>运行：<Tag color={conversation.active ? "processing" : "default"}>{conversation.active ? "进行中" : conversation.enabled ? "空闲" : "停用"}</Tag></Text>
              <Text>Bot 回合：{conversation.botTurns}</Text>
              <Text>续聊评估：{conversation.evaluations}</Text>
              <Text type="secondary">话题：{conversation.topic || "—"}</Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card size="small" title="记忆整理">
            <Space orientation="vertical" size={6}>
              <Text>待整理消息：{memory.pendingMessages} 条</Text>
              <Text>连续失败：<Tag color={memory.consecutiveFailures > 0 ? "red" : "green"}>{memory.consecutiveFailures} 次</Tag></Text>
              <Text>运行：<Tag color={!memory.runtimeEnabled ? "default" : memory.inFlight ? "processing" : "green"}>{!memory.runtimeEnabled ? "已停用" : memory.inFlight ? "整理中" : "空闲"}</Tag></Text>
              <Text type="secondary">最近成功：{formatTime(memory.lastSuccessAt)}</Text>
            </Space>
          </Card>
        </Col>
      </Row>
    </Card>

    <Card title="为什么 Agent 现在可能不回复 / 不主动说话">
      {data.blockers.length === 0
        ? <Alert type="success" showIcon message="当前没有发现硬性阻塞" description="触发条件、LLM 路由、主动额度和记忆治理状态均未发现明确异常。" />
        : <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
          {data.blockers.map((item) => (
            <Alert
              key={item.code}
              type={item.severity}
              showIcon
              message={item.title}
              description={item.detail}
            />
          ))}
        </Space>}
      <Space wrap className="section-row">
        <Link to={`?tab=config`}>调整运行配置</Link>
        <Link to={`?tab=memories`}>检查记忆治理</Link>
        <Link to="/environment">检查 LLM Provider / 模型路由</Link>
      </Space>
    </Card>

    <Card
      title="OneBot 协议能力"
      extra={<Button onClick={() => void refreshCapabilities()} loading={capabilityRefreshing}>清缓存并重新探测</Button>}
    >
      {capabilityQuery.error
        ? <QueryErrorAlert error={capabilityQuery.error} onRetry={capabilityQuery.reload} />
        : !capabilities
          ? <Spin />
          : <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
            <Space wrap>
              <Tag color={capabilities.offline ? "default" : capabilities.action.degraded ? "orange" : "green"}>
                {capabilities.offline ? "Bot 离线" : capabilities.action.degraded ? "探测降级" : `角色 ${capabilities.action.role || "unknown"}`}
              </Tag>
              <Text>Actions：{capabilities.action.actions.length}</Text>
              <Text>Segments：{capabilities.segments.filter((item) => item.exposed).length} 可暴露</Text>
              {capabilities.action.lastError && <Text type="danger">最近探测：{capabilities.action.lastError}</Text>}
            </Space>
            <Space wrap>
              {capabilities.segments.map((item) => (
                <Tag
                  key={item.type}
                  color={item.forbidden ? "red" : item.runtimeUnsupported ? "orange" : item.exposed ? "green" : "default"}
                >
                  {item.type}{item.runtimeUnsupported ? ` · 降级 ${Math.ceil(item.retryAfterSeconds ?? 0)}s` : ""}
                </Tag>
              ))}
            </Space>
            {capabilities.segments.some((item) => item.runtimeUnsupported) && (
              <Alert
                type="warning"
                showIcon
                message="存在运行时降级能力"
                description={capabilities.segments
                  .filter((item) => item.runtimeUnsupported)
                  .map((item) => `${item.type}: ${item.lastFailureReason || "后端不兼容"}`)
                  .join("；")}
              />
            )}
          </Space>}
    </Card>

    <Card title="LLM 实际路由" extra={data.llm.unconfiguredProviders.length > 0 ? <Tag color="red">Provider 未配置</Tag> : <Tag color="green">路由可用</Tag>}>
      <List
        dataSource={data.llm.routes}
        locale={{ emptyText: <AdminEmpty description="暂无 LLM 路由" /> }}
        renderItem={(route) => (
          <List.Item>
            <List.Item.Meta
              title={<Space><Text strong>{LLM_TASK_LABELS[route.task] ?? route.task}</Text><Tag>{route.profile}</Tag></Space>}
              description={`${route.provider} · ${route.model || "未配置模型"} · thinking=${route.thinking}`}
            />
            <Tag color={route.configured ? "green" : "red"}>{route.configured ? "可用" : "不可用"}</Tag>
          </List.Item>
        )}
      />
    </Card>
  </Space>;
}

type ParticipationIntensity = "restrained" | "balanced" | "active" | "custom";

const PARTICIPATION_PRESETS: Record<Exclude<ParticipationIntensity, "custom">, { warmup: number; interject: number }> = {
  restrained: { warmup: 0.18, interject: 0.10 },
  balanced: { warmup: 0.35, interject: 0.25 },
  active: { warmup: 0.55, interject: 0.45 },
};

function participationIntensity(warmup: number, interject: number): ParticipationIntensity {
  const entry = Object.entries(PARTICIPATION_PRESETS).find(([, preset]) =>
    Math.abs(preset.warmup - warmup) < 0.001 && Math.abs(preset.interject - interject) < 0.001,
  );
  return (entry?.[0] as ParticipationIntensity | undefined) ?? "custom";
}

function AgentConfigPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [saving, setSaving] = useState(false); const [dirty, setDirty] = useState(false);
  const load = useCallback(() => api<AgentConfig>(`/agent/groups/${groupId}/config`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_config"] });
  const watchedProactiveEnabled = Form.useWatch("proactiveEnabled", form) as boolean | undefined;
  const watchedWarmupProbability = Form.useWatch("proactiveProbability", form) as number | undefined;
  const watchedInterjectProbability = Form.useWatch("proactiveActiveProbability", form) as number | undefined;
  useUnsavedChanges(dirty);
  useEffect(() => { if (query.data) { form.setFieldsValue(query.data); setDirty(false); } }, [form, query.data]);
  const save = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      const result = await api<AgentConfig>(`/agent/groups/${groupId}/config`, { method: "PATCH", body: JSON.stringify({ ...values, version: query.data?.version }) });
      form.setFieldsValue(result.data);
      setDirty(false);
      message.success("Agent 配置已保存");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); query.reload(); } else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  const intensity = participationIntensity(
    watchedWarmupProbability ?? data.proactiveProbability,
    watchedInterjectProbability ?? data.proactiveActiveProbability,
  );
  const setParticipationIntensity = (value: string | number): void => {
    const preset = PARTICIPATION_PRESETS[String(value) as Exclude<ParticipationIntensity, "custom">];
    if (!preset) return;
    form.setFieldsValue({
      proactiveProbability: preset.warmup,
      proactiveActiveProbability: preset.interject,
    });
    setDirty(true);
  };
  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={save}
      onValuesChange={() => setDirty(true)}
      className="agent-config-form"
    >
      <div className="agent-config-page agent-studio-page agent-studio-runtime">
        <section className="agent-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="agent-config-hero-copy">
            <div className="agent-config-eyebrow">GROUP AGENT</div>
            <div className="agent-config-title-row">
              <div>
                <h2>群级运行配置</h2>
                <p>控制 Agent 在这个群里的响应方式、主动行为、记忆边界与管理能力。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="agent-config-metrics">
              <div className="agent-config-metric">
                <span>今日主动发言</span>
                <strong>{data.proactiveToday}</strong>
              </div>
              <div className="agent-config-metric">
                <span>今日管理工具</span>
                <strong>{data.adminToolsToday}</strong>
              </div>
              <div className="agent-config-metric">
                <span>今日高风险工具</span>
                <strong>{data.criticalToolsToday}</strong>
              </div>
              <div className="agent-config-metric agent-config-metric-wide">
                <span>配置范围</span>
                <strong>仅当前群</strong>
              </div>
            </div>
          </div>

          <div className="agent-master-card">
            <div className="agent-master-copy">
              <div className="agent-master-label">总开关</div>
              <div className="agent-master-title">启用 Agent</div>
              <div className="agent-master-description">
                关闭后停止群聊响应、主动发言、短会话以及自动消息采集和记忆整理；子配置会保留。
              </div>
            </div>
            <Form.Item name="enabled" valuePropName="checked" noStyle>
              <Switch size="default" />
            </Form.Item>
          </div>
        </section>

        <div className="agent-config-layout">
          <div className="agent-config-main">
            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">CONVERSATION</div>
                  <h3>聊天参与</h3>
                  <p>@ Agent 始终会回应；这里只控制额外的自然交互能力。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <div className="agent-config-toggle-card">
                  <div>
                    <div className="agent-config-toggle-title">叫名字也回应</div>
                    <div className="agent-config-toggle-help">不必 @，直接叫 Agent 名字或常用唤醒词也可以开始对话。</div>
                  </div>
                  <Form.Item name="explicitWakeupEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
                <div className="agent-config-toggle-card">
                  <div>
                    <div className="agent-config-toggle-title">自然续聊</div>
                    <div className="agent-config-toggle-help">Bot 回复后，可在同一话题中继续自然接话。</div>
                  </div>
                  <Form.Item name="shortConversationEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
                <div className="agent-config-toggle-card agent-config-toggle-card-featured">
                  <div>
                    <div className="agent-config-toggle-title">主动参与群聊</div>
                    <div className="agent-config-toggle-help">没人直接叫它时，也允许根据群聊上下文适时暖场或加入话题。</div>
                  </div>
                  <Form.Item name="proactiveEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
              </div>
            </section>

            {(watchedProactiveEnabled ?? data.proactiveEnabled) && <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">PARTICIPATION</div>
                  <h3>主动参与策略</h3>
                  <p>Agent 自动根据群聊状态选择冷场暖场或加入正在进行的话题；普通配置只需要控制参与强度和边界。</p>
                </div>
              </div>
              <div className="agent-config-toggle-card agent-config-toggle-card-featured">
                <div>
                  <div className="agent-config-toggle-title">参与强度</div>
                  <div className="agent-config-toggle-help">同时调整暖场和加入话题的积极程度，不需要分别理解两套概率。</div>
                </div>
                <Segmented
                  value={intensity}
                  onChange={setParticipationIntensity}
                  options={[
                    { value: "restrained", label: "克制" },
                    { value: "balanced", label: "平衡" },
                    { value: "active", label: "活跃" },
                    ...(intensity === "custom" ? [{ value: "custom", label: "自定义", disabled: true }] : []),
                  ]}
                />
              </div>
              <div className="agent-config-toggle-card">
                <div>
                  <div className="agent-config-toggle-title">群聊活跃时也允许加入</div>
                  <div className="agent-config-toggle-help">关闭后仍可在冷场时自然暖场，但不会加入群友正在进行的聊天。</div>
                </div>
                <Form.Item name="proactiveActiveEnabled" valuePropName="checked" noStyle>
                  <Switch />
                </Form.Item>
              </div>
              <div className="agent-config-grid agent-config-grid-3">
                <Form.Item name="idleThresholdMinutes" label="冷场判定" extra="安静多久后才考虑主动暖场（分钟）">
                  <InputNumber min={1} max={10080} />
                </Form.Item>
                <Form.Item name="cooldownMinutes" label="参与冷却" extra="两次主动参与之间至少间隔多少分钟">
                  <InputNumber min={0} max={10080} />
                </Form.Item>
                <Form.Item name="dailyLimit" label="每日参与上限" extra="达到后当天停止自动参与">
                  <InputNumber min={0} max={1000} />
                </Form.Item>
              </div>
              <details className="agent-debug-details">
                <summary>精细调节（高级）</summary>
                <div className="agent-config-grid agent-config-grid-3 section-row">
                  <Form.Item name="proactiveProbability" label="暖场基础概率" extra="满足冷场条件后的基础概率">
                    <InputNumber min={0} max={1} step={0.05} />
                  </Form.Item>
                  <Form.Item name="proactiveActiveProbability" label="加入话题概率" extra="活跃聊天进入候选后的参与概率">
                    <InputNumber min={0} max={1} step={0.02} />
                  </Form.Item>
                  <Form.Item name="proactiveActiveWindowMinutes" label="活跃话题窗口" extra="最近多少分钟的真人消息算作活跃话题">
                    <InputNumber min={1} max={1440} />
                  </Form.Item>
                </div>
              </details>
            </section>}

            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">MEMORY & MEDIA</div>
                  <h3>记忆与媒体</h3>
                  <p>设置原始消息保留周期、跨群记忆范围和媒体缓存策略。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="rawRetentionDays" label="原始消息保留" extra="到期后按记忆治理策略清理（天）">
                  <InputNumber min={1} max={365} />
                </Form.Item>
                <Form.Item name="crossGroupVisibility" label="跨群记忆">
                  <Select
                    options={[
                      { value: "isolated", label: "群隔离" },
                      { value: "public_summary", label: "共享低风险公开摘要" },
                    ]}
                  />
                </Form.Item>
              </div>
              <div className="agent-config-toggle-card">
                <div>
                  <div className="agent-config-toggle-title">媒体缓存</div>
                  <div className="agent-config-toggle-help">缓存图片理解结果，减少重复识图调用；关闭不会影响普通文字对话。</div>
                </div>
                <Form.Item name="mediaCacheEnabled" valuePropName="checked" noStyle>
                  <Switch />
                </Form.Item>
              </div>
            </section>

            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">TOOLS</div>
                  <h3>特权工具权限</h3>
                  <p>限制 Agent 可以调用的高副作用能力，以及每天的调用额度。读取、记忆写入和普通消息发送不占此额度。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="adminToolDailyLimit" label="每日特权工具上限" extra="群管理操作和群文件发送共用此额度">
                  <InputNumber min={1} max={1000} />
                </Form.Item>
                <Form.Item name="criticalToolDailyLimit" label="每日高风险工具上限" extra="踢人、管理员变更、全员禁言和破坏性群文件操作单独计数">
                  <InputNumber min={1} max={100} />
                </Form.Item>
                <Form.Item name="toolAllowlist" label="允许的特权工具">
                  <Select
                    mode="multiple"
                    placeholder="未选择时不允许调用特权工具"
                    options={[
                      { value: "mute_member", label: "禁言成员" },
                      { value: "create_group_announcement", label: "发布群公告" },
                      { value: "set_essence_message", label: "设置精华消息" },
                      { value: "remove_essence_message", label: "移出精华消息" },
                      { value: "delete_group_notice", label: "删除群公告" },
                      { value: "set_group_card", label: "修改群名片" },
                      { value: "set_special_title", label: "设置专属头衔" },
                      { value: "set_group_name", label: "修改群名称" },
                      { value: "create_group_folder", label: "创建群文件夹" },
                      { value: "send_file", label: "发送群文件" },
                      { value: "kick_member", label: "高风险 · 踢出成员" },
                      { value: "set_whole_group_mute", label: "高风险 · 全员禁言" },
                      { value: "set_group_admin", label: "高风险 · 设置管理员" },
                      { value: "delete_group_file", label: "高风险 · 删除群文件" },
                      { value: "move_group_file", label: "高风险 · 移动群文件" },
                      { value: "rename_group_file", label: "高风险 · 重命名群文件" },
                      { value: "delete_group_folder", label: "高风险 · 删除群文件夹" },
                    ]}
                  />
                </Form.Item>
              </div>
            </section>
          </div>

          <aside className="agent-config-aside">
            <div className="agent-config-note-card liquid-glass agent-config-floating">
              <div className="agent-config-note-title">配置说明</div>
              <p>总开关只控制运行状态，不会清空这里的参数、已有记忆或人设。</p>
              <p>因此你可以先关闭 Agent，再安全调整各项策略，最后重新开启。</p>
            </div>
            <div className="agent-config-note-card agent-config-note-soft liquid-glass agent-config-floating">
              <div className="agent-config-note-title">推荐顺序</div>
              <ol>
                <li>先选择需要的聊天参与能力</li>
                <li>需要时再调整主动参与频率</li>
                <li>确认记忆边界</li>
                <li>最后开放管理工具</li>
              </ol>
            </div>
          </aside>
        </div>

        <div className="agent-config-savebar liquid-glass agent-config-floating">
          <div>
            <strong>{dirty ? "有未保存的修改" : "配置已同步"}</strong>
            <span>{dirty ? "保存后立即按新策略运行" : "修改任意选项后可统一保存"}</span>
          </div>
          <Button type="primary" htmlType="submit" size="large" loading={saving} disabled={!dirty}>
            保存配置
          </Button>
        </div>
      </div>
    </Form>
  );
}

function PersonaPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<PersonaFormValues>();
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [trialActorUserId, setTrialActorUserId] = useState("");
  const [trialScenario, setTrialScenario] = useState("ordinary");
  const [trialCustomText, setTrialCustomText] = useState("");
  const [trialRunModel, setTrialRunModel] = useState(false);
  const [trialRunning, setTrialRunning] = useState(false);
  const [trialResult, setTrialResult] = useState<AgentDebugResponse | null>(null);
  const [trialError, setTrialError] = useState<string | null>(null);
  const watchedMode = Form.useWatch("mode", form) as PersonaFormValues["mode"] | undefined;
  const watchedProfile = Form.useWatch("profile", form) as PersonaProfile | undefined;
  const load = useCallback(() => api<Persona>(`/agent/groups/${groupId}/persona`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_persona"] });
  useUnsavedChanges(dirty);

  useEffect(() => {
    if (query.data) {
      form.setFieldsValue({
        mode: query.data.enabled ? "custom" : "inherit",
        profile: query.data.profile,
      });
      setDirty(false);
      setTrialResult(null);
      setTrialError(null);
    }
  }, [form, query.data]);

  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;

  const profile = watchedProfile ?? data.profile;
  const mode = watchedMode ?? (data.enabled ? "custom" : "inherit");
  const draftSummary = personaDraftSummary(profile, data.presets);
  const draftBehavior = personaBehaviorPreview(profile);
  const draftEmotionExpression = personaEmotionExpressionPreview(data.emotion, profile.expressiveness);
  const selectedPreset = data.presets.find((item) => item.id === profile.presetId) ?? data.presets[0];

  const applyPreset = (presetId: string) => {
    const preset = data.presets.find((item) => item.id === presetId);
    if (!preset) return;
    const current = form.getFieldValue("profile") as PersonaProfile;
    form.setFieldsValue({ profile: mergePersonaPreset(current, preset) });
    setDirty(true);
    setTrialResult(null);
  };

  const save = async (values: PersonaFormValues) => {
    setSaving(true);
    try {
      await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "PUT",
        body: JSON.stringify({
          version: data.version,
          enabled: values.mode === "custom",
          profile: values.profile,
        }),
      });
      setDirty(false);
      message.success(values.mode === "custom" ? "当前群人设已保存" : "已切换为跟随全局人设");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        message.warning(error.message);
        query.reload();
      } else message.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setResetting(true);
    try {
      await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "DELETE",
        headers: data.version ? { "If-Match": data.version } : {},
      });
      setDirty(false);
      message.success("已清除当前群自定义并恢复全局人设");
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setResetting(false);
    }
  };

  const runTrial = async () => {
    const actorId = Number(trialActorUserId.trim());
    if (!Number.isInteger(actorId) || actorId <= 0) {
      message.warning("请填写当前群中的成员 QQ 号作为试演发言人");
      return;
    }
    const scenario = PERSONA_TRIAL_SCENARIOS.find((item) => item.value === trialScenario);
    const trialText = (trialScenario === "custom" ? trialCustomText : scenario?.text ?? "").trim();
    if (!trialText) {
      message.warning("请输入试演消息");
      return;
    }
    const draft = form.getFieldValue("profile") as PersonaProfile;
    setTrialRunning(true);
    setTrialError(null);
    try {
      const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, {
        method: "POST",
        body: JSON.stringify({
          mode: scenario?.mode ?? "dialogue",
          actorUserId: actorId,
          text: trialText,
          runModel: trialRunModel,
          personaDraft: draft,
        }),
      });
      setTrialResult(response.data);
      message.success(trialRunModel ? "草稿真实模型试演完成，无副作用" : "草稿 Prompt 快照已生成");
    } catch (error) {
      setTrialError((error as Error).message);
    } finally {
      setTrialRunning(false);
    }
  };

  const renderTrait = (key: PersonaTraitKey) => {
    const meta = PERSONA_TRAIT_META[key];
    const value = Number(profile[key] ?? 0);
    return (
      <div className="persona-trait-card" key={key}>
        <div className="persona-trait-head">
          <div>
            <strong>{meta.label}</strong>
            <span>{meta.help}</span>
          </div>
          <Tag>{value}/4 · {meta.levels[value]}</Tag>
        </div>
        <Form.Item name={["profile", key]} noStyle>
          <Segmented
            block
            options={meta.levels.map((label, level) => ({ value: level, label: `${level} ${label}` }))}
          />
        </Form.Item>
      </div>
    );
  };

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={save}
      onValuesChange={() => { setDirty(true); setTrialResult(null); }}
      className="persona-config-form"
    >
      <div className="persona-config-page agent-studio-page agent-studio-persona">
        <section className="persona-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="persona-config-hero-copy">
            <div className="persona-config-eyebrow">PERSONA STUDIO · V2</div>
            <div className="persona-config-title-row">
              <div>
                <h2>当前群人设</h2>
                <p>先选一个角色模板，再用少量可理解的特征微调。事实、隐私、权限与工具安全不属于人设，始终由系统策略强制执行。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="persona-config-metrics">
              <div className="persona-config-metric"><span>当前模板</span><strong>{selectedPreset?.label ?? "自然群友"}</strong></div>
              <div className="persona-config-metric"><span>说话风格</span><strong>{PERSONA_TRAIT_META.warmth.levels[profile.warmth]}</strong></div>
              <div className="persona-config-metric"><span>社交倾向</span><strong>{PERSONA_TRAIT_META.sociability.levels[profile.sociability]}</strong></div>
              <div className="persona-config-metric persona-config-metric-wide"><span>模式</span><strong>{mode === "custom" ? "当前群自定义" : "跟随全局"}</strong></div>
            </div>
          </div>

          <div className="persona-master-card is-enabled">
            <div className="persona-master-copy">
              <div className="persona-master-label">生效模式</div>
              <div className="persona-master-title">{mode === "custom" ? "使用当前群自定义" : "跟随全局人设"}</div>
              <div className="persona-master-description">切换为“跟随全局”不会删除当前草稿；再次切回自定义时可以继续编辑。恢复默认会真正清空当前群人设。</div>
            </div>
            <Form.Item name="mode" noStyle>
              <Segmented options={[{ value: "inherit", label: "跟随全局" }, { value: "custom", label: "当前群自定义" }]} />
            </Form.Item>
          </div>
        </section>

        {mode === "inherit" && (
          <Alert
            className="section-alert"
            type="info"
            showIcon
            message="当前 Agent 正在跟随全局人设"
            description="你仍然可以编辑和试演下面的草稿；只有切换到“当前群自定义”并保存后，草稿才会参与真实群聊。"
          />
        )}

        <div className="persona-config-layout">
          <div className="persona-config-main">
            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">PRESET</div><h3>角色模板</h3><p>模板给出一套完整起点，选择后仍可继续微调；不会改变系统安全规则。</p></div>
                <Tag>{data.presets.length} 个模板</Tag>
              </div>
              <div className="persona-preset-grid">
                {data.presets.map((preset) => (
                  <button
                    type="button"
                    key={preset.id}
                    className={`persona-preset-card${profile.presetId === preset.id ? " is-selected" : ""}`}
                    onClick={() => applyPreset(preset.id)}
                  >
                    <strong>{preset.label}</strong>
                    <span>{preset.description}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">IDENTITY</div><h3>它是谁</h3><p>这里只保留真正需要文字表达的身份信息，不要求你写 Prompt。</p></div>
              </div>
              <Row gutter={[16, 0]}>
                <Col xs={24} md={8}><Form.Item name={["profile", "name"]} label="名字" rules={[{ required: true, message: "请输入名字" }]}><Input maxLength={64} showCount /></Form.Item></Col>
                <Col xs={24} md={16}><Form.Item name={["profile", "groupRole"]} label="群内角色"><Input maxLength={240} showCount placeholder="例如：普通群友" /></Form.Item></Col>
              </Row>
              <Form.Item name={["profile", "identity"]} label="身份定位"><Input.TextArea maxLength={240} showCount autoSize={{ minRows: 2, maxRows: 5 }} placeholder="例如：熟悉群聊节奏、自然简洁的普通群友" /></Form.Item>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">VOICE</div><h3>怎么说话</h3><p>用 0–4 档位微调常见表达特征，避免自由文本互相打架。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_STYLE_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">SOCIAL</div><h3>怎么参与群聊</h3><p>这些倾向会直接参与主动候选、短会话续聊和主动 reaction 决策；“运行设置”的开关、概率、冷却和每日上限始终是硬边界。</p></div>
              </div>
              <div className="persona-trait-grid">{PERSONA_SOCIAL_TRAITS.map(renderTrait)}</div>
            </section>

            <section className="persona-config-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">NOTES</div><h3>自定义补充</h3><p>只写模板和档位无法表达的角色细节。不要在这里重复隐私、知识或工具安全规则。</p></div>
              </div>
              <Form.Item name={["profile", "customNotes"]}>
                <Input.TextArea maxLength={240} showCount autoSize={{ minRows: 3, maxRows: 6 }} placeholder="例如：喜欢偶尔用“好困”自嘲，但不要每条消息都提。" />
              </Form.Item>
            </section>

            <section className="persona-config-section persona-trial-section liquid-glass agent-config-floating">
              <div className="persona-config-section-head">
                <div><div className="persona-config-section-kicker">TRY IT</div><h3>未保存草稿试演</h3><p>直接用当前表单草稿调用同一套 Agent Debug。不会保存人设、不会执行工具、不会发 QQ、不会写记忆或主动状态。</p></div>
                <Tag color="blue">personaDraft</Tag>
              </div>
              <Row gutter={[16, 16]}>
                <Col xs={24} md={8}>
                  <Space orientation="vertical" size={6} style={{ width: "100%" }}>
                    <Text strong>试演成员 QQ 号</Text>
                    <Input value={trialActorUserId} onChange={(event) => setTrialActorUserId(event.target.value)} placeholder="必须是当前群成员" />
                  </Space>
                </Col>
                <Col xs={24} md={16}>
                  <Space orientation="vertical" size={6} style={{ width: "100%" }}>
                    <Text strong>场景</Text>
                    <Select value={trialScenario} onChange={setTrialScenario} options={PERSONA_TRIAL_SCENARIOS.map((item) => ({ value: item.value, label: item.label }))} style={{ width: "100%" }} />
                  </Space>
                </Col>
              </Row>
              {trialScenario === "custom" && <Input.TextArea value={trialCustomText} onChange={(event) => setTrialCustomText(event.target.value)} maxLength={4000} showCount autoSize={{ minRows: 3, maxRows: 7 }} placeholder="输入自定义试演消息" style={{ marginTop: 16 }} />}
              {trialScenario !== "custom" && <Alert style={{ marginTop: 16 }} type="info" showIcon message={PERSONA_TRIAL_SCENARIOS.find((item) => item.value === trialScenario)?.text} />}
              <div className="agent-debug-run-row persona-trial-run-row">
                <Space wrap>
                  <Switch checked={trialRunModel} onChange={setTrialRunModel} />
                  <div><Text strong>{trialRunModel ? "调用真实模型" : "仅构建 Prompt"}</Text><br /><Text type="secondary">真实模型也仍然是 dry-run，不执行任何工具或发送动作。</Text></div>
                </Space>
                <Button type="primary" onClick={runTrial} loading={trialRunning}>{trialRunModel ? "试演草稿" : "生成草稿快照"}</Button>
              </div>
              {trialError && <QueryErrorAlert error={trialError} onRetry={runTrial} />}
              {trialResult && (
                <Card size="small" className="persona-trial-result" title="试演结果" extra={<Tag color="purple">{trialResult.persona.source === "draft" ? "未保存草稿" : "已保存人设"}</Tag>}>
                  <Descriptions size="small" column={{ xs: 1, md: 2 }} items={[
                    { key: "saved", label: "当前已保存", children: trialResult.persona.persistedSummary },
                    { key: "draft", label: "本次草稿", children: personaDraftSummary(trialResult.persona.appliedProfile, data.presets) },
                    { key: "behavior", label: "本次行为", children: `主动候选 ×${trialResult.persona.appliedBehavior.activeProbabilityScale.toFixed(2)} · 自动续聊 ${Math.max(0, trialResult.persona.appliedBehavior.maxFollowupBotTurns - 1)} 次 · 主动 reaction ${trialResult.persona.appliedBehavior.allowSpontaneousReaction ? "允许" : "关闭"}` },
                    { key: "emotion", label: "动态情绪", children: `${trialResult.persona.appliedEmotion.displayLabel} · 状态强度 ${Math.round(trialResult.persona.appliedEmotion.intensity * 100)}% · 表达 ${Math.round(trialResult.persona.appliedEmotion.expressionIntensity * 100)}%` },
                    { key: "prompt", label: "Prompt", children: `${trialResult.promptVersion} · ${trialResult.promptMessages.length} 条消息` },
                    { key: "sideEffect", label: "副作用", children: "0（工具、发送、状态写入均跳过）" },
                  ]} />
                  <Paragraph style={{ marginTop: 14, marginBottom: 0, whiteSpace: "pre-wrap" }}>
                    {trialResult.result?.text || (trialRunModel ? "（模型没有返回文本，可能只产生了工具意图）" : "已生成 Prompt 快照；开启“调用真实模型”可查看实际回答。")}
                  </Paragraph>
                </Card>
              )}
            </section>
          </div>

          <aside className="persona-config-aside">
            <div className="persona-note-card liquid-glass agent-config-floating">
              <div className="persona-note-title">当前草稿</div>
              <strong className="persona-draft-summary">{draftSummary}</strong>
              <p>{profile.identity}</p>
              <div className="persona-summary-tags">
                <Tag>{PERSONA_TRAIT_META.directness.levels[profile.directness]}</Tag>
                <Tag>{PERSONA_TRAIT_META.expressiveness.levels[profile.expressiveness]}</Tag>
                <Tag>{PERSONA_TRAIT_META.followupTendency.levels[profile.followupTendency]}</Tag>
                <Tag>{PERSONA_TRAIT_META.reactionTendency.levels[profile.reactionTendency]}</Tag>
              </div>
            </div>
            <div className="persona-note-card persona-behavior-card liquid-glass agent-config-floating">
              <div className="persona-note-title">实际行为影响</div>
              <p>主动候选：现有运行策略 × {draftBehavior.activeProbabilityScale.toFixed(2)}。Persona 只能收窄，不能突破运行配置。</p>
              <p>自动续聊：{draftBehavior.maxFollowupBotTurns <= 1 ? "首轮回复后结束" : `最多再续 ${draftBehavior.maxFollowupBotTurns - 1} 次`}。</p>
              <p>主动 reaction：{draftBehavior.allowSpontaneousReaction ? `允许 · ${draftBehavior.reactionMode}` : "关闭（明确用户请求不受影响）"}。</p>
            </div>
            <div className="persona-note-card persona-emotion-card liquid-glass agent-config-floating">
              <div className="persona-note-title">动态情绪</div>
              <Space size={8} wrap>
                <Tag>{data.emotion.displayLabel}</Tag>
                <Text type="secondary">状态强度 {Math.round(data.emotion.intensity * 100)}%</Text>
              </Space>
              <Progress percent={Math.round(draftEmotionExpression * 100)} size="small" showInfo={false} />
              <p>{data.emotion.reason || "近期没有明显情绪事件，保持 Persona 的基础气质。"}</p>
              <p>{data.emotion.expressionHint}</p>
              {data.emotion.updatedAt && (
                <Text type="secondary">约 {data.emotion.ageMinutesBucket} 分钟前更新 · 会自动衰减回平静</Text>
              )}
            </div>
            <div className="persona-note-card persona-note-soft liquid-glass agent-config-floating">
              <div className="persona-note-title">已保存版本</div>
              <p>{data.summary}</p>
              {dirty && <Tag color="orange">草稿尚未保存</Tag>}
            </div>
            <div className="persona-note-card persona-note-soft liquid-glass agent-config-floating">
              <div className="persona-note-title">系统策略不可覆盖</div>
              <p>事实性、隐私、权限、工具能力和 Prompt 注入防护永远不由人设控制。人设只决定“像谁、怎么说、倾向怎么参与”。</p>
            </div>
          </aside>
        </div>

        <div className="persona-config-savebar liquid-glass agent-config-floating">
          <div className="persona-save-state">
            <strong>{dirty ? "当前人设有未保存草稿" : "人设配置已同步"}</strong>
            <span>{dirty ? draftSummary : data.summary}</span>
          </div>
          <Space>
            <Popconfirm
              title="恢复全局人设？"
              description="会真正清空当前群 Persona v2 自定义资料并恢复全局默认。"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={reset}
            >
              <Button loading={resetting}>恢复全局</Button>
            </Popconfirm>
            <Button type="primary" htmlType="submit" size="large" loading={saving} disabled={!dirty || resetting}>
              保存人设
            </Button>
          </Space>
        </div>
      </div>
    </Form>
  );
}

// 记忆表单的可编辑字段；expiresInDays 为空表示永久有效。
interface MemoryFormValues {
  content: string;
  salience: number;
  confidence: number;
  expiresInDays: number | null;
}

// 记忆编辑抽屉：记忆表格与成员画像面板共用，只改内容/权重/置信度/有效期。
function MemoryEditDrawer({ memory, saving, onClose, onSave }: { memory: MemoryItem | null; saving: boolean; onClose: () => void; onSave: (values: MemoryFormValues) => void }): React.JSX.Element {
  const [form] = Form.useForm();
  useEffect(() => {
    if (memory) {
      form.setFieldsValue({
        content: memory.content,
        salience: memory.salience,
        confidence: memory.confidence,
        expiresInDays: memory.expiresAt ? Math.max(1, Math.ceil((new Date(memory.expiresAt).getTime() - Date.now()) / 86400000)) : null,
      });
    }
  }, [form, memory]);
  return <Drawer open={!!memory} width={520} title={`编辑记忆 · ${memory?.key ?? ""}`} onClose={onClose}>
    <Form form={form} layout="vertical" onFinish={(values) => onSave(values as MemoryFormValues)}>
      <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount /></Form.Item>
      <Row gutter={16}>
        <Col span={12}><Form.Item name="salience" label="显著度（注入优先级）" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
        <Col span={12}><Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
      </Row>
      <Form.Item name="expiresInDays" label="有效期（天）"><InputNumber min={1} max={3650} placeholder="清空则永久有效" style={{ width: "100%" }} /></Form.Item>
      <Space><Button type="primary" htmlType="submit" loading={saving}>保存</Button><Button onClick={onClose}>取消</Button></Space>
    </Form>
  </Drawer>;
}

export function MemoriesPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm();
  const load = useCallback(() => api<MemoryItem[]>(`/agent/groups/${groupId}/memories?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search]);
  const query = useApiQuery(load, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const statusLoad = useCallback(
    () => readOnly
      ? Promise.resolve(null)
      : api<AgentMemoryStatus>(`/agent/groups/${groupId}/memories/status`).then((r) => r.data),
    [groupId, readOnly],
  );
  const statusQuery = useApiQuery(statusLoad, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const status = readOnly ? null : statusQuery.data;
  const remove = async (id: string) => { if (readOnly) return; await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" }); message.success("记忆已删除"); query.reload(); };
  const removeMember = async (userId: string) => { if (readOnly) return; const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/members/${userId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条成员数据`); query.reload(); };
  const removeGroup = async () => { if (readOnly) return; const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条群 Agent 数据`); query.reload(); };
  const exportData = async () => { if (readOnly) return; const result = await api(`/agent/groups/${groupId}/memories/export`); const blob = new Blob([JSON.stringify(result.data, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `yawnbot-agent-${groupId}.json`; anchor.click(); URL.revokeObjectURL(url); };
  const compact = async () => {
    if (readOnly) return;
    try {
      await api(`/agent/groups/${groupId}/memories/compact`, { method: "POST" });
      message.success("整理已在后台启动，完成后这里会自动刷新");
      // 整理含 LLM 摘要可达数十秒；延迟一轮再拉状态，配合 entity.changed 事件兜底。
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 8000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const rebuild = async () => {
    if (readOnly) return;
    try {
      await api(`/agent/groups/${groupId}/memories/rebuild`, { method: "POST" });
      message.success("派生记忆重建已启动，手工记忆会保留");
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 3000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const openEdit = (row: MemoryItem) => { if (!readOnly) setEditing(row); };
  const saveEdit = async (values: MemoryFormValues) => {
    if (readOnly || !editing) return;
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories/${editing.id}`, { method: "PUT", body: JSON.stringify({ ...values, version: editing.updatedAt }) });
      message.success("记忆已更新");
      setEditing(null);
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); query.reload(); setEditing(null); } else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const saveCreate = async (values: MemoryFormValues & { type: string; key: string; subjectUserId?: number }) => {
    if (readOnly) return;
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories`, { method: "POST", body: JSON.stringify(values) });
      message.success("记忆已新增");
      setCreating(false);
      createForm.resetFields();
      query.reload();
      statusQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const pageRows = query.data?.rows ?? [];
  const pageTypeCounts = pageRows.reduce<Record<string, number>>((counts, row) => {
    counts[row.type] = (counts[row.type] ?? 0) + 1;
    return counts;
  }, {});
  return <>
    {readOnly ? (
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={12} md={6}><Card size="small"><Statistic title="可查看记忆" value={query.data?.total ?? (query.loading ? "—" : 0)} suffix="条" /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="本页显示" value={pageRows.length} suffix="条" /></Card></Col>
        <Col xs={24} md={12}><Card size="small"><Space wrap size={[8, 8]}>{Object.entries(pageTypeCounts).map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {count}</Tag>)}{!query.loading && pageRows.length === 0 && <Text type="secondary">暂无记忆</Text>}</Space></Card></Col>
      </Row>
    ) : <>
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={12} md={6}><Card size="small"><Statistic title="有效记忆" value={status?.total ?? "—"} suffix="条" /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="待整理消息" value={status?.pendingMessages ?? "—"} suffix="条" /></Card></Col>
        <Col xs={24} md={12}><Card size="small"><div className="ag-stat-line">
          <Space wrap size={[8, 8]}>{Object.entries(status?.countsByType ?? {}).map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {count}</Tag>)}{status && Object.keys(status.countsByType).length === 0 && <Text type="secondary">暂无记忆</Text>}</Space>
          <Text type="secondary">最后成功：{status?.lastSuccessAt ? formatTime(status.lastSuccessAt) : "尚未整理"}</Text>
        </div></Card></Col>
      </Row>
      {status && !status.runtimeEnabled && <Alert type="info" showIcon message="Agent 总开关已关闭，自动记忆已暂停" description="新群消息不会进入 Agent 记忆采集，定时整理也不会运行；已有记忆仍可查看、导出或手工维护。" className="section-alert" />}
      {status?.lastError && <Alert type="error" showIcon closable message={`最近整理失败（连续 ${status.consecutiveFailures} 次）`} description={status.lastError} className="section-alert" />}
      {status?.rebuildRequired && <Alert type="warning" showIcon message="派生记忆正在重建" description="系统会按连续批次处理保留期内原始消息；手工记忆不会被覆盖。" className="section-alert" />}
    </>}
    <Card title="公开/群级记忆" extra={readOnly ? undefined : <Space><Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增记忆</Button><Popconfirm title="立即整理本群记忆？" description="含 LLM 摘要，将在后台运行数十秒。" onConfirm={compact}><Button loading={status?.inFlight}>立即整理</Button></Popconfirm><Popconfirm title="重建全部自动派生记忆？" description="保留手工记忆，清除自动摘要/画像/关系后从短期消息重新生成。" onConfirm={rebuild}><Button>重建派生记忆</Button></Popconfirm><Button onClick={exportData}>导出 JSON</Button><Popconfirm title="清理整个群的消息、记忆、关系和媒体缓存？" description="此操作还会重置上下文游标，且不可撤销。" onConfirm={removeGroup}><DangerActionButton>清理全群 Agent 数据</DangerActionButton></Popconfirm></Space>}><Input.Search className="table-search" placeholder="搜索 key 或内容" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table
          rowKey="id"
          loading={query.loading}
          dataSource={query.data?.rows ?? []}
          pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }}
          expandable={readOnly ? undefined : {
            expandedRowRender: (row: MemoryItem) => <Space orientation="vertical" size={6}>
              <Paragraph copyable style={{ marginBottom: 0 }}>{row.content}</Paragraph>
              <Text type="secondary">证据 {row.provenance.evidenceCount} 条 · 首次观察 {formatTime(row.provenance.firstObservedAt)} · 最近确认 {formatTime(row.provenance.lastConfirmedAt)}</Text>
              {row.evidenceMessageIds.length > 0 && <Text type="secondary" copyable={{ text: row.evidenceMessageIds.join(",") }}>证据消息：{row.evidenceMessageIds.join(", ")}</Text>}
            </Space>,
          }}
          columns={readOnly ? [
            { title: "记忆", render: (_, row: MemoryItem) => <Space orientation="vertical" size={4}><Space wrap><Text strong>{row.key}</Text><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag></Space><Paragraph style={{ marginBottom: 0 }}>{row.content}</Paragraph></Space> },
            { title: "归属成员", dataIndex: "subjectUserId", width: 150, render: (value?: string | null) => value ? <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSearchParams({ tab: "profiles", userId: value }, { replace: true })}>{value}</Button> : "群级" },
            { title: "置信度", width: 150, render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "更新时间", dataIndex: "updatedAt", width: 180, render: formatTime },
          ] : [
            { title: "记忆", render: (_, row: MemoryItem) => <><Text strong>{row.key}</Text><br /><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag><Text type="secondary"> · {row.visibility} · {row.sourceKind === "manual" ? "手工" : "自动"}</Text></> },
            { title: "成员", dataIndex: "subjectUserId", render: (value?: string) => value ? <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSearchParams({ tab: "profiles", userId: value }, { replace: true })}>{value}</Button> : "群级" },
            { title: "权重", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.salience * 100)} size="small" /> },
            { title: "置信度", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" strokeColor="var(--ant-color-success)" /> },
            { title: "有效期至", dataIndex: "expiresAt", render: (value?: string | null) => value ? formatTime(value) : "永久" },
            { title: "更新时间", dataIndex: "updatedAt", render: formatTime },
            { title: "操作", render: (_, row: MemoryItem) => <Space><Button type="link" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" danger>删除</Button></Popconfirm>{row.subjectUserId && <Popconfirm title={`清理成员 ${row.subjectUserId} 的全部 Agent 数据？`} onConfirm={() => removeMember(row.subjectUserId!)}><Button type="link" danger>清理成员</Button></Popconfirm>}</Space> },
          ]}
        />
    }</Card>
    {!readOnly && <><MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />
    <Drawer open={creating} width={520} title="新增记忆" onClose={() => setCreating(false)}>
      <Form form={createForm} layout="vertical" onFinish={saveCreate} initialValues={{ type: "manual", salience: 0.7, confidence: 0.9 }}>
        <Form.Item name="type" label="类型" rules={[{ required: true }]}><Select options={Object.entries(MEMORY_TYPE_META).map(([value, meta]) => ({ value, label: meta.label }))} /></Form.Item>
        <Form.Item name="key" label="Key（同类型下唯一）" rules={[{ required: true, message: "请输入 key" }]}><Input maxLength={128} placeholder="如 群规 / display_name" /></Form.Item>
        <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount /></Form.Item>
        <Form.Item name="subjectUserId" label="归属成员 QQ（画像类填写）"><InputNumber min={1} precision={0} placeholder="留空为群级" style={{ width: "100%" }} /></Form.Item>
        <Row gutter={16}>
          <Col span={12}><Form.Item name="salience" label="显著度"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
          <Col span={12}><Form.Item name="confidence" label="置信度"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Form.Item name="expiresInDays" label="有效期（天）"><InputNumber min={1} max={3650} placeholder="留空则永久有效" style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>新增</Button><Button onClick={() => setCreating(false)}>取消</Button></Space>
      </Form>
    </Drawer></>}
  </>;
}

// 画像分组展示顺序：core 为反复确认晋升的不过期事实，置前展示。
const PROFILE_TYPE_ORDER = ["core", "profile", "manual"];

export function MemberProfilesPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const userId = searchParams.get("userId") ?? "";
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setDraft(userId); }, [userId]);
  const setUserId = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("userId", value); else next.delete("userId");
    setSearchParams(next, { replace: true });
  };
  const subjectsLoad = useCallback(() => api<MemorySubjectItem[]>(`/agent/groups/${groupId}/memories/subjects`).then((r) => r.data), [groupId]);
  const subjectsQuery = useApiQuery(subjectsLoad, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const subjects = subjectsQuery.data ?? [];
  const memberLoad = useCallback(() => userId
    ? api<MemoryItem[]>(`/agent/groups/${groupId}/memories?subjectUserId=${userId}&pageSize=100`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 }))
    : Promise.resolve({ rows: [] as MemoryItem[], total: 0 }), [groupId, userId]);
  const memberQuery = useApiQuery(memberLoad, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const rows = memberQuery.data?.rows ?? [];
  const grouped = PROFILE_TYPE_ORDER
    .map((type) => ({ type, items: rows.filter((row) => row.type === type) }))
    .filter((group) => group.items.length > 0);
  const counts = { profile: 0, core: 0, manual: 0 } as Record<string, number>;
  for (const row of rows) counts[row.type] = (counts[row.type] ?? 0) + 1;
  const subject = subjects.find((item) => item.userId === userId);
  const memberOptions = subjects.map((item) => ({
    value: item.userId,
    label: `${memberDisplayName(item.groupNickname, item.nickname, item.userId)}（${item.userId}）· 画像 ${item.counts.profile} / 核心 ${item.counts.core}`,
  }));
  const remove = async (id: string) => {
    if (readOnly) return;
    await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" });
    message.success("记忆已删除");
    memberQuery.reload(); subjectsQuery.reload();
  };
  const saveEdit = async (values: MemoryFormValues) => {
    if (readOnly || !editing) return;
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories/${editing.id}`, { method: "PUT", body: JSON.stringify({ ...values, version: editing.updatedAt }) });
      message.success("记忆已更新");
      setEditing(null);
      memberQuery.reload(); subjectsQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); memberQuery.reload(); subjectsQuery.reload(); setEditing(null); }
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  return <>
    <Card title="成员画像" extra={<Space wrap>
      <AutoComplete
        value={draft}
        options={memberOptions}
        onChange={(value) => setDraft(value)}
        onSelect={(value) => setUserId(String(value))}
        onInputKeyDown={(event) => { if (event.key === "Enter" && draft.trim()) setUserId(draft.trim()); }}
        filterOption={(input, option) => `${String(option?.value ?? "")} ${String(option?.label ?? "")}`.toLowerCase().includes(input.toLowerCase())}
        placeholder="输入或选择成员 QQ"
        style={{ width: 320 }}
        allowClear
      />
      <Button type="primary" disabled={!draft.trim()} onClick={() => setUserId(draft.trim())}>查看画像</Button>
      {userId && <Button onClick={() => setUserId("")}>返回列表</Button>}
    </Space>}>
      <Alert type="info" showIcon className="section-alert" message={readOnly ? "这里只展示允许公开的成员画像；已退出记忆治理的成员不会出现。" : "画像由记忆整理自动生成，也可在「记忆」页手工新增；已退出记忆（隐私）的成员不展示。"} />
      {userId
        ? (memberQuery.error && !memberQuery.data
          ? <QueryErrorAlert error={memberQuery.error} onRetry={memberQuery.reload} />
          : <>
            <div className="ag-stat-line" style={{ marginBottom: 12 }}>
              <Space wrap size={[8, 8]}>
                <Text strong>{memberDisplayName(subject?.groupNickname, subject?.nickname, userId)}</Text>
                <Text type="secondary" copyable>{userId}</Text>
                {PROFILE_TYPE_ORDER.map((type) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {counts[type] ?? 0}</Tag>)}
              </Space>
              <Text type="secondary">最近更新：{rows[0] ? formatTime(rows[0].updatedAt) : "—"}</Text>
            </div>
            {memberQuery.data && memberQuery.data.total > rows.length && <Alert type="warning" showIcon className="section-alert" message={`该成员共 ${memberQuery.data.total} 条记录，仅展示最近 100 条`} />}
            {memberQuery.loading
              ? <Spin />
              : rows.length === 0
                ? <Empty description="该成员暂无画像" />
                : grouped.map((group) => <Card key={group.type} size="small" className="section-row" title={<Space size={8}><Tag color={MEMORY_TYPE_META[group.type]?.color}>{memoryTypeLabel(group.type)}</Tag><Text type="secondary">{group.items.length} 条</Text></Space>}>
                  <List dataSource={group.items} renderItem={(row) => (
                    <List.Item actions={readOnly ? undefined : [
                      <Button key="edit" type="link" size="small" onClick={() => setEditing(row)}>编辑</Button>,
                      <Popconfirm key="remove" title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" size="small" danger>删除</Button></Popconfirm>,
                    ]}>
                      <List.Item.Meta
                        title={<Space wrap size={[8, 4]}>
                          <Text strong>{profileKeyLabel(row.key)}</Text>
                          {row.key !== profileKeyLabel(row.key) && <Text type="secondary">{row.key}</Text>}
                          <Text type="secondary">{readOnly ? `更新 ${formatTime(row.updatedAt)}` : `${row.sourceKind === "manual" ? "手工" : "自动"} · ${row.expiresAt ? `有效期至 ${formatTime(row.expiresAt)}` : "永久"} · 更新 ${formatTime(row.updatedAt)}`}</Text>
                        </Space>}
                        description={<>
                          <Paragraph copyable style={{ marginBottom: 8 }}>{row.content}</Paragraph>
                          <Space wrap size={[16, 4]}>
                            <Space size={6}>置信度<Progress percent={Math.round(row.confidence * 100)} size="small" style={{ width: 90 }} strokeColor="var(--ant-color-success)" /></Space>
                            {!readOnly && <Space size={6}>显著度<Progress percent={Math.round(row.salience * 100)} size="small" style={{ width: 90 }} /></Space>}
                          </Space>
                        </>}
                      />
                    </List.Item>
                  )} />
                </Card>)}
          </>)
        : (subjectsQuery.error && !subjectsQuery.data
          ? <QueryErrorAlert error={subjectsQuery.error} onRetry={subjectsQuery.reload} />
          : <Table rowKey="userId" loading={subjectsQuery.loading} dataSource={subjects} pagination={{ pageSize: 20, showSizeChanger: false }} locale={{ emptyText: <Empty description="暂无成员画像" /> }} columns={[
            { title: "成员", render: (_, row: MemorySubjectItem) => <>{memberDisplayName(row.groupNickname, row.nickname, row.userId)}<br /><Text type="secondary" copyable>{row.userId}</Text></> },
            { title: "成员画像", dataIndex: ["counts", "profile"], width: 100 },
            { title: "核心记忆", dataIndex: ["counts", "core"], width: 100 },
            { title: "置顶事实", dataIndex: ["counts", "manual"], width: 100 },
            { title: "最近更新", dataIndex: "updatedAt", render: formatTime, width: 170 },
            { title: "操作", width: 110, render: (_, row: MemorySubjectItem) => <Button type="link" onClick={() => setUserId(row.userId)}>查看画像</Button> },
          ]} />)}
    </Card>
    {!readOnly && <MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />}
  </>;
}

export function RelationsPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view") === "graph" ? "graph" : "table";
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [typeFilter, setTypeFilter] = useState("");
  const [creating, setCreating] = useState(false); const [editing, setEditing] = useState<AgentRelationItem | null>(null); const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm(); const [editForm] = Form.useForm();
  const setView = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "graph") next.set("view", "graph"); else next.delete("view");
    setSearchParams(next, { replace: true });
  };
  const load = useCallback(() => api<AgentRelationItem[]>(`/agent/groups/${groupId}/relations?page=${page}&pageSize=20&search=${encodeURIComponent(search)}${typeFilter ? `&type=${encodeURIComponent(typeFilter)}` : ""}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, typeFilter]);
  const query = useApiQuery(load, { resources: ["agent_relation", "agent_member_data", "agent_group_data"] });
  const graphLoad = useCallback(() => api<AgentRelationGraph>(`/agent/groups/${groupId}/relations/graph`).then((r) => r.data), [groupId]);
  const graphQuery = useApiQuery(graphLoad, { resources: ["agent_relation", "agent_member_data", "agent_group_data"] });
  const graph = graphQuery.data;
  const nodeByUserId = useMemo(() => new Map((graph?.nodes ?? []).map((node) => [node.userId, node])), [graph]);
  const linkedMemberCount = useMemo(() => (graph?.nodes ?? []).filter((node) => node.linked).length, [graph]);
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const edge of graph?.edges ?? []) counts.set(edge.type, (counts.get(edge.type) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [graph]);
  const lastSeen = useMemo(() => {
    let latest: string | null = null;
    for (const edge of graph?.edges ?? []) if (edge.lastSeenAt && (!latest || edge.lastSeenAt > latest)) latest = edge.lastSeenAt;
    return latest;
  }, [graph]);
  const typesLoad = useCallback(() => api<string[]>(`/agent/groups/${groupId}/relations/types`).then((r) => r.data), [groupId]);
  const typesQuery = useApiQuery(typesLoad, { resources: ["agent_relation"] });
  const typeOptions = Array.from(new Set([...RELATION_TYPE_PRESETS, ...(typesQuery.data ?? [])])).map((value) => ({ value, label: value }));
  const remove = async (id: string) => { if (readOnly) return; await api(`/agent/groups/${groupId}/relations/${id}`, { method: "DELETE" }); message.success("关系边已删除"); query.reload(); typesQuery.reload(); graphQuery.reload(); };
  const saveCreate = async (values: { subjectUserId: number; objectUserId: number; type: string; note: string; confidence: number }) => {
    if (readOnly) return;
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations`, { method: "POST", body: JSON.stringify(values) });
      message.success("关系边已新增"); setCreating(false); createForm.resetFields(); query.reload(); typesQuery.reload(); graphQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const openEdit = (row: AgentRelationItem) => { if (readOnly) return; setEditing(row); editForm.setFieldsValue({ note: row.note, confidence: row.confidence }); };
  const saveEdit = async (values: { note: string; confidence: number }) => {
    if (readOnly || !editing) return;
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations/${editing.id}`, { method: "PUT", body: JSON.stringify(values) });
      message.success("关系边已更新"); setEditing(null); query.reload(); graphQuery.reload();
    } catch (error) { message.error((error as Error).message); } finally { setSaving(false); }
  };
  const renderMemberCell = (value: string) => {
    const name = nodeDisplayName(nodeByUserId.get(value), value);
    return name !== value ? <>{name}<br /><Text type="secondary" copyable>{value}</Text></> : <Text copyable>{value}</Text>;
  };
  return <>
    <Row gutter={[12, 12]} className="section-row">
      <Col xs={12} md={6}><Card size="small"><Statistic title="关系边" value={graph ? graph.edges.length : "—"} suffix={graph?.meta.relationTruncated ? "+ 条（已截断）" : "条"} /></Card></Col>
      <Col xs={12} md={6}><Card size="small"><Statistic title="关系成员" value={graph ? linkedMemberCount : "—"} suffix="人" /></Card></Col>
      <Col xs={24} md={12}><Card size="small"><div className="ag-stat-line">
        <Space wrap size={[8, 8]}>{typeCounts.map(([type, count]) => <Tag key={type} color={relationTypeColor(type)}>{type} × {count}</Tag>)}{graph && typeCounts.length === 0 && <Text type="secondary">暂无关系记忆</Text>}{!graph && graphQuery.error && <Text type="secondary">图谱数据加载失败</Text>}</Space>
        <Text type="secondary">最近关系更新：{lastSeen ? formatTime(lastSeen) : "—"}</Text>
      </div></Card></Col>
    </Row>
    <Card title="成员关系边" extra={<Space wrap>
      <Segmented value={view} onChange={(value) => setView(String(value))} options={[{ value: "table", label: "列表视图" }, { value: "graph", label: "图谱视图" }]} />
      <Select value={typeFilter} onChange={(value) => { setTypeFilter(value); setPage(1); }} style={{ width: 140 }} options={[{ value: "", label: "全部类型" }, ...typeOptions]} />
      {view === "table" && <Input.Search className="table-search" placeholder="搜索成员 QQ 号" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />}
      {!readOnly && <Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增关系边</Button>}
    </Space>}>{
      view === "graph"
        ? (graphQuery.error && !graph
          ? <QueryErrorAlert error={graphQuery.error} onRetry={graphQuery.reload} />
          : graph
            ? <Suspense fallback={<div className="rg-loading-wrap"><Spin /></div>}><LazyRelationGraphView graph={graph} typeFilter={typeFilter} readOnly={readOnly} onEditRelation={readOnly ? undefined : openEdit} onDeleteRelation={readOnly ? undefined : (edge) => remove(edge.id)} /></Suspense>
            : <div className="rg-loading-wrap"><Spin /></div>)
        : (query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: <Empty description="暂无关系记忆" /> }} columns={readOnly ? [
            { title: "主体", dataIndex: "subjectUserId", render: renderMemberCell },
            { title: "客体", dataIndex: "objectUserId", render: renderMemberCell },
            { title: "类型", dataIndex: "type", render: (value: string) => <Tag color={relationTypeColor(value)}>{value}</Tag> },
            { title: "备注", dataIndex: "note", ellipsis: true, render: (value: string) => value || <Text type="secondary">—</Text> },
            { title: "置信度", render: (_, row: AgentRelationItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "最后见到", dataIndex: "lastSeenAt", render: formatTime, width: 170 },
          ] : [
            { title: "主体", dataIndex: "subjectUserId", render: renderMemberCell },
            { title: "客体", dataIndex: "objectUserId", render: renderMemberCell },
            { title: "类型", dataIndex: "type", render: (value: string) => <Tag color={relationTypeColor(value)}>{value}</Tag> },
            { title: "备注", dataIndex: "note", ellipsis: true, render: (value: string) => value || <Text type="secondary">—</Text> },
            { title: "来源", dataIndex: "sourceKind", width: 90, render: (value: string) => <Tag color={RELATION_SOURCE_META[value]?.color}>{RELATION_SOURCE_META[value]?.label ?? value}</Tag> },
            { title: "置信度", render: (_, row: AgentRelationItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "证据数", dataIndex: "evidenceCount", width: 80 },
            { title: "最后见到", dataIndex: "lastSeenAt", render: formatTime, width: 170 },
            { title: "操作", width: 120, render: (_, row: AgentRelationItem) => <Space><Button type="link" size="small" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这条关系边？" onConfirm={() => remove(row.id)}><Button type="link" size="small" danger>删除</Button></Popconfirm></Space> },
          ]} />)
    }</Card>
    {!readOnly && <><Drawer open={creating} width={520} title="新增关系边" onClose={() => setCreating(false)}>
      <Form form={createForm} layout="vertical" onFinish={saveCreate} initialValues={{ confidence: 0.9 }}>
        <Row gutter={16}>
          <Col span={12}><Form.Item name="subjectUserId" label="主体 QQ" rules={[{ required: true, message: "请输入主体 QQ" }]}><InputNumber min={1} precision={0} style={{ width: "100%" }} /></Form.Item></Col>
          <Col span={12}><Form.Item name="objectUserId" label="客体 QQ" rules={[{ required: true, message: "请输入客体 QQ" }]}><InputNumber min={1} precision={0} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Form.Item name="type" label="类型" rules={[{ required: true, message: "请选择或输入类型" }]}><AutoComplete options={typeOptions} placeholder="如 好友 / 情侣 / 对立" filterOption={(input, option) => String(option?.value ?? "").includes(input)} /></Form.Item>
        <Form.Item name="note" label="备注"><Input maxLength={200} placeholder="一句话关系背景（可选）" /></Form.Item>
        <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>新增</Button><Button onClick={() => setCreating(false)}>取消</Button></Space>
      </Form>
    </Drawer>
    <Drawer open={!!editing} width={520} title={`编辑关系边 · ${editing?.type ?? ""}`} onClose={() => setEditing(null)}>
      <Form form={editForm} layout="vertical" onFinish={saveEdit}>
        <Alert type="info" showIcon className="section-alert" message="类型与两端成员属于边的唯一身份，如需调整请删除后重新新增。" />
        <Form.Item name="note" label="备注"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={200} showCount /></Form.Item>
        <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>保存</Button><Button onClick={() => setEditing(null)}>取消</Button></Space>
      </Form>
    </Drawer></>}
  </>;
}

const AGENT_DEBUG_MODES: Array<{ value: AgentDebugMode; label: string }> = [
  { value: "dialogue", label: "普通对话" },
  { value: "active", label: "活跃插话" },
  { value: "warmup", label: "冷场暖场" },
  { value: "followup", label: "短会话续聊" },
];

function agentDebugModeLabel(value: string): string {
  return AGENT_DEBUG_MODES.find((item) => item.value === value)?.label ?? value;
}

function debugJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}

function debugRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function debugDisplay(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return debugJson(value);
}

function DebugRawBlock({ value }: { value: unknown }): React.JSX.Element {
  return <pre className="agent-debug-raw">{debugJson(value)}</pre>;
}

function DebugContextBudget({ stats }: { stats: Record<string, unknown> }): React.JSX.Element {
  const rows = [
    { key: "history", label: "历史消息" },
    { key: "memory", label: "记忆" },
    { key: "relation", label: "关系" },
  ].map(({ key, label }) => {
    const data = debugRecord(stats[key]);
    const count = Number(data.count ?? 0);
    const limit = Math.max(1, Number(data.limit ?? 1));
    return { key, label, count, limit, reached: Boolean(data.limitReached), characters: Number(data.characters ?? 0) };
  });
  return <Card size="small" title="上下文预算">
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      {rows.map((row) => <div key={row.key} className="agent-debug-budget-row">
        <div className="agent-debug-budget-head">
          <Text>{row.label}</Text>
          <Text type={row.reached ? "warning" : "secondary"}>{row.count} / {row.limit} · {row.characters} 字符</Text>
        </div>
        <Progress percent={Math.min(100, Math.round((row.count / row.limit) * 100))} showInfo={false} status={row.reached ? "exception" : "normal"} />
      </div>)}
      <Space wrap>
        <Tag>成员 {Number(stats.memberCount ?? 0)}</Tag>
        <Tag>媒体摘要 {Number(stats.mediaSummaryCount ?? 0)}</Tag>
      </Space>
    </Space>
  </Card>;
}

function DebugCurrentTurn({ value }: { value: Record<string, unknown> }): React.JSX.Element {
  const mentions = Array.isArray(value.mentions) ? value.mentions : [];
  const replyTo = debugRecord(value.reply_to);
  const media = Array.isArray(value.media) ? value.media.map(debugRecord) : [];
  return <Card size="small" title="当前消息">
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
      { key: "actor", label: "发言人", children: debugDisplay(value.name || value.user_id) },
      { key: "message", label: "消息 ID", children: debugDisplay(value.message_id) },
      { key: "trigger", label: "触发方式", children: <Tag>{debugDisplay(value.trigger)}</Tag> },
      { key: "role", label: "角色", children: debugDisplay(value.role) },
      { key: "mentions", label: "@ 成员", children: mentions.length ? mentions.map(String).join("、") : "—" },
      { key: "reply", label: "回复对象", children: debugDisplay(replyTo.name || replyTo.user_id) },
      { key: "forward", label: "转发节点", children: debugDisplay(value.forward_nodes, "0") },
    ]} />
    <Paragraph className="agent-debug-message-content">{debugDisplay(value.content, "[空消息]")}</Paragraph>
    {media.length > 0 && <Space wrap>
      {media.map((item, index) => <Tag key={`${debugDisplay(item.type)}-${index}`} color={item.source === "reply" ? "blue" : item.source === "forward" ? "purple" : "green"}>
        {TRACE_MEDIA_SOURCE_LABELS[String(item.source || "current")] ?? debugDisplay(item.source, "current")} · {SEGMENT_NAME_LABELS[String(item.type)] ?? debugDisplay(item.type, "media")}{item.name ? ` · ${debugDisplay(item.name)}` : ""}
      </Tag>)}
    </Space>}
  </Card>;
}

const CONTEXT_SELECTION_REASON: Record<string, string> = {
  recent_cluster: "近期话题簇",
  focus_relation: "直接涉及当前成员",
  query_overlap: "与当前消息相关",
  relevant_neighbor: "相关消息的相邻上下文",
  proactive_recent_cluster: "主动会话近期话题簇",
  low_information: "低信息消息",
  stale: "超过相关性时间窗",
  not_relevant: "与当前回合无明显关联",
  context_budget: "超过上下文预算",
};

function DebugContextView({ context, selection }: { context: AgentDebugResponse["context"]; selection: AgentDebugResponse["contextSelection"] }): React.JSX.Element {
  const messages = context.messages ?? [];
  const members = context.members ?? [];
  const memories = context.memories ?? [];
  const relations = context.relations ?? [];
  return <Tabs size="small" items={[
    {
      key: "messages",
      label: `历史消息 ${messages.length}`,
      children: messages.length === 0 ? <AdminEmpty description="没有进入本轮 Prompt 的历史消息" /> : <List
        className="agent-debug-list"
        dataSource={messages}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.message_id ?? index)}>
            <div className="agent-debug-list-item">
              <Space wrap size={6}>
                <Text strong>{debugDisplay(row.name || row.user_id, "未知成员")}</Text>
                {row.minutes_ago !== undefined && <Text type="secondary">{debugDisplay(row.minutes_ago)} 分钟前</Text>}
                {Boolean(row.topic_break_before) && <Tag>话题分界</Tag>}
              </Space>
              <Text>{debugDisplay(row.text, "[媒体消息]")}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "selection",
      label: `筛选轨迹 ${selection.length}`,
      children: selection.length === 0 ? <AdminEmpty description="本轮没有历史筛选轨迹" /> : <List
        className="agent-debug-list"
        dataSource={selection}
        renderItem={(item, index) => <List.Item key={`${String(item.message_id ?? "none")}-${index}`}>
          <div className="agent-debug-list-item">
            <Space wrap size={6}>
              <Tag color={item.selected ? "green" : undefined}>{item.selected ? "保留" : "丢弃"}</Tag>
              <Text strong>{item.name?.trim() || String(item.user_id ?? "未知成员")}</Text>
              <Tag>{item.role || "member"}</Tag>
              {item.title ? <Tag>{item.title}</Tag> : null}
              <Text type="secondary">{item.minutes_ago} 分钟前</Text>
            </Space>
            <Paragraph className="agent-debug-message-content" type={item.selected ? undefined : "secondary"}>
              {item.text || "[媒体消息/空文本]"}{item.text_truncated ? " …（调试预览已截断）" : ""}
            </Paragraph>
            <Space wrap size={8}>
              <Text type={item.selected ? "success" : "secondary"}>
                筛选原因：{CONTEXT_SELECTION_REASON[item.reason] ?? item.reason}
              </Text>
              <Text type="secondary">消息 ID {String(item.message_id ?? "—")}</Text>
              {typeof item.score === "number" && <Text type="secondary">相关分 {item.score.toFixed(2)}</Text>}
            </Space>
          </div>
        </List.Item>}
      />,
    },
    {
      key: "members",
      label: `成员 ${members.length}`,
      children: members.length === 0 ? <AdminEmpty description="本轮没有成员画像上下文" /> : <List
        className="agent-debug-list"
        dataSource={members}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.user_id ?? index)}>
            <div className="agent-debug-list-item">
              <Text strong>{debugDisplay(row.name || row.nickname || row.user_id, "未知成员")}</Text>
              <Text type="secondary">{debugDisplay(row.role)}{row.title ? ` · ${debugDisplay(row.title)}` : ""}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "memories",
      label: `记忆 ${memories.length}`,
      children: memories.length === 0 ? <AdminEmpty description="本轮没有命中的记忆" /> : <List
        className="agent-debug-list"
        dataSource={memories}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.id ?? index)}>
            <div className="agent-debug-list-item">
              <Space wrap size={6}><Text strong>{debugDisplay(row.subject_name || row.subject || row.memory_type, "记忆")}</Text>{row.memory_type ? <Tag>{debugDisplay(row.memory_type)}</Tag> : null}</Space>
              <Text>{debugDisplay(row.content)}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "relations",
      label: `关系 ${relations.length}`,
      children: relations.length === 0 ? <AdminEmpty description="本轮没有关系上下文" /> : <List size="small" dataSource={relations} renderItem={(item) => <List.Item><Text>{debugDisplay(item)}</Text></List.Item>} />,
    },
  ]} />;
}

function DebugPromptView({ messages }: { messages: AgentDebugResponse["promptMessages"] }): React.JSX.Element {
  return messages.length === 0 ? <AdminEmpty description="Prompt 为空" /> : <List
    className="agent-debug-prompt-list"
    dataSource={messages}
    renderItem={(item, index) => <List.Item key={`${item.role}-${index}`}>
      <div className="agent-debug-prompt-item">
        <Tag>{item.role}</Tag>
        {typeof item.content === "string" ? <pre className="agent-debug-prompt-content">{item.content}</pre> : <DebugRawBlock value={item.content} />}
      </div>
    </List.Item>}
  />;
}

const TOOL_PERMISSION_LABELS: Record<string, string> = {
  read: "只读",
  state_write: "状态写入",
  message_send: "消息发送",
  privileged: "特权",
  critical: "高风险",
};

const TOOL_PERMISSION_REASON_LABELS: Record<string, string> = {
  exposed: "已暴露",
  permission_level: "本轮没有开放这个权限等级",
  onebot_action: "当前 OneBot 实现不支持这个操作",
  bot_not_admin: "机器人没有群管理权限",
  bot_not_owner: "机器人不是群主，无法执行这个操作",
  actor_not_admin: "当前调用者没有群管理权限",
  not_allowlisted: "当前群没有把这个特权工具加入白名单",
};

const TOOL_NAME_LABELS: Record<string, string> = {
  get_group_info: "查看群信息",
  get_group_member: "查看成员详情",
  list_group_members: "查看群成员列表",
  get_message: "读取消息",
  get_recent_group_messages: "读取近期群消息",
  discover_tools: "发现可用工具",
  search_group_memory: "搜索群记忆",
  get_person_profile: "查看成员画像",
  list_user_relations: "查看成员关系",
  record_user_relation: "记录成员关系",
  search_reactions: "搜索表情包",
  send_message: "发送消息",
  send_forward: "发送合并转发",
  react_to_message: "消息表情回应",
  list_group_notices: "查看群公告",
  list_essence_messages: "查看精华消息",
  list_muted_members: "查看禁言列表",
  get_group_honor: "查看群荣誉",
  list_group_files: "查看群文件",
  get_group_file_link: "获取群文件链接",
  mute_member: "禁言成员",
  create_group_announcement: "发布群公告",
  set_essence_message: "设置精华消息",
  remove_essence_message: "移出精华消息",
  delete_group_notice: "删除群公告",
  set_group_card: "修改群名片",
  set_special_title: "设置专属头衔",
  set_group_name: "修改群名称",
  create_group_folder: "创建群文件夹",
  send_file: "发送群文件",
  kick_member: "踢出成员",
  set_whole_group_mute: "全员禁言",
  set_group_admin: "设置群管理员",
  delete_group_file: "删除群文件",
  move_group_file: "移动群文件",
  rename_group_file: "重命名群文件",
  delete_group_folder: "删除群文件夹",
};

const SEGMENT_NAME_LABELS: Record<string, string> = {
  text: "文本",
  at: "@成员",
  reply: "引用回复",
  image: "图片",
  face: "QQ 表情",
  record: "语音",
  video: "视频",
  file: "文件",
  forward: "合并转发",
};

const DELIVERY_STATE_LABELS: Record<string, string> = {
  confirmed_success: "确认发送成功",
  confirmed_failure: "确认发送失败",
  degraded_success: "降级后发送成功",
  unknown: "投递结果未知",
};

const TRACE_MEDIA_SOURCE_LABELS: Record<string, string> = {
  current: "当前消息",
  reply: "引用消息",
  forward: "合并转发",
};

const TRACE_TRIGGER_SOURCE_LABELS: Record<string, string> = {
  mention: "@ 明确呼叫",
  reply: "回复 Agent",
  wake_word: "叫名 / 唤醒词",
  explicit_call: "明确呼叫",
  proactive_warmup: "主动参与 · 冷场暖场",
  proactive_interject: "主动参与 · 加入话题",
  proactive_participation: "主动参与",
  conversation_followup: "自然续聊",
};

function triggerSourceLabel(value: unknown): string {
  const key = String(value ?? "");
  return TRACE_TRIGGER_SOURCE_LABELS[key] ?? (key || "—");
}

const TRACE_OUTCOME_LABELS: Record<string, string> = {
  completed: "正常完成",
  success: "成功",
  speak: "已发言",
  wait: "决定暂不发言",
  close: "结束会话",
  error: "执行失败",
  timeout: "模型调用超时",
  snapshot: "仅生成调试快照",
  delivery_unknown: "消息投递结果未知",
};

function traceOutcomeLabel(value: unknown): string {
  const key = String(value ?? "");
  return TRACE_OUTCOME_LABELS[key] ?? (key || "—");
}

function toolDisplayName(name: unknown): string {
  const key = String(name ?? "");
  return TOOL_NAME_LABELS[key] ?? (key || "未知工具");
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function traceMetric(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

const TRACE_FIELD_LABELS: Record<string, string> = {
  model: "模型",
  text_preview: "文本预览",
  content_preview: "模型输出预览",
  current_turn_preview: "当前回合预览",
  query_preview: "检索文本预览",
  error_type: "异常类型",
  error_message: "异常信息",
  finish_reason: "结束原因",
  message_id: "消息 ID",
  text_chars: "文本字符",
  query_chars: "检索字符",
  current_turn_chars: "当前回合字符",
  size_bytes: "大小",
  mime: "MIME",
  url_host: "URL 主机",
  url_allowed: "URL 可直传",
  status: "处理状态",
  reason: "原因",
  multimodal_mode: "多模态模式",
  cache_enabled: "媒体缓存",
  caption_cache: "图片转述缓存",
  context_token_limit: "上下文 Token 上限",
  completion_reserve: "预留输出 Token",
  prefix_fingerprint: "Prompt 指纹",
  prompt_cache: "Prompt 前缀缓存",
  context_cache: "稳定上下文缓存",
};

function traceByteSize(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(2)} MiB`;
}

function TraceDiagnosticValue({ name, value }: { name: string; value: unknown }): React.JSX.Element {
  if (value == null || value === "") return <Text type="secondary">—</Text>;
  if (typeof value === "boolean") return <Tag color={value ? "green" : "default"}>{value ? "是" : "否"}</Tag>;
  if (typeof value === "number") return <Text>{name === "size_bytes" ? traceByteSize(value) : value}</Text>;
  if (typeof value === "string") return <Text code={name.endsWith("_id") || name.includes("fingerprint")}>{value}</Text>;
  if (Array.isArray(value)) {
    const scalar = value.filter((item) => ["string", "number", "boolean"].includes(typeof item)).slice(0, 12);
    if (scalar.length === value.length) return <Space wrap size={[4, 4]}>{scalar.map((item, index) => <Tag key={`${String(item)}-${index}`}>{String(item)}</Tag>)}</Space>;
    return <Text type="secondary">{value.length} 项结构化数据</Text>;
  }
  const row = debugRecord(value);
  if (row.redacted === true) {
    const kind = String(row.kind ?? "sensitive");
    if (kind === "url") return <Space wrap size={[4, 4]}><Tag color="gold">完整 URL 已隐藏</Tag>{row.host ? <Text code>{String(row.host)}</Text> : null}{row.suffix ? <Tag>{String(row.suffix)}</Tag> : null}{row.has_query ? <Tag>含查询参数</Tag> : null}</Space>;
    if (kind === "path") return <Space wrap size={[4, 4]}><Tag color="gold">完整路径已隐藏</Tag>{row.platform ? <Tag>{String(row.platform)}</Tag> : null}{row.suffix ? <Tag>{String(row.suffix)}</Tag> : null}</Space>;
    if (kind === "payload") return <Space wrap size={[4, 4]}><Tag color="gold">Payload 值已隐藏</Tag>{stringArray(row.keys).map((key) => <Tag key={key}>{key}</Tag>)}</Space>;
    return <Tag color="gold">敏感值已隐藏 · {kind}</Tag>;
  }
  return <Space wrap size={[4, 4]}>{Object.entries(row).slice(0, 10).map(([key, item]) => <Tag key={key}>{key}={typeof item === "object" ? "…" : String(item)}</Tag>)}</Space>;
}

function TraceDiagnosticFields({ input, output }: { input: Record<string, unknown>; output: Record<string, unknown> }): React.JSX.Element | null {
  const hidden = new Set(["media", "items", "usage", "arguments", "trigger_signals", "onebot_actions", "selected_tool_names", "tool_names", "message_segment_types", "content_hashes", "roles"]);
  const rows = [...Object.entries(input).map(([name, value]) => [`输入 · ${name}`, name, value] as const), ...Object.entries(output).map(([name, value]) => [`输出 · ${name}`, name, value] as const)]
    .filter(([, name, value]) => !hidden.has(name) && value != null && value !== "")
    .slice(0, 18);
  if (rows.length === 0) return null;
  return <Descriptions
    className="agent-trace-facts"
    size="small"
    column={{ xs: 1, sm: 2, lg: 3 }}
    items={rows.map(([key, name, value]) => ({ key, label: TRACE_FIELD_LABELS[name] ?? name, children: <TraceDiagnosticValue name={name} value={value} /> }))}
  />;
}

function ToolArgumentSummary({ name, value }: { name: string; value: unknown }): React.JSX.Element | null {
  const args = debugRecord(value);
  if (Object.keys(args).length === 0) return null;

  if (name === "send_message") {
    const segments = Array.isArray(args.segments) ? args.segments.map((item) => debugRecord(item)) : [];
    const types = segments.map((item) => String(item.type ?? "")).filter(Boolean);
    return <Text type="secondary">
      准备发送 {segments.length} 个消息段{types.length > 0 ? `：${types.map((type) => SEGMENT_NAME_LABELS[type] ?? type).join(" + ")}` : ""}
    </Text>;
  }
  if (name === "send_forward") {
    const nodes = Array.isArray(args.nodes) ? args.nodes.length : 0;
    return <Text type="secondary">准备发送包含 {nodes} 个节点的合并转发</Text>;
  }
  if (name === "search_group_memory") {
    return <Text type="secondary">搜索内容：{String(args.query ?? args.keyword ?? "（未提供关键词）")}</Text>;
  }
  if (name === "get_group_member" || name === "get_person_profile") {
    return <Text type="secondary">目标成员：{String(args.user_id ?? args.userId ?? "未知")}</Text>;
  }
  if (name === "list_user_relations") {
    return <Text type="secondary">查看成员 {String(args.user_id ?? args.userId ?? "未知")} 的关系</Text>;
  }
  if (name === "record_user_relation") {
    return <Text type="secondary">
      记录 {String(args.subject_user_id ?? "?")} → {String(args.object_user_id ?? "?")} 的「{String(args.type ?? "关系")}」关系
    </Text>;
  }
  if (name === "mute_member") {
    return <Text type="secondary">目标成员：{String(args.user_id ?? "未知")}；时长：{String(args.duration ?? args.duration_seconds ?? "默认")} 秒</Text>;
  }
  if (name === "create_group_announcement") {
    const content = String(args.content ?? args.text ?? "");
    return <Text type="secondary">公告内容：{content ? `${content.slice(0, 80)}${content.length > 80 ? "…" : ""}` : "（空）"}</Text>;
  }
  if (name === "send_file") {
    return <Text type="secondary">发送一个经过 Agent 文件安全校验的群文件</Text>;
  }
  if (name === "search_reactions") {
    return <Text type="secondary">搜索表情：{String(args.query ?? args.keyword ?? args.tag ?? "未指定")}</Text>;
  }
  return null;
}

function TraceHumanSummary({ event }: { event: AgentExecutionTrace["events"][number] }): React.JSX.Element | null {
  const input = debugRecord(event.input);
  const output = debugRecord(event.output);

  if (event.phase === "intake" && (output.trigger_source || output.scene)) {
    const trigger = triggerSourceLabel(output.trigger_source);
    const scene = String(output.scene ?? "");
    const signals = debugRecord(output.trigger_signals);
    const matched = Object.entries(signals).filter(([, value]) => value === true).map(([key]) => key);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>这次执行的触发原因：<Tag color="blue">{trigger}</Tag></Text>
      {scene && <Text type="secondary">内部参与场景：{scene === "warmup" ? "冷场暖场" : scene === "active" ? "加入活跃话题" : scene === "followup" ? "自然续聊" : scene}</Text>}
      {matched.length > 1 && <Text type="secondary">同时检测到：{matched.join(" / ")}</Text>}
      {output.text_preview ? <Text type="secondary">消息预览：{String(output.text_preview)}</Text> : null}
      {typeof output.queue_wait_ms === "number" ? <Text type="secondary">进入执行前排队 {output.queue_wait_ms.toFixed(1)} ms</Text> : null}
    </Space>;
  }

  if (event.phase === "capability") {
    const exposed = stringArray(output.selected_tools ?? output.selected_tool_names ?? output.exposed_tools ?? output.tool_names);
    const messageSegments = stringArray(output.message_segment_types);
    const blocked = Array.isArray(output.blocked_tools)
      ? output.blocked_tools.map((item) => debugRecord(item))
      : [];
    const toolCount = traceMetric(output.tool_count, exposed.length);
    const roundLimit = traceMetric(output.round_limit);
    return <Space orientation="vertical" size={6} style={{ width: "100%" }}>
      <Text>
        本轮向模型开放 <Text strong>{toolCount}</Text> 个工具
        {blocked.length > 0 ? <>，另有 <Text strong>{blocked.length}</Text> 个工具未开放</> : ""}
        {roundLimit > 0 ? <>；模型调用最多 <Text strong>{roundLimit}</Text> 轮。</> : "。"}
      </Text>
      {exposed.length > 0 && <Space wrap size={[6, 6]}>
        <Text type="secondary">可用：</Text>
        {exposed.map((name) => <Tag color="green" key={name}>{toolDisplayName(name)}</Tag>)}
      </Space>}
      {messageSegments.length > 0 && exposed.includes("send_message") && <Space wrap size={[6, 6]}>
        <Text type="secondary">本轮消息段：</Text>
        {messageSegments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}
      </Space>}
      {blocked.length > 0 && <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        <Text type="secondary">未开放：</Text>
        {blocked.map((item, index) => {
          const name = String(item.name ?? "");
          const reason = String(item.reason ?? "");
          return <Text key={`${name}-${index}`}>
            <Tag>{toolDisplayName(name)}</Tag>
            {TOOL_PERMISSION_REASON_LABELS[reason] ?? (reason || "未满足当前权限条件")}
          </Text>;
        })}
      </Space>}
    </Space>;
  }

  if (event.phase === "context") {
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        最终选入 <Text strong>{traceMetric(output.messages)}</Text> 条历史消息、
        <Text strong>{traceMetric(output.members)}</Text> 位成员、
        <Text strong>{traceMetric(output.memories)}</Text> 条记忆和
        <Text strong>{traceMetric(output.relations)}</Text> 条关系。
      </Text>
      {output.model ? <Text type="secondary">上下文按模型 <Text code>{String(output.model)}</Text> 的预算装箱；上限 {traceMetric(input.context_token_limit)} Token，预留输出 {traceMetric(input.completion_reserve)} Token。</Text> : null}
      {input.query_preview ? <Text type="secondary">用于相关性选择的文本：{String(input.query_preview)}</Text> : null}
    </Space>;
  }

  if (event.phase === "parse" || event.phase === "intake") {
    const replyDepth = traceMetric(output.reply_depth);
    const forwardNodes = traceMetric(output.forward_nodes ?? output.top_level_nodes);
    const mediaRefs = traceMetric(output.media_refs ?? output.media_refs_total);
    const segments = stringArray(output.segment_types);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {event.label}：{replyDepth > 0 ? `解析了 ${replyDepth} 层引用；` : ""}
        {forwardNodes > 0 ? `展开 ${forwardNodes} 个转发节点；` : ""}
        {mediaRefs > 0 ? `发现 ${mediaRefs} 个媒体引用。` : "未发现额外媒体。"}
      </Text>
      {segments.length > 0 && <Space wrap size={[6, 6]}>{segments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}</Space>}
    </Space>;
  }

  if (event.phase === "media") {
    const media = Array.isArray(input.media) ? input.media.map((item) => debugRecord(item)) : [];
    const items = Array.isArray(output.items) ? output.items.map((item) => debugRecord(item)) : [];
    const statusLabel: Record<string, string> = {
      loaded: "已读取并转成视觉输入",
      url_passthrough: "本地读取失败，改由模型读取原始 URL",
      dropped_unavailable: "无法取得图片，已丢弃",
      skipped_non_image: "不是图片，已跳过",
    };
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        准备了 <Text strong>{traceMetric(output.vision_blocks)}</Text> 个视觉输入，
        命中 <Text strong>{traceMetric(output.cached_captions)}</Text> 条图片转述缓存。
      </Text>
      {output.multimodal_mode ? <Text type="secondary">当前多模态策略：<Text code>{String(output.multimodal_mode)}</Text>；媒体缓存：{output.cache_enabled ? "开启" : "关闭"}。</Text> : null}
      {media.length > 0 && <Space wrap size={[6, 6]}>{media.map((item, index) => {
        const source = String(item.source || "current");
        return <Tag key={`${String(item.type)}-${index}`}>{SEGMENT_NAME_LABELS[String(item.type)] ?? String(item.type || "媒体")} · {TRACE_MEDIA_SOURCE_LABELS[source] ?? source}</Tag>;
      })}</Space>}
      {items.length > 0 && <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        {items.map((item, index) => {
          const status = String(item.status ?? "unknown");
          return <div key={`${status}-${index}`} className="agent-trace-media-row">
            <Space wrap size={[4, 4]}>
              <Tag color={status === "loaded" ? "green" : status === "dropped_unavailable" ? "red" : "gold"}>图片 {index + 1}</Tag>
              <Text>{statusLabel[status] ?? status}</Text>
              {item.mime ? <Tag>{String(item.mime)}</Tag> : null}
              {typeof item.size_bytes === "number" ? <Tag>{traceByteSize(item.size_bytes)}</Tag> : null}
              {item.url_host ? <Tag>{String(item.url_host)}</Tag> : null}
              {item.caption_cache ? <Tag>字幕缓存 {String(item.caption_cache)}</Tag> : null}
            </Space>
            {item.reason ? <Text type="danger">{String(item.reason)}</Text> : null}
          </div>;
        })}
      </Space>}
    </Space>;
  }

  if (event.phase === "prompt") {
    const count = traceMetric(output.message_count);
    const promptCache = String(output.prompt_cache ?? "");
    const contextCache = String(output.context_cache ?? "");
    const roles = debugRecord(output.roles);
    const tools = stringArray(output.tool_names);
    const hasPromptShape = typeof output.text_chars === "number" || typeof output.media_blocks === "number";
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        已组装 <Text strong>{count}</Text> 条 Prompt 消息
        {hasPromptShape ? <>，共约 <Text strong>{traceMetric(output.text_chars)}</Text> 个文本字符、<Text strong>{traceMetric(output.media_blocks)}</Text> 个图片块</> : null}
        {promptCache ? `；Prompt 前缀缓存${promptCache === "hit" ? "命中" : "未命中"}` : ""}
        {contextCache ? `，稳定上下文缓存${contextCache === "hit" ? "命中" : "未命中"}` : ""}。
      </Text>
      {Object.keys(roles).length > 0 && <Space wrap size={[4, 4]}><Text type="secondary">角色构成：</Text>{Object.entries(roles).map(([role, amount]) => <Tag key={role}>{role} × {String(amount)}</Tag>)}</Space>}
      {tools.length > 0 && <Space wrap size={[4, 4]}><Text type="secondary">Prompt 携带工具：</Text>{tools.map((tool) => <Tag key={tool}>{toolDisplayName(tool)}</Tag>)}</Space>}
      {output.current_turn_preview ? <Text type="secondary">模型看到的当前回合预览：{String(output.current_turn_preview)}</Text> : null}
    </Space>;
  }

  if (event.phase === "llm") {
    const tools = stringArray(output.tool_calls);
    const action = String(output.action ?? "");
    const chars = traceMetric(output.content_chars);
    const usage = debugRecord(output.usage);
    const requestUsage = debugRecord(usage.request);
    const turnUsage = debugRecord(usage.turn);
    const hasTurnUsage = Object.keys(turnUsage).length > 0;
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {action ? `模型决策：${action}` : `模型返回 ${chars} 个字符`}
        {tools.length > 0 ? `，并请求调用 ${tools.length} 个工具。` : "。"}
      </Text>
      {tools.length > 0 && <Space wrap size={[6, 6]}>{tools.map((name) => <Tag color="blue" key={name}>{toolDisplayName(name)}</Tag>)}</Space>}
      {output.model ? <Text type="secondary">模型：<Text code>{String(output.model)}</Text>{output.finish_reason ? `；结束原因：${String(output.finish_reason)}` : ""}</Text> : null}
      {output.content_preview ? <Text type="secondary">输出预览：{String(output.content_preview)}</Text> : null}
      {hasTurnUsage && <Text type="secondary">
        本次请求输入/输出：{traceMetric(requestUsage.prompt_tokens)} / {traceMetric(requestUsage.completion_tokens)} Token；
        本回合累计输入：{traceMetric(turnUsage.prompt_tokens)}，其中缓存命中 {traceMetric(turnUsage.cached_tokens)}、未命中 {traceMetric(turnUsage.cache_miss_tokens)} Token。
      </Text>}
      {typeof output.confidence === "number" && <Text type="secondary">决策置信度：{Math.round(output.confidence * 100)}%</Text>}
    </Space>;
  }

  if (event.phase === "tool") {
    const toolName = event.label.replace(/^工具\s+/, "").replace(/^工具意图\s+/, "");
    const executed = output.executed;
    const ok = output.ok;
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {executed === false
          ? <>模型计划调用 <Text strong>{toolDisplayName(toolName)}</Text>，但这是无副作用调试，因此没有实际执行。</>
          : <><Text strong>{toolDisplayName(toolName)}</Text>{ok === false ? " 执行失败" : " 执行完成"}{output.ends_turn ? "，并结束本轮回复" : ""}。</>}
      </Text>
      <ToolArgumentSummary name={toolName} value={input.arguments} />
    </Space>;
  }

  if (event.phase === "speech") {
    const quality = Array.isArray(output.quality) ? output.quality.map(debugRecord) : [];
    const style = debugRecord(output.style);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        发言动作 <Text strong>{String(output.action ?? "speak")}</Text>，场景 <Text code>{String(output.scene ?? "conversation")}</Text>，
        话语动作 {String(output.act ?? "continue")}，话轮压力 {String(output.turn_pressure ?? "low")}。
      </Text>
      <Space wrap size={[6, 6]}>
        {output.target_user_id ? <Tag>目标 {String(output.target_user_id)}</Tag> : null}
        {output.topic ? <Tag color="blue">话题 {String(output.topic)}</Tag> : null}
        {output.topic_action ? <Tag>{String(output.topic_action)}</Tag> : null}
        {Object.keys(style).length > 0 ? <Tag>Persona style 已应用</Tag> : null}
        {output.emotion ? <Tag color="purple">Emotion 已应用</Tag> : null}
      </Space>
      {quality.length > 0 && <Text type="secondary">质量检查：{quality.map((item) => String(item.code ?? "quality")).join(" / ")}</Text>}
    </Space>;
  }

  if (event.phase === "outbound") {
    const segments = stringArray(output.segment_types ?? output.fallback_types ?? input.segment_types);
    const deliveryState = String(output.delivery_state ?? "");
    const degradedFrom = String(output.degraded_from ?? "");
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {deliveryState ? (DELIVERY_STATE_LABELS[deliveryState] ?? deliveryState) : event.label}
        {degradedFrom ? `；已从 ${degradedFrom} 方案降级` : ""}。
      </Text>
      {segments.length > 0 && <Space wrap size={[6, 6]}><Text type="secondary">实际消息：</Text>{segments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}</Space>}
      {(output.text_preview || input.text_preview) ? <Text type="secondary">发送文本预览：{String(output.text_preview ?? input.text_preview)}</Text> : null}
      {output.message_id ? <Text type="secondary">OneBot message_id：<Text code>{String(output.message_id)}</Text></Text> : null}
    </Space>;
  }

  if (event.phase === "state") {
    if (event.status === "skipped") return <Text>本次是调试执行，不会修改数据库、冷却时间或 Agent 状态。</Text>;
    if (event.status === "failed") return <Text type="danger">状态写入失败，已按当前流程回滚或保留发送结果。</Text>;
    if (event.status === "degraded") return <Text type="warning">消息投递流程已结束，但部分本地状态更新失败；发送结果不会因此改判为失败。</Text>;
    return <Text>本轮状态已经写入，包括去重指纹、冷却时间和会话进度。</Text>;
  }

  if (event.phase === "turn") {
    const outcome = String(output.outcome ?? "");
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>本轮执行结束{outcome ? `，结果：${traceOutcomeLabel(outcome)}` : ""}。</Text>
      {output.error_type ? <Text type="danger">{String(output.error_type)}{output.error_message ? `：${String(output.error_message)}` : ""}</Text> : null}
    </Space>;
  }

  return null;
}

function DebugToolsView({ tools, permissions }: { tools: AgentDebugResponse["tools"]; permissions: AgentDebugResponse["toolPermissions"] }): React.JSX.Element {
  if (permissions.length === 0) return <AdminEmpty description="当前调试场景不使用工具权限矩阵" />;
  const schemas = new Map<string, Record<string, unknown>>();
  tools.forEach((tool) => {
    const row = debugRecord(tool);
    const fn = debugRecord(row.function);
    const name = String(fn.name || row.name || "");
    if (name) schemas.set(name, tool);
  });
  return <List
    className="agent-debug-list"
    dataSource={permissions}
    renderItem={(permission) => {
      const tool = schemas.get(permission.name);
      const row = tool ? debugRecord(tool) : {};
      const fn = debugRecord(row.function);
      return <List.Item key={permission.name}>
        <div className="agent-debug-list-item">
          <Space wrap>
            <Text strong>{toolDisplayName(permission.name)}</Text>
            <Text type="secondary" code>{permission.name}</Text>
            <Tag color={permission.permissionLevel === "critical" ? "volcano" : permission.permissionLevel === "privileged" ? "red" : permission.permissionLevel === "message_send" ? "blue" : permission.permissionLevel === "state_write" ? "orange" : "default"}>{TOOL_PERMISSION_LABELS[permission.permissionLevel] ?? permission.permissionLevel}</Tag>
            <Tag color={permission.exposed ? "green" : "default"}>{TOOL_PERMISSION_REASON_LABELS[permission.reason] ?? permission.reason}</Tag>
          </Space>
          {(fn.description || row.description) ? <Text type="secondary">{debugDisplay(fn.description || row.description)}</Text> : null}
          {permission.actions.length > 0 && <Text type="secondary">OneBot：{permission.actions.join(" / ")}</Text>}
          {tool && <details className="agent-debug-details"><summary>查看 Schema</summary><DebugRawBlock value={tool} /></details>}
        </div>
      </List.Item>;
    }}
  />;
}

const TRACE_PHASE_LABELS: Record<string, string> = {
  parse: "解析",
  intake: "输入",
  policy: "策略",
  context: "上下文",
  capability: "能力",
  media: "媒体",
  prompt: "Prompt",
  llm: "模型",
  tool: "工具",
  speech: "发言",
  outbound: "发送",
  state: "状态",
  turn: "回合",
};

const TRACE_STATUS_META: Record<string, { label: string; color: string }> = {
  running: { label: "执行中", color: "processing" },
  completed: { label: "完成", color: "green" },
  planned: { label: "计划", color: "blue" },
  success: { label: "成功", color: "green" },
  failed: { label: "失败", color: "red" },
  degraded: { label: "降级", color: "orange" },
  unknown: { label: "未知", color: "gold" },
  skipped: { label: "跳过", color: "default" },
};

function ExecutionTraceView({ trace, compact = false }: { trace: AgentExecutionTrace; compact?: boolean }): React.JSX.Element {
  const status = TRACE_STATUS_META[trace.status] ?? { label: trace.status, color: "default" };
  const visibleEvents = compact ? trace.events.slice(-12) : trace.events;
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "source", label: "执行来源", children: <Tag color={trace.source === "runtime" ? "purple" : "blue"}>{trace.source === "runtime" ? "真实执行" : "调试执行"}</Tag> },
      { key: "trigger", label: "触发原因", children: trace.triggerSource ? <Tag color="blue">{triggerSourceLabel(trace.triggerSource)}</Tag> : "—" },
      { key: "status", label: "状态", children: <Tag color={status.color}>{status.label}</Tag> },
      { key: "outcome", label: "结果", children: traceOutcomeLabel(trace.outcome) },
      { key: "duration", label: "总耗时", children: trace.durationMs == null ? "—" : `${trace.durationMs.toFixed(1)} ms` },
      { key: "actor", label: "发言人", children: trace.actorUserId ? <Text code>{trace.actorUserId}</Text> : "—" },
      { key: "message", label: "触发消息", children: trace.messageId ? <Text code>{trace.messageId}</Text> : "—" },
      { key: "trace", label: "Trace", children: <Text code>{trace.traceId.slice(0, 12)}</Text> },
    ]} />
    {compact && trace.events.length > visibleEvents.length && <Alert type="info" showIcon message={`仅显示最后 ${visibleEvents.length} / ${trace.events.length} 个事件`} />}
    <Timeline
      items={visibleEvents.map((event) => {
        const eventMeta = TRACE_STATUS_META[event.status] ?? { label: event.status, color: "default" };
        const hasInput = Object.keys(event.input ?? {}).length > 0;
        const hasOutput = Object.keys(event.output ?? {}).length > 0;
        return {
          color: event.status === "failed" ? "red" : event.status === "degraded" || event.status === "unknown" ? "orange" : event.status === "success" ? "green" : "blue",
          children: <div className="agent-debug-list-item">
            <Space wrap>
              <Tag>{TRACE_PHASE_LABELS[event.phase] ?? event.phase}</Tag>
              <Text strong>{event.label}</Text>
              <Tag color={eventMeta.color}>{eventMeta.label}</Tag>
              {event.round != null && <Tag>第 {event.round} 轮</Tag>}
              <Text type="secondary">+{event.offsetMs.toFixed(1)} ms</Text>
              {event.durationMs != null && <Text type="secondary">耗时 {event.durationMs.toFixed(1)} ms</Text>}
            </Space>
            <TraceHumanSummary event={event} />
            {event.detail && <Text type={event.status === "failed" ? "danger" : "secondary"}>{event.detail}</Text>}
            <TraceDiagnosticFields input={event.input ?? {}} output={event.output ?? {}} />
            {(hasInput || hasOutput) && <details className="agent-debug-details">
              <summary>查看原始诊断字段（JSON，备用）</summary>
              {hasInput && <><Text type="secondary">输入</Text><DebugRawBlock value={event.input} /></>}
              {hasOutput && <><Text type="secondary">输出</Text><DebugRawBlock value={event.output} /></>}
            </details>}
          </div>,
        };
      })}
    />
  </Space>;
}

function DebugSpeechSimulation({ value }: { value: AgentDebugResponse["speechSimulation"] }): React.JSX.Element {
  const style = debugRecord(value.style);
  const quality = Array.isArray(value.quality) ? value.quality.map(debugRecord) : [];
  const segments = Array.isArray(value.segments) ? value.segments.map(debugRecord) : [];
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "speak", label: "是否发言", children: value.should_speak == null ? <Tag>仅策略预览</Tag> : <Tag color={value.should_speak ? "green" : "default"}>{value.should_speak ? "发言" : "保持沉默"}</Tag> },
      { key: "action", label: "参与动作", children: <Tag>{value.action}</Tag> },
      { key: "scene", label: "Speech Scene", children: <Tag color="blue">{value.scene}</Tag> },
      { key: "act", label: "话语动作", children: value.act },
      { key: "turn", label: "话轮压力", children: <Tag color={value.turn_pressure === "high" ? "orange" : undefined}>{value.turn_pressure}</Tag> },
      { key: "target", label: "目标成员", children: debugDisplay(value.target_user_id) },
      { key: "topic", label: "话题", children: debugDisplay(value.topic) },
      { key: "topicAction", label: "话题动作", children: value.topic_action },
    ]} />
    <Space wrap>
      <Tag>温暖 {debugDisplay(style.warmth)}</Tag>
      <Tag>幽默 {debugDisplay(style.humor)}</Tag>
      <Tag>直接 {debugDisplay(style.directness)}</Tag>
      <Tag>详略 {debugDisplay(style.verbosity)}</Tag>
      <Tag>表现力 {debugDisplay(style.expressiveness)}</Tag>
    </Space>
    {value.text ? <Card size="small" title="最终文本"><Paragraph style={{ marginBottom: 0 }}>{value.text}</Paragraph></Card> : <Text type="secondary">{value.status === "policy_only" ? "未调用模型；这里只预览发言策略，最终文本尚未生成。" : "本轮没有纯文本。"}</Text>}
    {segments.length > 0 && <Card size="small" title={`最终消息段 ${segments.length}`}><DebugRawBlock value={segments} /></Card>}
    {quality.length > 0 && <Space wrap><Text type="secondary">质量检查：</Text>{quality.map((item, index) => <Tag key={`${String(item.code ?? "quality")}-${index}`} color={item.autofixed ? "green" : "orange"}>{String(item.code ?? "quality")}{item.autofixed ? " · 已修正" : ""}</Tag>)}</Space>}
    {value.reason ? <Text type="secondary">决策理由：{value.reason}</Text> : null}
  </Space>;
}

function DebugModelView({ result }: { result: AgentDebugResponse["result"] }): React.JSX.Element {
  if (!result) return <AdminEmpty description="本次只生成提示词，没有调用模型" />;
  const decision = result.decision ? debugRecord(result.decision) : null;
  const cacheHitRate = result.usage.promptTokens && result.usage.cachedTokens != null
    ? `${Math.round((result.usage.cachedTokens / result.usage.promptTokens) * 100)}%`
    : "—";
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "outcome", label: "结果", children: <Tag color={result.outcome === "success" ? "green" : result.outcome === "timeout" ? "red" : "orange"}>{traceOutcomeLabel(result.outcome)}</Tag> },
      { key: "duration", label: "耗时", children: `${result.durationMs} ms` },
      { key: "finish", label: "结束原因", children: result.finishReason || "—" },
      { key: "tokens", label: "Token（输入 / 输出）", children: `${result.usage.promptTokens ?? "—"} / ${result.usage.completionTokens ?? "—"}` },
      { key: "cacheHit", label: "缓存命中 Token", children: result.usage.cachedTokens ?? "—" },
      { key: "cacheMiss", label: "缓存未命中 Token", children: result.usage.cacheMissTokens ?? "—" },
      { key: "cacheRate", label: "Prompt 缓存命中率", children: cacheHitRate },
    ]} />
    {decision && <Card size="small" title="主动发言决策"><Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      { key: "action", label: "动作", children: debugDisplay(decision.action) },
      { key: "target", label: "目标成员", children: debugDisplay(decision.targetUserId) },
      { key: "topic", label: "话题", children: debugDisplay(decision.topic) },
      { key: "reason", label: "原因", children: debugDisplay(decision.reason) },
      { key: "confidence", label: "决策置信度", children: debugDisplay(decision.confidence) },
      { key: "segments", label: "消息段", children: debugDisplay(decision.segments) },
    ]} /></Card>}
    <Card size="small" title="模型文本"><Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>{result.text || "（无文本输出）"}</Paragraph></Card>
    {result.toolCalls.length > 0 && <Card size="small" title={`工具意图（${result.toolCalls.length}）`}><List
      size="small"
      dataSource={result.toolCalls}
      renderItem={(call) => <List.Item><div className="agent-debug-list-item">
        <Space wrap>
          <Text strong>{toolDisplayName(call.name)}</Text>
          <Text type="secondary" code>{call.name}</Text>
          <Tag color="blue">模型计划</Tag>
        </Space>
        <ToolArgumentSummary name={call.name} value={call.arguments} />
        <details className="agent-debug-details"><summary>查看工具参数（JSON）</summary><DebugRawBlock value={call.arguments} /></details>
      </div></List.Item>}
    /></Card>}
  </Space>;
}

function AgentDebugPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams] = useSearchParams();
  const linkedMessageId = searchParams.get("messageId") ?? "";
  const [mode, setMode] = useState<AgentDebugMode>("dialogue");
  const [source, setSource] = useState<"history" | "simulation">(linkedMessageId ? "history" : "simulation");
  const [messageId, setMessageId] = useState(linkedMessageId);
  const [actorUserId, setActorUserId] = useState("");
  const [text, setText] = useState("");
  const [runModel, setRunModel] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AgentDebugResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadMessages = useCallback(
    () => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=1&pageSize=100`).then((r) => r.data),
    [groupId],
  );
  const messagesQuery = useApiQuery(loadMessages, { resources: ["agent_group_data", "agent_privacy"] });
  const [runtimeTraceId, setRuntimeTraceId] = useState("");
  const loadRuntimeTraces = useCallback(
    () => api<AgentExecutionTrace[]>(`/agent/groups/${groupId}/execution-traces`).then((r) => r.data),
    [groupId],
  );
  const runtimeTraceQuery = useApiQuery(loadRuntimeTraces);
  const runtimeTrace = useMemo(
    () => (runtimeTraceQuery.data ?? []).find((item) => item.traceId === runtimeTraceId) ?? runtimeTraceQuery.data?.[0] ?? null,
    [runtimeTraceId, runtimeTraceQuery.data],
  );
  useEffect(() => {
    if (linkedMessageId) {
      setSource("history");
      setMessageId(linkedMessageId);
    }
  }, [linkedMessageId]);
  const messageOptions = useMemo(
    () => (messagesQuery.data ?? []).filter((row) => row.role !== "bot").map((row) => ({ value: row.messageId, label: debugMessageLabel(row) })),
    [messagesQuery.data],
  );
  const selectedMessage = useMemo(
    () => (messagesQuery.data ?? []).find((row) => row.messageId === messageId) ?? null,
    [messageId, messagesQuery.data],
  );
  const run = async () => {
    if (source === "history" && !messageId) {
      message.warning("请先选择一条历史消息"); return;
    }
    if (source === "simulation" && (!actorUserId.trim() || !text.trim())) {
      message.warning("请填写模拟发言人和消息正文"); return;
    }
    setRunning(true); setError(null);
    try {
      const body = source === "history"
        ? { mode, messageId: Number(messageId), runModel }
        : { mode, actorUserId: Number(actorUserId), text: text.trim(), runModel };
      const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, { method: "POST", body: JSON.stringify(body) });
      setResult(response.data);
      message.success(runModel ? "真实模型试跑完成，未执行任何动作" : "提示词快照已生成");
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return <Space orientation="vertical" size="large" style={{ width: "100%" }}>
    <Alert
      type="info"
      showIcon
      className="section-alert"
      message="执行追踪器 + 发言模拟器"
      description="这里同时提供无副作用发言模拟、调试 Trace 和当前进程最近的真实 Agent Trace。真实试跑只请求模型，不执行工具、不发送消息、不修改状态；真实运行时间线则会记录实际工具、发送降级与 delivery_state。"
    />

    <Card
      title="最近真实执行"
      extra={<Button onClick={() => runtimeTraceQuery.reload()} loading={runtimeTraceQuery.loading}>刷新 Trace</Button>}
    >
      <Alert type="warning" showIcon className="section-alert" message="Trace 仅保存在当前 Bot 进程内，重启后清空。完整 URL、本机路径、file 值与原始 OneBot payload 不会保留；调试页会保留 host、文件类型/大小、Payload 字段结构等安全元数据用于排障。" />
      {runtimeTraceQuery.error && !runtimeTraceQuery.data
        ? <QueryErrorAlert error={runtimeTraceQuery.error} onRetry={runtimeTraceQuery.reload} />
        : (runtimeTraceQuery.data?.length ?? 0) === 0
          ? <AdminEmpty description="暂无真实执行 Trace；让 Agent 实际处理一条触发消息后刷新这里" />
          : <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
              <Select
                value={runtimeTrace?.traceId}
                onChange={setRuntimeTraceId}
                style={{ width: "100%" }}
                options={(runtimeTraceQuery.data ?? []).map((trace) => ({
                  value: trace.traceId,
                  label: `${formatTime(trace.startedAt)} · ${agentDebugModeLabel(trace.mode)} · ${traceOutcomeLabel(trace.outcome ?? trace.status)} · ${trace.events.length} 个事件`,
                }))}
              />
              {runtimeTrace && <ExecutionTraceView trace={runtimeTrace} />}
            </Space>}
    </Card>

    <Card
      title="调试场景"
      extra={<Space wrap><Link to={`?tab=config`}>运行配置</Link><Link to={`?tab=persona`}>人设配置</Link><Link to="/environment">LLM Provider</Link></Space>}
    >
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        <div className="agent-debug-control-row">
          <Text strong>调试场景</Text>
          <Segmented block value={mode} onChange={(value) => setMode(value as AgentDebugMode)} options={AGENT_DEBUG_MODES} />
        </div>
        <div className="agent-debug-control-row">
          <Text strong>消息来源</Text>
          <Segmented value={source} onChange={(value) => setSource(value as "history" | "simulation")} options={[{ value: "history", label: "历史消息回放" }, { value: "simulation", label: "模拟消息" }]} />
        </div>

        {source === "history" ? <Space orientation="vertical" size="small" style={{ width: "100%" }}>
          <Select
            showSearch
            loading={messagesQuery.loading}
            value={messageId || undefined}
            onChange={setMessageId}
            options={messageOptions}
            optionFilterProp="label"
            placeholder="选择保留期内的一条成员消息"
            style={{ width: "100%" }}
            notFoundContent={messagesQuery.error ? "消息记录加载失败" : undefined}
          />
          {selectedMessage && <Card size="small" className="agent-debug-source-preview">
            <Space orientation="vertical" size={4} style={{ width: "100%" }}>
              <Space wrap size={8}>
                <Text strong>{selectedMessage.senderName || selectedMessage.userId}</Text>
                <Tag>{selectedMessage.role}</Tag>
                <Text type="secondary">{formatTime(selectedMessage.receivedAt)}</Text>
              </Space>
              <Paragraph style={{ marginBottom: 0 }}>{selectedMessage.text || "[媒体消息]"}</Paragraph>
            </Space>
          </Card>}
        </Space> : <Row gutter={[16, 16]}>
          <Col xs={24} md={8}>
            <Space orientation="vertical" size={6} style={{ width: "100%" }}>
              <Text strong>模拟发言人</Text>
              <Input value={actorUserId} onChange={(event) => setActorUserId(event.target.value)} placeholder="成员 QQ 号" />
            </Space>
          </Col>
          <Col xs={24} md={16}>
            <Space orientation="vertical" size={6} style={{ width: "100%" }}>
              <Text strong>消息正文</Text>
              <Input.TextArea value={text} onChange={(event) => setText(event.target.value)} autoSize={{ minRows: 3, maxRows: 7 }} maxLength={4000} showCount placeholder="输入要模拟的当前群消息" />
            </Space>
          </Col>
        </Row>}

        <div className="agent-debug-run-row">
          <Space wrap>
            <Switch checked={runModel} onChange={setRunModel} />
            <div>
              <Text strong>{runModel ? "调用真实模型" : "仅构建提示词"}</Text><br />
              <Text type="secondary">{runModel ? "30 秒超时，并发上限 2；仍不会执行任何副作用" : "用于检查上下文、Prompt 和可见工具，不产生模型调用"}</Text>
            </div>
          </Space>
          <Button type="primary" onClick={run} loading={running}>{runModel ? "开始真实试跑" : "生成调试快照"}</Button>
        </div>
      </Space>
    </Card>

    {error && <QueryErrorAlert error={error} onRetry={run} />}

    {result && <>
      {result.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={warning} />)}
      <Card title="本次调试摘要" extra={<Tag color={result.route.configured ? "green" : "red"}>{result.route.configured ? "路由可用" : "路由未配置"}</Tag>}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
          { key: "mode", label: "调试场景", children: AGENT_DEBUG_MODES.find((item) => item.value === result.mode)?.label ?? result.mode },
          { key: "prompt", label: "Prompt 版本", children: result.promptVersion },
          { key: "provider", label: "Provider", children: result.route.provider || "—" },
          { key: "model", label: "模型", children: result.route.model || "—" },
          { key: "profile", label: "路由配置", children: result.route.profile || "—" },
          { key: "thinking", label: "Thinking", children: result.route.thinking || "—" },
          { key: "multimodal", label: "多模态", children: result.route.multimodal || "—" },
          { key: "result", label: "模型结果", children: result.result ? <Tag color={result.result.outcome === "success" ? "green" : "orange"}>{result.result.outcome}</Tag> : <Tag>未调用</Tag> },
        ]} />
      </Card>

      <Card title="发言模拟器" extra={<Tag color="blue">Dry-run · 不发送</Tag>}>
        <DebugSpeechSimulation value={result.speechSimulation} />
      </Card>

      <Card title="调试详情" className="agent-debug-detail-card">
        <Tabs items={[
          { key: "trace", label: `执行轨迹 ${result.executionTrace.events.length}`, children: <ExecutionTraceView trace={result.executionTrace} /> },
          {
            key: "overview",
            label: "概览",
            children: <Row gutter={[16, 16]}>
              <Col xs={24} xl={14}><DebugCurrentTurn value={result.currentTurn} /></Col>
              <Col xs={24} xl={10}><DebugContextBudget stats={result.stats} /></Col>
            </Row>,
          },
          { key: "context", label: "上下文", children: <DebugContextView context={result.context} selection={result.contextSelection} /> },
          { key: "prompt", label: `Prompt ${result.promptMessages.length}`, children: <DebugPromptView messages={result.promptMessages} /> },
          { key: "tools", label: `工具 ${result.tools.length}`, children: <DebugToolsView tools={result.tools} permissions={result.toolPermissions} /> },
          { key: "model", label: "模型结果", children: <DebugModelView result={result.result} /> },
          { key: "raw", label: "原始数据", children: <DebugRawBlock value={result} /> },
        ]} />
      </Card>
    </>}
  </Space>;
}

function AgentMessagesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [role, setRole] = useState("");
  const load = useCallback(() => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&role=${role}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, role]);
  const query = useApiQuery(load);
  return <Card title="短期消息库" extra={<Select value={role} onChange={(value) => { setRole(value); setPage(1); }} options={MEMORY_ROLE_OPTIONS} style={{ width: 120 }} />}>
    <Alert type="info" showIcon className="section-alert" message="仅保留 rawRetentionDays 内的原始消息；隐私退出成员的消息不在此展示，到期由整理任务清除。" />
    <Input.Search className="table-search" placeholder="搜索消息内容或昵称" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.text}</Paragraph> }} columns={[{ title: "时间", dataIndex: "receivedAt", render: formatTime, width: 170 }, { title: "成员", render: (_, row: AgentMessageItem) => <>{row.senderName || "—"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role", width: 90, render: (value: string) => <Tag color={value === "bot" ? "blue" : value === "owner" ? "gold" : value === "admin" ? "cyan" : "default"}>{value}</Tag> }, { title: "内容", dataIndex: "text", ellipsis: true }, { title: "操作", width: 80, render: (_, row: AgentMessageItem) => row.role === "bot" ? null : <Button type="link" size="small" onClick={() => setSearchParams({ tab: "debug", messageId: row.messageId }, { replace: true })}>调试</Button> }]} />
    }</Card>;
}

function PrivacyPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const load = useCallback(() => api<PrivacyItem[]>(`/agent/groups/${groupId}/privacy?pageSize=100`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_privacy", "agent_group_data"] });
  const toggle = async (userId: string, optedOut: boolean) => {
    try {
      await api(`/agent/groups/${groupId}/privacy/${userId}`, { method: "PATCH", body: JSON.stringify({ optedOut }) });
      message.success(optedOut ? "已退出记忆并清除其已有数据" : "已恢复记忆");
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  return <Card><Alert type="info" showIcon message="成员也可以自行通过群命令 /Agent隐私 退出或恢复" description="退出会立即清除该成员已沉淀的记忆与消息；后续消息不再进入 Agent 上下文。" />{
    query.error && !query.data
      ? <div className="section-alert"><QueryErrorAlert error={query.error} onRetry={query.reload} /></div>
      : <Table rowKey="userId" loading={query.loading} dataSource={query.data ?? []} locale={{ emptyText: <Empty description="暂无成员隐私记录" /> }} columns={[{ title: "用户 ID", dataIndex: "userId" }, { title: "状态", dataIndex: "optedOut", render: (value: boolean) => <Tag color={value ? "orange" : "green"}>{value ? "已退出记忆" : "已恢复"}</Tag> }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: PrivacyItem) => row.optedOut ? <Button type="link" onClick={() => toggle(row.userId, false)}>恢复记忆</Button> : <Popconfirm title={`让成员 ${row.userId} 退出 Agent 记忆？`} description="将立即清除其已沉淀的记忆、关系与消息，不可撤销。" onConfirm={() => toggle(row.userId, true)}><Button type="link" danger>退出记忆</Button></Popconfirm> }]} />
  }</Card>;
}

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

function AgentAuditsPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [page, setPage] = useState(1); const [result, setResult] = useState("");
  const load = useCallback(() => api<AgentAudit[]>(`/agent/audits?groupId=${groupId}&page=${page}&pageSize=20&result=${result}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, result]);
  const query = useApiQuery(load);
  return <Card extra={<Select value={result} onChange={(value) => { setResult(value); setPage(1); }} options={RESULT_OPTIONS} style={{ width: 120 }} />}>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <>
        <AgentAuditTable data={query.data?.rows ?? []} />
        <TablePagination current={page} total={query.data?.total ?? 0} onChange={setPage} />
      </>
  }</Card>;
}
