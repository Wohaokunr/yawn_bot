import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  List,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { lazy, Suspense, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "./api";
import {
  AdminEmpty,
  formatTime,
  PageHeader,
  QueryErrorAlert,
  useApiQuery,
} from "./shared";
import type { AgentCapabilities, AgentDiagnostics, GroupSummary } from "./types";

const { Text } = Typography;

const LazyAgentConfigPanel = lazy(() =>
  import("./agent-panels/AgentConfigPanel").then(({ AgentConfigPanel }) => ({ default: AgentConfigPanel })),
);
const LazyPersonaPanel = lazy(() =>
  import("./agent-panels/PersonaPanel").then(({ PersonaPanel }) => ({ default: PersonaPanel })),
);
const LazyMemoriesPanel = lazy(() =>
  import("./agent-panels/MemoriesPanel").then(({ MemoriesPanel }) => ({ default: MemoriesPanel })),
);
const LazyMemberProfilesPanel = lazy(() =>
  import("./agent-panels/MemberProfilesPanel").then(({ MemberProfilesPanel }) => ({ default: MemberProfilesPanel })),
);
const LazyRelationsPanel = lazy(() =>
  import("./agent-panels/RelationsPanel").then(({ RelationsPanel }) => ({ default: RelationsPanel })),
);
const LazyAgentMessagesPanel = lazy(() =>
  import("./agent-panels/AgentMessagesPanel").then(({ AgentMessagesPanel }) => ({ default: AgentMessagesPanel })),
);
const LazyAgentDebugger = lazy(() =>
  import("./agent-debug/AgentDebugger").then(({ AgentDebugger }) => ({ default: AgentDebugger })),
);
const LazyPrivacyPanel = lazy(() =>
  import("./agent-panels/PrivacyPanel").then(({ PrivacyPanel }) => ({ default: PrivacyPanel })),
);
const LazyAgentAuditsPanel = lazy(() =>
  import("./agent-panels/AgentAuditsPanel").then(({ AgentAuditsPanel }) => ({ default: AgentAuditsPanel })),
);

const AGENT_TAB_KEYS = new Set([
  "overview",
  "config",
  "persona",
  "memories",
  "profiles",
  "relations",
  "messages",
  "debug",
  "privacy",
  "audit",
]);

type AgentSectionKey = "runtime" | "knowledge" | "debug" | "governance";

const AGENT_SECTION_BY_TAB: Record<string, AgentSectionKey> = {
  overview: "runtime",
  config: "runtime",
  persona: "runtime",
  memories: "knowledge",
  profiles: "knowledge",
  relations: "knowledge",
  messages: "knowledge",
  debug: "debug",
  privacy: "governance",
  audit: "governance",
};

const AGENT_SECTION_DEFAULT: Record<AgentSectionKey, string> = {
  runtime: "overview",
  knowledge: "memories",
  debug: "debug",
  governance: "privacy",
};

interface AgentGroupDetail {
  groupId: string;
  groupName?: string | null;
  memberCount: number;
}

function AgentPanelFallback(): React.JSX.Element {
  return <div style={{ display: "grid", minHeight: 180, placeItems: "center" }}><Spin /></div>;
}

function panel(content: React.ReactNode): React.JSX.Element {
  return <Suspense fallback={<AgentPanelFallback />}>{content}</Suspense>;
}

export function AgentGroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const query = useApiQuery({
    queryKey: ["agent-groups", page, search],
    fetcher: (signal) => api<GroupSummary[]>(
      `/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`,
      { signal },
    ).then((response) => ({ rows: response.data, total: response.meta.total ?? 0 })),
    invalidation: { resources: ["agent_config"] },
  });

  return <>
    <PageHeader
      title="Agent 管理"
      subtitle="选择群组配置触发、人设、记忆和工具策略"
      onRefresh={query.reload}
      refreshing={query.refreshing}
      extra={
        <Input.Search
          placeholder="搜索群组"
          allowClear
          onSearch={(value) => {
            setSearch(value);
            setPage(1);
          }}
        />
      }
    />
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table
            rowKey="groupId"
            loading={query.loading}
            dataSource={query.data?.rows ?? []}
            locale={{ emptyText: <AdminEmpty description="暂无可管理群组" /> }}
            pagination={{
              current: page,
              pageSize: 20,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              onChange: setPage,
            }}
            columns={[
              {
                title: "群组",
                render: (_, row: GroupSummary) => <>
                  {row.groupName || "未命名群"}<br />
                  <Text type="secondary">{row.groupId}</Text>
                </>,
              },
              { title: "成员", dataIndex: "memberCount" },
              {
                title: "状态",
                render: (_, row: GroupSummary) => (
                  <Tag color={row.agentEnabled ? "green" : "default"}>
                    {row.agentEnabled ? "开启" : "关闭"}
                  </Tag>
                ),
              },
              {
                title: "操作",
                render: (_, row: GroupSummary) => <Link to={`/agent/${row.groupId}`}>进入管理</Link>,
              },
            ]}
          />}
    </Card>
  </>;
}

export function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") ?? "overview";
  const tab = AGENT_TAB_KEYS.has(requestedTab) ? requestedTab : "overview";
  const section = AGENT_SECTION_BY_TAB[tab] ?? "runtime";
  const [lastLeafBySection, setLastLeafBySection] = useState<Record<AgentSectionKey, string>>({
    ...AGENT_SECTION_DEFAULT,
  });
  const groupQuery = useApiQuery({
    queryKey: ["agent-group-detail", groupId],
    fetcher: (signal) => api<AgentGroupDetail>(`/groups/${groupId}`, { signal }).then((response) => response.data),
    invalidation: {
      resources: ["agent_group_data", "agent_config"],
      scope: { groupId },
    },
  });

  useEffect(() => {
    if (requestedTab === tab) return;
    const next = new URLSearchParams(searchParams);
    next.delete("tab");
    setSearchParams(next, { replace: true });
  }, [requestedTab, searchParams, setSearchParams, tab]);

  useEffect(() => {
    setLastLeafBySection((current) => current[section] === tab
      ? current
      : { ...current, [section]: tab });
  }, [section, tab]);

  const changeTab = (key: string) => {
    if (!AGENT_TAB_KEYS.has(key)) return;
    const next = new URLSearchParams(searchParams);
    if (key === "overview") next.delete("tab"); else next.set("tab", key);
    setSearchParams(next, { replace: true });
  };
  const changeSection = (key: string) => {
    if (!(key in AGENT_SECTION_DEFAULT)) return;
    const sectionKey = key as AgentSectionKey;
    changeTab(lastLeafBySection[sectionKey] ?? AGENT_SECTION_DEFAULT[sectionKey]);
  };

  const groupName = groupQuery.data?.groupName?.trim() || "未命名群";
  const title = groupQuery.data ? `Agent · ${groupName}（${groupId}）` : `Agent · ${groupId}`;

  return <>
    <PageHeader
      title={title}
      subtitle="群级运行状态、知识、调试与数据治理"
      extra={<Link to="/agent">返回 Agent 列表</Link>}
    />
    <Tabs
      className="agent-ia-tabs"
      activeKey={section}
      onChange={changeSection}
      items={[
        {
          key: "runtime",
          label: "运行",
          children: <div className="agent-ia-section">
            <Tabs
              className="agent-ia-subtabs"
              activeKey={tab}
              onChange={changeTab}
              items={[
                {
                  key: "overview",
                  label: "诊断",
                  children: <AgentOverviewPanel groupId={groupId} onNavigate={changeTab} />,
                },
                {
                  key: "config",
                  label: "配置",
                  children: panel(<LazyAgentConfigPanel groupId={groupId} />),
                },
                {
                  key: "persona",
                  label: "人设",
                  children: panel(<LazyPersonaPanel groupId={groupId} />),
                },
              ]}
            />
          </div>,
        },
        {
          key: "knowledge",
          label: "知识",
          children: <div className="agent-ia-section">
            <Tabs
              className="agent-ia-subtabs"
              activeKey={tab}
              onChange={changeTab}
              items={[
                {
                  key: "memories",
                  label: "记忆",
                  children: panel(<LazyMemoriesPanel groupId={groupId} />),
                },
                {
                  key: "profiles",
                  label: "画像",
                  children: panel(<LazyMemberProfilesPanel groupId={groupId} />),
                },
                {
                  key: "relations",
                  label: "关系",
                  children: panel(<LazyRelationsPanel groupId={groupId} />),
                },
                {
                  key: "messages",
                  label: "消息",
                  children: panel(<LazyAgentMessagesPanel groupId={groupId} />),
                },
              ]}
            />
          </div>,
        },
        {
          key: "debug",
          label: "调试",
          children: <div className="agent-ia-section agent-ia-single">
            <div className="agent-ia-section-label">Dialogue Debug</div>
            {panel(<LazyAgentDebugger groupId={groupId} />)}
          </div>,
        },
        {
          key: "governance",
          label: "治理",
          children: <div className="agent-ia-section">
            <Tabs
              className="agent-ia-subtabs"
              activeKey={tab}
              onChange={changeTab}
              items={[
                {
                  key: "privacy",
                  label: "隐私",
                  children: panel(<LazyPrivacyPanel groupId={groupId} />),
                },
                {
                  key: "audit",
                  label: "审计",
                  children: panel(<LazyAgentAuditsPanel groupId={groupId} />),
                },
              ]}
            />
          </div>,
        },
      ]}
    />
  </>;
}

const LLM_TASK_LABELS: Record<string, string> = {
  agent_dialogue: "群聊对话",
  agent_proactive: "主动发言",
  agent_memory: "记忆整理",
  agent_image: "图片理解",
};

function AgentOverviewPanel({
  groupId,
  onNavigate,
}: {
  groupId: string;
  onNavigate: (tab: string) => void;
}): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["agent-diagnostics", groupId],
    fetcher: (signal) => api<AgentDiagnostics>(
      `/agent/groups/${groupId}/diagnostics`,
      { signal },
    ).then((response) => response.data),
    invalidation: {
      resources: ["agent_config", "agent_memory", "agent_group_data"],
      scope: { groupId },
    },
  });
  const capabilityQuery = useApiQuery({
    queryKey: ["agent-capabilities", groupId],
    fetcher: (signal) => api<AgentCapabilities>(
      `/agent/groups/${groupId}/capabilities`,
      { signal },
    ).then((response) => response.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const [capabilityRefreshing, setCapabilityRefreshing] = useState(false);
  const data = query.data;
  if (!data) {
    return query.error
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Spin />;
  }

  const effective = data.effective;
  const memory = data.memory;
  const conversation = effective.shortConversation;
  const capabilities = capabilityQuery.data;
  const refreshCapabilities = async (): Promise<void> => {
    setCapabilityRefreshing(true);
    try {
      await api<AgentCapabilities>(`/agent/groups/${groupId}/capabilities/refresh`, { method: "POST" });
      capabilityQuery.reload();
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
        <Button type="link" onClick={() => onNavigate("config")}>调整运行配置</Button>
        <Button type="link" onClick={() => onNavigate("memories")}>检查记忆治理</Button>
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

    <Card
      title="LLM 实际路由"
      extra={data.llm.unconfiguredProviders.length > 0
        ? <Tag color="red">Provider 未配置</Tag>
        : <Tag color="green">路由可用</Tag>}
    >
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