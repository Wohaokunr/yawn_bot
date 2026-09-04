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
  Tag,
  Timeline,
  Typography,
} from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { AgentAuditTable } from "./agent-audit-table";
import { AgentDebugger } from "./agent-debug/AgentDebugger";
import { TraceCompareView } from "./agent-debug/TraceWorkspace";
import { api, ApiError } from "./api";
import { PageFrame, PanelStack } from "./layout";
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
  AgentDebugResponse,
  AgentDiagnostics,
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

import { AgentConfigPanel } from "./agent-panels/AgentConfigPanel";
import { PersonaPanel } from "./agent-panels/PersonaPanel";
import { MemoriesPanel } from "./agent-panels/MemoriesPanel";
import { MemberProfilesPanel } from "./agent-panels/MemberProfilesPanel";
import { RelationsPanel } from "./agent-panels/RelationsPanel";
import { AgentMessagesPanel } from "./agent-panels/AgentMessagesPanel";
import { PrivacyPanel } from "./agent-panels/PrivacyPanel";
import { AgentAuditsPanel } from "./agent-panels/AgentAuditsPanel";

export {
  MEMORY_TYPE_META, PROFILE_KEY_META, RELATION_TYPE_PRESETS,
  memoryTypeLabel, profileKeyLabel, memberDisplayName, mergePersonaPreset,
  personaDraftSummary, personaBehaviorPreview, personaEmotionExpressionPreview,
} from "./agent-meta";
export { MemoriesPanel } from "./agent-panels/MemoriesPanel";
export { MemberProfilesPanel } from "./agent-panels/MemberProfilesPanel";
export { RelationsPanel } from "./agent-panels/RelationsPanel";
export { debugMessageLabel } from "./agent-debug/debug-utils";

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

const AGENT_DETAIL_NAV = [
  { key: "overview", label: "运行诊断" },
  { key: "config", label: "运行配置" },
  { key: "persona", label: "人设" },
  { key: "memories", label: "记忆" },
  { key: "profiles", label: "成员画像" },
  { key: "relations", label: "关系边" },
  { key: "messages", label: "消息记录" },
  { key: "debug", label: "对话调试" },
  { key: "privacy", label: "隐私退出" },
  { key: "audit", label: "工具审计" },
] as const;

type AgentDetailTab = typeof AGENT_DETAIL_NAV[number]["key"];

export function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") ?? "overview";
  const activeTab = (AGENT_DETAIL_NAV.some((item) => item.key === requestedTab) ? requestedTab : "overview") as AgentDetailTab;
  const changeTab = (key: string) => {
    if (key === activeTab) return;
    if (!confirmDiscardChanges()) return;
    const next = new URLSearchParams(searchParams);
    if (key === "overview") next.delete("tab");
    else next.set("tab", key);
    setSearchParams(next, { replace: true });
  };

  const panel = (() => {
    switch (activeTab) {
      case "config": return <AgentConfigPanel groupId={groupId} />;
      case "persona": return <PersonaPanel groupId={groupId} />;
      case "memories": return <MemoriesPanel groupId={groupId} />;
      case "profiles": return <MemberProfilesPanel groupId={groupId} />;
      case "relations": return <RelationsPanel groupId={groupId} />;
      case "messages": return <AgentMessagesPanel groupId={groupId} />;
      case "debug": return <AgentDebugger groupId={groupId} />;
      case "privacy": return <PrivacyPanel groupId={groupId} />;
      case "audit": return <AgentAuditsPanel groupId={groupId} />;
      default: return <AgentOverviewPanel groupId={groupId} />;
    }
  })();

  return (
    <PageFrame className="agent-detail-page">
      <div className="agent-detail-heading">
        <PageHeader
          title={`Agent · ${groupId}`}
          subtitle="群级运行状态、配置、人设、记忆与数据治理"
          extra={<Link to="/agent">返回 Agent 列表</Link>}
        />
      </div>
      <nav className="agent-studio-nav-shell" aria-label="Agent 功能导航">
        <div className="agent-studio-nav-scroll" role="tablist" aria-label="Agent 功能">
          {AGENT_DETAIL_NAV.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={activeTab === item.key}
              aria-controls="agent-studio-panel"
              className={`agent-studio-nav-button${activeTab === item.key ? " is-active" : ""}`}
              onClick={() => changeTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <Select
          className="agent-studio-nav-select"
          value={activeTab}
          onChange={changeTab}
          options={AGENT_DETAIL_NAV.map((item) => ({ value: item.key, label: item.label }))}
          aria-label="选择 Agent 功能"
        />
      </nav>
      <div id="agent-studio-panel" className="agent-studio-panel" role="tabpanel">
        {panel}
      </div>
    </PageFrame>
  );
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
  return <PanelStack>
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
  </PanelStack>;
}
