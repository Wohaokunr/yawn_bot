import {
  Alert,
  App as AntApp,
  AutoComplete,
  Button,
  Card,
  Col,
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
  AgentConfig,
  AgentDiagnostics,
  AgentMemoryStatus,
  AgentMessageItem,
  AgentRelationGraph,
  AgentRelationItem,
  GroupSummary,
  MemoryItem,
  MemorySubjectItem,
  Persona,
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

const PERSONA_FIELD_META: Record<string, { label: string; help: string; placeholder: string }> = {
  name: { label: "名字", help: "Agent 在群里自称或被称呼的名字。", placeholder: "例如：Yawn" },
  identity: { label: "身份定位", help: "描述它是谁，以及希望给群友留下的整体印象。", placeholder: "例如：友好、自然、简洁的群友" },
  role: { label: "群内角色", help: "定义它在群聊中的职责和参与方式。", placeholder: "例如：群聊助手" },
  tone: { label: "语气", help: "控制措辞的温度、正式程度和情绪表达。", placeholder: "例如：口语化、温和、不过度热情" },
  speech_style: { label: "表达风格", help: "控制句式、节奏、口头禅以及整体说话方式。", placeholder: "例如：短句为主，偶尔使用轻松语气词" },
  emotion_baseline: { label: "情绪基线", help: "设定日常情绪状态，以及随上下文变化的幅度。", placeholder: "例如：平静、友善，随对话轻微变化" },
  response_length: { label: "回复长度", help: "描述通常回答多长，复杂问题是否允许展开。", placeholder: "例如：通常 1-3 句，复杂问题再展开" },
  values: { label: "价值取向", help: "定义回答时优先遵循的原则和行为偏好。", placeholder: "例如：尊重事实、尊重边界、先倾听再回答" },
  knowledge_boundary: { label: "知识边界", help: "约束不知道或不确定的信息应该如何处理。", placeholder: "例如：不知道就明确说不知道，不猜测成员隐私" },
  privacy_boundary: { label: "隐私边界", help: "明确哪些内容绝不能在群聊中主动公开。", placeholder: "例如：不公开私聊内容、隐私记忆和权限信息" },
};

const PERSONA_SECTIONS = [
  { key: "identity", kicker: "IDENTITY", title: "身份与角色", description: "先定义 Agent 是谁，以及它在这个群里以什么身份参与。", fields: ["name", "identity", "role"] },
  { key: "voice", kicker: "VOICE & TEMPERAMENT", title: "语气与表达", description: "塑造说话的声音、情绪基线和回复节奏。", fields: ["tone", "speech_style", "emotion_baseline", "response_length"] },
  { key: "boundaries", kicker: "VALUES & BOUNDARIES", title: "原则与边界", description: "明确价值取向、知识边界和隐私底线，避免人设覆盖安全约束。", fields: ["values", "knowledge_boundary", "privacy_boundary"] },
] as const;

// 画像成员的展示名：群名片优先、全局昵称兜底，解析失败回退 QQ 号（与关系图谱同口径）。
export function memberDisplayName(
  groupNickname: string | null | undefined,
  nickname: string | null | undefined,
  userId: string,
): string {
  return (groupNickname || nickname || "").trim() || userId;
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

export function triggerModeLabel(mode: string): string {
  return ({
    mention_only: "仅 @",
    mention_or_reply: "@ 或回复",
    explicit_wakeup: "@ 或显式唤醒",
    mention_or_proactive: "@ / 回复 / 唤醒 / 主动",
  } as Record<string, string>)[mode] ?? mode;
}

function AgentOverviewPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const load = useCallback(
    () => api<AgentDiagnostics>(`/agent/groups/${groupId}/diagnostics`).then((r) => r.data),
    [groupId],
  );
  const query = useApiQuery(load, { resources: ["agent_config", "agent_memory", "agent_group_data"] });
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  const effective = data.effective;
  const memory = data.memory;
  const conversation = effective.shortConversation;
  return <Space orientation="vertical" size="large" style={{ width: "100%" }}>
    <Card
      title="实际生效配置"
      extra={<Button onClick={query.reload} loading={query.refreshing}>刷新诊断</Button>}
    >
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}><Statistic title="Agent" value={effective.enabled ? "开启" : "关闭"} /></Col>
        <Col xs={12} md={6}><Statistic title="触发模式" value={triggerModeLabel(effective.triggerMode)} /></Col>
        <Col xs={12} md={6}><Statistic title="今日主动额度" value={effective.dailyRemaining} suffix={`/ ${effective.dailyLimit}`} /></Col>
        <Col xs={12} md={6}><Statistic title="主动冷却剩余" value={effective.cooldownRemainingMinutes} suffix="分钟" /></Col>
      </Row>
      <Row gutter={[16, 16]} className="section-row">
        <Col xs={24} lg={8}>
          <Card size="small" title="主动发言">
            <Space orientation="vertical" size={6}>
              <Text>模式：<Tag color={effective.proactiveEnabled ? "green" : "default"}>{effective.proactiveEnabled ? "允许主动" : "仅被动"}</Tag></Text>
              <Text>热闹插话：{effective.proactiveActiveEnabled ? "开启" : "关闭"}</Text>
              <Text>上次主动：{formatTime(effective.lastProactiveAt)}</Text>
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

function AgentConfigPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [saving, setSaving] = useState(false); const [dirty, setDirty] = useState(false);
  const load = useCallback(() => api<AgentConfig>(`/agent/groups/${groupId}/config`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_config"] });
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
                  <h3>触发与会话</h3>
                  <p>决定什么时候响应，以及一次回复后是否继续自然续聊。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="triggerMode" label="触发模式" rules={[{ required: true }]}>
                  <Select
                    options={[
                      { value: "mention_only", label: "仅 @" },
                      { value: "mention_or_reply", label: "@ 或回复" },
                      { value: "explicit_wakeup", label: "@ 或显式唤醒" },
                      { value: "mention_or_proactive", label: "@ / 回复 / 唤醒 / 主动" },
                    ]}
                  />
                </Form.Item>
                <div className="agent-config-toggle-card">
                  <div>
                    <div className="agent-config-toggle-title">短会话续聊</div>
                    <div className="agent-config-toggle-help">Bot 回复后，可在同一话题中继续自然接话。</div>
                  </div>
                  <Form.Item name="shortConversationEnabled" valuePropName="checked" noStyle>
                    <Switch />
                  </Form.Item>
                </div>
              </div>
            </section>

            <section className="agent-config-section liquid-glass agent-config-floating">
              <div className="agent-config-section-head">
                <div>
                  <div className="agent-config-section-kicker">PROACTIVE</div>
                  <h3>主动发言</h3>
                  <p>分别控制冷场暖场和群聊活跃时的自然插话，并限制频率。</p>
                </div>
              </div>
              <div className="agent-config-toggle-card agent-config-toggle-card-featured">
                <div>
                  <div className="agent-config-toggle-title">热闹插话</div>
                  <div className="agent-config-toggle-help">群里正在聊天时，允许 Agent 根据上下文自然加入话题。</div>
                </div>
                <Form.Item name="proactiveActiveEnabled" valuePropName="checked" noStyle>
                  <Switch />
                </Form.Item>
              </div>
              <div className="agent-config-grid agent-config-grid-3">
                <Form.Item name="proactiveProbability" label="冷场暖场概率" extra="每次满足冷场条件后的发言概率">
                  <InputNumber min={0} max={1} step={0.05} />
                </Form.Item>
                <Form.Item name="proactiveActiveProbability" label="热闹插话概率" extra="活跃窗口内进入候选后的插话概率">
                  <InputNumber min={0} max={1} step={0.02} />
                </Form.Item>
                <Form.Item name="proactiveActiveWindowMinutes" label="活跃窗口" extra="分钟">
                  <InputNumber min={1} max={1440} />
                </Form.Item>
                <Form.Item name="idleThresholdMinutes" label="冷场阈值" extra="连续多少分钟安静后开始考虑暖场">
                  <InputNumber min={1} max={10080} />
                </Form.Item>
                <Form.Item name="cooldownMinutes" label="主动冷却" extra="两次主动发言之间的最短间隔（分钟）">
                  <InputNumber min={0} max={10080} />
                </Form.Item>
                <Form.Item name="dailyLimit" label="每日主动上限" extra="达到后当天停止主动发言">
                  <InputNumber min={0} max={1000} />
                </Form.Item>
              </div>
            </section>

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
                  <h3>管理工具权限</h3>
                  <p>限制 Agent 可以调用的群管理能力，以及每天的调用额度。</p>
                </div>
              </div>
              <div className="agent-config-grid agent-config-grid-2">
                <Form.Item name="adminToolDailyLimit" label="每日管理工具上限" extra="所有允许的管理工具共用此额度">
                  <InputNumber min={1} max={1000} />
                </Form.Item>
                <Form.Item name="toolAllowlist" label="允许的管理工具">
                  <Select
                    mode="multiple"
                    placeholder="未选择时不允许调用管理工具"
                    options={[
                      { value: "mute_member", label: "禁言成员" },
                      { value: "create_group_announcement", label: "发布群公告" },
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
                <li>先设置触发模式</li>
                <li>再调整主动发言频率</li>
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
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const watchedEnabled = Form.useWatch("enabled", form) as boolean | undefined;
  const watchedOverrides = Form.useWatch("overrides", form) as Record<string, string> | undefined;
  const load = useCallback(() => api<Persona>(`/agent/groups/${groupId}/persona`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_persona"] });
  useUnsavedChanges(dirty);
  useEffect(() => {
    if (query.data) {
      form.setFieldsValue({ enabled: query.data.enabled, overrides: query.data.overrides });
      setDirty(false);
    }
  }, [form, query.data]);
  const save = async (values: { enabled: boolean; overrides?: Record<string, string> }) => {
    setSaving(true);
    try {
      await api<Persona>(`/agent/groups/${groupId}/persona`, {
        method: "PUT",
        body: JSON.stringify({ version: query.data?.version, enabled: values.enabled, overrides: values.overrides ?? {} }),
      });
      setDirty(false);
      message.success("群级人设已保存");
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
        headers: query.data?.version ? { "If-Match": query.data.version } : {},
      });
      setDirty(false);
      message.success("已恢复全局默认人设");
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setResetting(false);
    }
  };
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;

  const personaEnabled = watchedEnabled ?? data.enabled;
  const activeOverrideCount = data.fields.filter((field) => Boolean(watchedOverrides?.[field]?.trim())).length;
  const knownFields = new Set<string>(PERSONA_SECTIONS.flatMap((section) => [...section.fields]));
  const extraFields = data.fields.filter((field) => !knownFields.has(field));
  const fieldEditor = (field: string) => {
    const meta = PERSONA_FIELD_META[field] ?? {
      label: field,
      help: "覆盖该字段在当前群中的人设表现。",
      placeholder: "留空则继承全局默认",
    };
    const overridden = Boolean(watchedOverrides?.[field]?.trim());
    return (
      <div className="persona-field-card" key={field}>
        <div className="persona-field-head">
          <div>
            <div className="persona-field-title">{meta.label}</div>
            <div className="persona-field-help">{meta.help}</div>
          </div>
          <Tag color={overridden ? "magenta" : undefined}>{overridden ? "群级覆盖" : "继承默认"}</Tag>
        </div>
        <Form.Item name={["overrides", field]} className="persona-field-input">
          {field === "name" ? (
            <Input maxLength={240} placeholder={meta.placeholder} showCount />
          ) : (
            <Input.TextArea maxLength={240} autoSize={{ minRows: 2, maxRows: 5 }} placeholder={meta.placeholder} showCount />
          )}
        </Form.Item>
        <div className="persona-field-resolved">
          <span>当前已保存的生效值</span>
          <strong>{data.resolved[field] || "—"}</strong>
        </div>
      </div>
    );
  };

  return (
    <Form
      form={form}
      layout="vertical"
      onFinish={save}
      onValuesChange={() => setDirty(true)}
      className="persona-config-form"
    >
      <div className="persona-config-page agent-studio-page agent-studio-persona">
        <section className="persona-config-hero agent-studio-hero liquid-glass agent-config-floating">
          <div className="persona-config-hero-copy">
            <div className="persona-config-eyebrow">PERSONA PROFILE</div>
            <div className="persona-config-title-row">
              <div>
                <h2>群级人设</h2>
                <p>在不修改全局默认的前提下，为当前群塑造独立的身份、语气与行为边界。</p>
              </div>
              <SaveStatus dirty={dirty} saving={saving} />
            </div>
            <div className="persona-config-metrics">
              <div className="persona-config-metric">
                <span>群级覆盖</span>
                <strong>{activeOverrideCount}</strong>
                <small>/ {data.fields.length} 项</small>
              </div>
              <div className="persona-config-metric">
                <span>未覆盖字段</span>
                <strong>{Math.max(0, data.fields.length - activeOverrideCount)}</strong>
                <small>自动继承</small>
              </div>
              <div className="persona-config-metric persona-config-metric-wide">
                <span>当前状态</span>
                <strong>{personaEnabled ? "群级人设生效" : "使用全局默认"}</strong>
              </div>
            </div>
          </div>

          <div className={`persona-master-card${personaEnabled ? " is-enabled" : ""}`}>
            <div className="persona-master-copy">
              <div className="persona-master-label">群级覆盖总开关</div>
              <div className="persona-master-title">{personaEnabled ? "当前正在使用群级人设" : "当前使用全局默认人设"}</div>
              <div className="persona-master-description">
                关闭只会暂停这些覆盖，不会删除已填写内容；重新开启后会继续使用原来的群级设置。
              </div>
            </div>
            <Form.Item name="enabled" valuePropName="checked" noStyle>
              <Switch />
            </Form.Item>
          </div>
        </section>

        {!personaEnabled && (
          <Alert
            className="persona-paused-alert"
            type="info"
            showIcon
            message="群级人设覆盖已暂停"
            description="下面的内容仍可编辑和保存，但 Agent 当前会使用全局默认人设；重新打开总开关后这些覆盖会再次生效。"
          />
        )}

        <div className="persona-config-layout">
          <div className="persona-config-main">
            {PERSONA_SECTIONS.map((section) => {
              const fields = section.fields.filter((field) => data.fields.includes(field));
              if (fields.length === 0) return null;
              return (
                <section className="persona-config-section liquid-glass agent-config-floating" key={section.key}>
                  <div className="persona-config-section-head">
                    <div>
                      <div className="persona-config-section-kicker">{section.kicker}</div>
                      <h3>{section.title}</h3>
                      <p>{section.description}</p>
                    </div>
                    <Tag>{fields.filter((field) => Boolean(watchedOverrides?.[field]?.trim())).length} 项覆盖</Tag>
                  </div>
                  <div className="persona-field-grid">{fields.map(fieldEditor)}</div>
                </section>
              );
            })}
            {extraFields.length > 0 && (
              <section className="persona-config-section liquid-glass agent-config-floating">
                <div className="persona-config-section-head">
                  <div>
                    <div className="persona-config-section-kicker">OTHER</div>
                    <h3>其他字段</h3>
                    <p>后端新增的人设字段会自动出现在这里。</p>
                  </div>
                </div>
                <div className="persona-field-grid">{extraFields.map(fieldEditor)}</div>
              </section>
            )}
          </div>

          <aside className="persona-config-aside">
            <div className="persona-note-card liquid-glass agent-config-floating">
              <div className="persona-note-title">继承规则</div>
              <div className="persona-inherit-flow">
                <div><span>1</span><strong>全局默认</strong><small>作为基础人设</small></div>
                <i>↓</i>
                <div><span>2</span><strong>群级覆盖</strong><small>仅替换已填写字段</small></div>
                <i>↓</i>
                <div><span>3</span><strong>最终生效</strong><small>注入当前群对话</small></div>
              </div>
            </div>
            <div className="persona-note-card persona-note-soft liquid-glass agent-config-floating">
              <div className="persona-note-title">编辑建议</div>
              <p>优先调整身份、语气和回复长度；除非确有需要，不必把所有字段都复制一遍。</p>
              <p>知识边界与隐私边界建议写成明确规则，而不是模糊的性格描述。</p>
            </div>
          </aside>
        </div>

        <div className="persona-config-savebar liquid-glass agent-config-floating">
          <div className="persona-save-state">
            <strong>{dirty ? "人设有未保存的修改" : "人设配置已同步"}</strong>
            <span>{dirty ? `当前准备覆盖 ${activeOverrideCount} 个字段` : "留空字段会继续继承全局默认值"}</span>
          </div>
          <Space>
            <Popconfirm
              title="恢复全局默认人设？"
              description="会清空当前群的全部人设覆盖，并重新启用全局默认。"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={reset}
            >
              <Button loading={resetting}>恢复默认</Button>
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

function MemoriesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm();
  const load = useCallback(() => api<MemoryItem[]>(`/agent/groups/${groupId}/memories?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search]);
  const query = useApiQuery(load, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const statusLoad = useCallback(() => api<AgentMemoryStatus>(`/agent/groups/${groupId}/memories/status`).then((r) => r.data), [groupId]);
  const statusQuery = useApiQuery(statusLoad, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const status = statusQuery.data;
  const remove = async (id: string) => { await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" }); message.success("记忆已删除"); query.reload(); };
  const removeMember = async (userId: string) => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/members/${userId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条成员数据`); query.reload(); };
  const removeGroup = async () => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条群 Agent 数据`); query.reload(); };
  const exportData = async () => { const result = await api(`/agent/groups/${groupId}/memories/export`); const blob = new Blob([JSON.stringify(result.data, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `yawnbot-agent-${groupId}.json`; anchor.click(); URL.revokeObjectURL(url); };
  const compact = async () => {
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
    try {
      await api(`/agent/groups/${groupId}/memories/rebuild`, { method: "POST" });
      message.success("派生记忆重建已启动，手工记忆会保留");
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 3000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const openEdit = (row: MemoryItem) => setEditing(row);
  const saveEdit = async (values: MemoryFormValues) => {
    if (!editing) return;
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
  return <>
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
    <Card title="公开/群级记忆" extra={<Space><Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增记忆</Button><Popconfirm title="立即整理本群记忆？" description="含 LLM 摘要，将在后台运行数十秒。" onConfirm={compact}><Button loading={status?.inFlight}>立即整理</Button></Popconfirm><Popconfirm title="重建全部自动派生记忆？" description="保留手工记忆，清除自动摘要/画像/关系后从短期消息重新生成。" onConfirm={rebuild}><Button>重建派生记忆</Button></Popconfirm><Button onClick={exportData}>导出 JSON</Button><Popconfirm title="清理整个群的消息、记忆、关系和媒体缓存？" description="此操作还会重置上下文游标，且不可撤销。" onConfirm={removeGroup}><DangerActionButton>清理全群 Agent 数据</DangerActionButton></Popconfirm></Space>}><Input.Search className="table-search" placeholder="搜索 key 或内容" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.content}</Paragraph> }} columns={[{ title: "记忆", render: (_, row: MemoryItem) => <><Text strong>{row.key}</Text><br /><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag><Text type="secondary"> · {row.visibility} · {row.sourceKind === "manual" ? "手工" : "自动"}</Text></> }, { title: "成员", dataIndex: "subjectUserId", render: (value?: string) => value ? <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSearchParams({ tab: "profiles", userId: value }, { replace: true })}>{value}</Button> : "群级" }, { title: "权重", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.salience * 100)} size="small" /> }, { title: "置信度", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" strokeColor="var(--ant-color-success)" /> }, { title: "有效期至", dataIndex: "expiresAt", render: (value?: string | null) => value ? formatTime(value) : "永久" }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: MemoryItem) => <Space><Button type="link" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" danger>删除</Button></Popconfirm>{row.subjectUserId && <Popconfirm title={`清理成员 ${row.subjectUserId} 的全部 Agent 数据？`} onConfirm={() => removeMember(row.subjectUserId!)}><Button type="link" danger>清理成员</Button></Popconfirm>}</Space> }]} />
    }</Card>
    <MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />
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
    </Drawer>
  </>;
}

// 画像分组展示顺序：core 为反复确认晋升的不过期事实，置前展示。
const PROFILE_TYPE_ORDER = ["core", "profile", "manual"];

function MemberProfilesPanel({ groupId }: { groupId: string }): React.JSX.Element {
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
    await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" });
    message.success("记忆已删除");
    memberQuery.reload(); subjectsQuery.reload();
  };
  const saveEdit = async (values: MemoryFormValues) => {
    if (!editing) return;
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
      <Alert type="info" showIcon className="section-alert" message="画像由记忆整理自动生成，也可在「记忆」页手工新增；已退出记忆（隐私）的成员不展示。" />
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
                    <List.Item actions={[
                      <Button key="edit" type="link" size="small" onClick={() => setEditing(row)}>编辑</Button>,
                      <Popconfirm key="remove" title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" size="small" danger>删除</Button></Popconfirm>,
                    ]}>
                      <List.Item.Meta
                        title={<Space wrap size={[8, 4]}>
                          <Text strong>{profileKeyLabel(row.key)}</Text>
                          {row.key !== profileKeyLabel(row.key) && <Text type="secondary">{row.key}</Text>}
                          <Text type="secondary">{row.sourceKind === "manual" ? "手工" : "自动"} · {row.expiresAt ? `有效期至 ${formatTime(row.expiresAt)}` : "永久"} · 更新 {formatTime(row.updatedAt)}</Text>
                        </Space>}
                        description={<>
                          <Paragraph copyable style={{ marginBottom: 8 }}>{row.content}</Paragraph>
                          <Space wrap size={[16, 4]}>
                            <Space size={6}>置信度<Progress percent={Math.round(row.confidence * 100)} size="small" style={{ width: 90 }} strokeColor="var(--ant-color-success)" /></Space>
                            <Space size={6}>显著度<Progress percent={Math.round(row.salience * 100)} size="small" style={{ width: 90 }} /></Space>
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
    <MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />
  </>;
}

function RelationsPanel({ groupId }: { groupId: string }): React.JSX.Element {
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
  const remove = async (id: string) => { await api(`/agent/groups/${groupId}/relations/${id}`, { method: "DELETE" }); message.success("关系边已删除"); query.reload(); typesQuery.reload(); graphQuery.reload(); };
  const saveCreate = async (values: { subjectUserId: number; objectUserId: number; type: string; note: string; confidence: number }) => {
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations`, { method: "POST", body: JSON.stringify(values) });
      message.success("关系边已新增"); setCreating(false); createForm.resetFields(); query.reload(); typesQuery.reload(); graphQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const openEdit = (row: AgentRelationItem) => { setEditing(row); editForm.setFieldsValue({ note: row.note, confidence: row.confidence }); };
  const saveEdit = async (values: { note: string; confidence: number }) => {
    if (!editing) return;
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
      <Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增关系边</Button>
    </Space>}>{
      view === "graph"
        ? (graphQuery.error && !graph
          ? <QueryErrorAlert error={graphQuery.error} onRetry={graphQuery.reload} />
          : graph
            ? <Suspense fallback={<div className="rg-loading-wrap"><Spin /></div>}><LazyRelationGraphView graph={graph} typeFilter={typeFilter} onEditRelation={openEdit} onDeleteRelation={(edge) => remove(edge.id)} /></Suspense>
            : <div className="rg-loading-wrap"><Spin /></div>)
        : (query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: <Empty description="暂无关系记忆" /> }} columns={[
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
    <Drawer open={creating} width={520} title="新增关系边" onClose={() => setCreating(false)}>
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
    </Drawer>
  </>;
}

function AgentMessagesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [role, setRole] = useState("");
  const load = useCallback(() => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&role=${role}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, role]);
  const query = useApiQuery(load);
  return <Card title="短期消息库" extra={<Select value={role} onChange={(value) => { setRole(value); setPage(1); }} options={MEMORY_ROLE_OPTIONS} style={{ width: 120 }} />}>
    <Alert type="info" showIcon className="section-alert" message="仅保留 rawRetentionDays 内的原始消息；隐私退出成员的消息不在此展示，到期由整理任务清除。" />
    <Input.Search className="table-search" placeholder="搜索消息内容或昵称" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.text}</Paragraph> }} columns={[{ title: "时间", dataIndex: "receivedAt", render: formatTime, width: 170 }, { title: "成员", render: (_, row: AgentMessageItem) => <>{row.senderName || "—"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role", width: 90, render: (value: string) => <Tag color={value === "bot" ? "blue" : value === "owner" ? "gold" : value === "admin" ? "cyan" : "default"}>{value}</Tag> }, { title: "内容", dataIndex: "text", ellipsis: true }]} />
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
