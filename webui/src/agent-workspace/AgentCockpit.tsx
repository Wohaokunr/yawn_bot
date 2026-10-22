import {
  ApiOutlined,
  BugOutlined,
  ClearOutlined,
  CopyOutlined,
  ReloadOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, List, Popconfirm, Row, Segmented, Space, Spin, Switch, Tag, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { triggerSourceLabel } from "../agent-debug/debug-utils";
import { api } from "../api";
import { AdminEmpty, type ApiQuery, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentActivity, AgentCapabilities, AgentDiagnostics, AgentExecutionTraceSummary } from "../types";
import { ActivityChart } from "./ActivityChart";
import { diagnosticsReport, healthScore, healthTone, MemoryHealthCard, PersonaMoodCard, ToolUsageCard } from "./CockpitExtras";
import { formatDuration, relativeTime } from "./polling";

const { Text } = Typography;

const LLM_TASK_LABELS: Record<string, string> = {
  agent_dialogue: "群聊对话",
  agent_proactive: "主动发言",
  agent_memory: "记忆整理",
  agent_image: "图片理解",
};

type Navigate = (tab: string, extra?: Record<string, string>) => void;

function traceTone(trace: AgentExecutionTraceSummary): { color: string; label: string } {
  if (trace.hasFailure) return { color: "red", label: "失败" };
  if (trace.hasDegradation || trace.hasOutboundProblem) return { color: "orange", label: "降级" };
  if (trace.status === "running") return { color: "processing", label: "进行中" };
  if (trace.outcome === "wait" || trace.outcome === "close") return { color: "default", label: "未发言" };
  return { color: "green", label: "完成" };
}

function peakLabel(hour: string, span: number): string {
  const date = new Date(hour);
  const clock = `${String(date.getHours()).padStart(2, "0")}:00`;
  return span > 24 ? `${date.getMonth() + 1}/${date.getDate()} ${clock}` : clock;
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function ActivityCard({ groupId, onNavigate }: { groupId: string; onNavigate: Navigate }): React.JSX.Element {
  const [hours, setHours] = useState(24);
  const query = useApiQuery({
    queryKey: ["agent-activity", groupId, hours],
    fetcher: (signal) => api<AgentActivity>(`/agent/groups/${groupId}/activity?hours=${hours}`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_group_data"], scope: { groupId } },
  });
  const data = query.data;
  const replyShare = data && data.totals.members > 0 ? data.totals.bot / data.totals.members : null;
  const peak = data?.buckets.reduce<AgentActivity["buckets"][number] | null>(
    (best, bucket) => (bucket.members + bucket.bot > (best ? best.members + best.bot : 0) ? bucket : best),
    null,
  );
  return (
    <Card
      className="agent-cockpit-card"
      title="群聊活跃度"
      extra={<Segmented
        size="small"
        value={hours}
        onChange={(value) => setHours(Number(value))}
        options={[{ value: 6, label: "6h" }, { value: 24, label: "24h" }, { value: 72, label: "3 天" }, { value: 168, label: "7 天" }]}
      />}
    >
      {!data
        ? query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />
        : <Spin spinning={query.loading}>
          <div className="agent-cockpit-kpis">
            <div><span>成员消息</span><strong>{data.totals.members}</strong></div>
            <div><span>Bot 回复</span><strong>{data.totals.bot}</strong></div>
            <div><span>活跃成员</span><strong>{data.totals.activeMembers}</strong></div>
            <div><span>回复占比</span><strong>{replyShare === null ? "—" : percent(replyShare)}</strong></div>
          </div>
          <ActivityChart activity={data} />
          <div className="agent-activity-legend">
            <span><i className="is-members" />成员消息</span>
            <span><i className="is-bot" />Bot 回复</span>
            {peak && <span className="agent-activity-peak">高峰 {peakLabel(peak.hour, hours)} · {peak.members + peak.bot} 条</span>}
          </div>
          {data.topMembers.length > 0 && (
            <div className="agent-cockpit-top">
              <div className="agent-cockpit-subtitle">最活跃成员</div>
              <div className="agent-cockpit-chips">
                {data.topMembers.map((member) => (
                  <button
                    key={member.userId}
                    type="button"
                    className="agent-cockpit-chip"
                    onClick={() => onNavigate("profiles", { "profiles.userId": member.userId })}
                    title="查看成员画像"
                  >
                    {member.name || member.userId}<em>{member.count}</em>
                  </button>
                ))}
              </div>
            </div>
          )}
        </Spin>}
    </Card>
  );
}

const PULSE_FILTERS: Array<{ value: string; label: string; match: (trace: AgentExecutionTraceSummary) => boolean }> = [
  { value: "all", label: "全部", match: () => true },
  { value: "failed", label: "失败", match: (trace) => trace.hasFailure },
  { value: "degraded", label: "降级", match: (trace) => trace.hasDegradation || trace.hasOutboundProblem },
  { value: "tool", label: "工具", match: (trace) => trace.hasTool },
  { value: "proactive", label: "主动", match: (trace) => String(trace.triggerSource ?? "").startsWith("proactive") },
];

function PulseCard({ groupId, onNavigate }: { groupId: string; onNavigate: Navigate }): React.JSX.Element {
  const [filter, setFilter] = useState("all");
  const query = useApiQuery({
    queryKey: ["agent-execution-pulse", groupId],
    fetcher: (signal) => api<AgentExecutionTraceSummary[]>(`/agent/groups/${groupId}/execution-traces`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const rows = (query.data ?? []).filter((trace) => trace.source !== "debug");
  const matcher = PULSE_FILTERS.find((item) => item.value === filter)?.match ?? (() => true);
  const visibleRows = rows.filter(matcher);
  const finished = rows.filter((trace) => trace.durationMs !== null);
  const failures = rows.filter((trace) => trace.hasFailure).length;
  const degraded = rows.filter((trace) => !trace.hasFailure && (trace.hasDegradation || trace.hasOutboundProblem)).length;
  const avgDuration = finished.length
    ? finished.reduce((sum, trace) => sum + (trace.durationMs ?? 0), 0) / finished.length
    : null;
  return (
    <Card
      className="agent-cockpit-card"
      title="执行脉搏"
      extra={<Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>}
    >
      {!query.data
        ? query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />
        : <>
          <div className="agent-cockpit-kpis">
            <div><span>最近回合</span><strong>{rows.length}</strong></div>
            <div><span>成功率</span><strong>{rows.length ? percent((rows.length - failures) / rows.length) : "—"}</strong></div>
            <div><span>平均耗时</span><strong>{formatDuration(avgDuration)}</strong></div>
            <div><span>异常</span><strong className={failures ? "is-danger" : undefined}>{failures} / {degraded}</strong></div>
          </div>
          <Segmented
            size="small"
            className="agent-pulse-filter"
            value={filter}
            onChange={(value) => setFilter(String(value))}
            options={PULSE_FILTERS.map((item) => ({ value: item.value, label: item.label }))}
          />
          <List
            size="small"
            className="agent-pulse-list"
            dataSource={visibleRows.slice(0, 6)}
            locale={{ emptyText: <AdminEmpty description="本进程启动后还没有真实执行回合" /> }}
            renderItem={(trace) => {
              const tone = traceTone(trace);
              return (
                <List.Item className="agent-pulse-item" onClick={() => onNavigate("debug", { "debug.trace": trace.traceId })}>
                  <div className="agent-feed-row">
                    <Tag color={tone.color}>{tone.label}</Tag>
                    <span className="agent-feed-main">{triggerSourceLabel(trace.triggerSource ?? trace.mode)}</span>
                    {trace.hasTool && <Tag className="agent-feed-flag">工具</Tag>}
                    {trace.hasMedia && <Tag className="agent-feed-flag">媒体</Tag>}
                    <span className="agent-feed-meta">{formatDuration(trace.durationMs)}</span>
                    <span className="agent-feed-time">{relativeTime(trace.startedAt)}</span>
                  </div>
                </List.Item>
              );
            }}
          />
          <Text type="secondary" className="agent-cockpit-footnote">Trace 只保存在当前进程内存，重启后清空；点一条可在调试台查看完整事件。</Text>
        </>}
    </Card>
  );
}

function HealthCard({
  groupId,
  data,
  onNavigate,
  onChanged,
}: {
  groupId: string;
  data: AgentDiagnostics;
  onNavigate: Navigate;
  onChanged: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, path: string, success: string): Promise<void> => {
    setBusy(key);
    try {
      await api(path, { method: "POST" });
      message.success(success);
      onChanged();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const conversation = data.effective.shortConversation;
  return (
    <Card className="agent-cockpit-card" title="健康检查与快捷操作">
      {data.blockers.length === 0
        ? <Alert type="success" showIcon message="没有发现阻塞" description="触发条件、LLM 路由、主动额度和记忆治理状态均未发现明确异常。" />
        : <Space orientation="vertical" size="small" style={{ width: "100%" }}>
          {data.blockers.map((item) => (
            <Alert key={item.code} type={item.severity} showIcon message={item.title} description={item.detail} />
          ))}
        </Space>}
      <div className="agent-cockpit-actions">
        <Button icon={<SettingOutlined />} onClick={() => onNavigate("config")}>调整运行配置</Button>
        <Popconfirm
          title="立即整理本群记忆？"
          description={`待整理 ${data.memory.pendingMessages} 条消息，含 LLM 摘要，后台运行数十秒。`}
          onConfirm={() => run("compact", `/agent/groups/${groupId}/memories/compact`, "已开始整理记忆")}
        >
          <Button icon={<ThunderboltOutlined />} loading={busy === "compact" || data.memory.inFlight} disabled={!data.effective.enabled}>
            立即整理记忆
          </Button>
        </Popconfirm>
        <Button
          icon={<ClearOutlined />}
          disabled={!conversation.active}
          loading={busy === "conversation"}
          onClick={() => void run("conversation", `/agent/groups/${groupId}/conversation/close`, "已结束当前短会话")}
        >
          结束短会话
        </Button>
        <Button icon={<BugOutlined />} onClick={() => onNavigate("debug")}>模拟一轮对话</Button>
        <Link to="/environment" className="agent-cockpit-link">检查 LLM Provider / 模型路由</Link>
      </div>
    </Card>
  );
}

function RhythmCard({ data }: { data: AgentDiagnostics }): React.JSX.Element {
  const effective = data.effective;
  const pipeline = data.pipeline;
  const stage = (title: string, enabled: boolean, lines: string[]) => (
    <div className={`agent-rhythm-stage${enabled ? "" : " is-off"}`}>
      <div className="agent-rhythm-title">
        <span>{title}</span>
        <Tag color={enabled ? "magenta" : "default"}>{enabled ? "启用" : "关闭"}</Tag>
      </div>
      {lines.map((line) => <div key={line} className="agent-rhythm-line">{line}</div>)}
    </div>
  );
  const triggers = [
    "@ 或回复 Bot 一定回应",
    effective.explicitWakeupEnabled ? "叫名字 / 唤醒词也会回应" : "叫名字不触发",
  ];
  return (
    <Card className="agent-cockpit-card" title="对话节奏" extra={<Text type="secondary">一条消息从进来到 Bot 开口的路径</Text>}>
      <div className="agent-rhythm">
        {stage("触发", effective.enabled, triggers)}
        {stage("合批", effective.enabled, pipeline
          ? [
            `只 @ 不说话时等 ${pipeline.callContinuationSeconds}s 补正文`,
            `同一人 ${pipeline.coalesceWindowSeconds}s 内连发合并，最多 ${pipeline.maxCoalescedMessages} 条`,
            "回复前有人插话会自动引用触发消息",
          ]
          : ["同一成员连发会合并成一个回合"])}
        {stage("续聊", effective.shortConversation.enabled, pipeline
          ? [
            `单人接话静默 ${pipeline.conversationDirectQuietSeconds}s、多人 ${pipeline.conversationQuietSeconds}s 后评估`,
            `每段最多 ${pipeline.conversationMaxBotTurns} 回合、${Math.round(pipeline.conversationMaxSeconds / 60)} 分钟`,
          ]
          : ["Bot 回复后在同一话题里继续接话"])}
        {stage("主动", effective.proactiveEnabled, [
          `冷场 ${effective.idleThresholdMinutes ?? "—"} 分钟后以 ${percent(effective.proactiveProbability ?? 0)} 概率暖场`,
          effective.proactiveActiveEnabled
            ? `热闹时 ${effective.proactiveActiveWindowMinutes ?? "—"} 分钟窗口内 ${percent(effective.proactiveActiveProbability ?? 0)} 概率插话`
            : "热闹插话关闭",
          `冷却 ${effective.cooldownMinutes} 分钟 · 每日 ${effective.dailyLimit} 次`,
        ])}
      </div>
      {effective.activeTopic && <Text type="secondary" className="agent-cockpit-footnote">当前话题：{effective.activeTopic}</Text>}
    </Card>
  );
}

function CapabilityCard({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [refreshing, setRefreshing] = useState(false);
  const query = useApiQuery({
    queryKey: ["agent-capabilities", groupId],
    fetcher: (signal) => api<AgentCapabilities>(`/agent/groups/${groupId}/capabilities`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const capabilities = query.data;
  const refresh = async (): Promise<void> => {
    setRefreshing(true);
    try {
      await api<AgentCapabilities>(`/agent/groups/${groupId}/capabilities/refresh`, { method: "POST" });
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <Card
      className="agent-cockpit-card"
      title="OneBot 协议能力"
      extra={<Button size="small" icon={<ApiOutlined />} onClick={() => void refresh()} loading={refreshing}>重新探测</Button>}
    >
      {query.error
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
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
            <Space wrap size={[4, 6]}>
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
  );
}

function LlmRouteCard({ data }: { data: AgentDiagnostics }): React.JSX.Element {
  return (
    <Card
      className="agent-cockpit-card"
      title="LLM 实际路由"
      extra={data.llm.unconfiguredProviders.length > 0
        ? <Tag color="red">Provider 未配置</Tag>
        : <Tag color="green">路由可用</Tag>}
    >
      <List
        size="small"
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
  );
}

// 驾驶舱：把“它在不在跑、跑得怎么样、为什么不说话、下一步做什么”放在同一屏。
export function AgentCockpit({
  groupId,
  groupLabel,
  diagnosticsQuery,
  autoRefresh,
  onAutoRefreshChange,
  onNavigate,
}: {
  groupId: string;
  groupLabel: string;
  diagnosticsQuery: ApiQuery<AgentDiagnostics>;
  autoRefresh: boolean;
  onAutoRefreshChange: (value: boolean) => void;
  onNavigate: Navigate;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const data = diagnosticsQuery.data;
  if (!data) {
    return diagnosticsQuery.error
      ? <QueryErrorAlert error={diagnosticsQuery.error} onRetry={diagnosticsQuery.reload} />
      : <Spin />;
  }
  const score = healthScore(data);
  const copyReport = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(diagnosticsReport(groupLabel, data));
      message.success("诊断报告已复制");
    } catch {
      message.error("浏览器不允许写入剪贴板");
    }
  };
  return (
    <div className="agent-cockpit">
      <div className="agent-cockpit-toolbar">
        <div className={`agent-health-score is-${healthTone(score)}`} aria-label={`健康分 ${score}`}>
          <strong>{score}</strong>
          <span>健康分</span>
        </div>
        <Text type="secondary" className="agent-cockpit-updated">
          诊断更新于 {diagnosticsQuery.updatedAt ? new Date(diagnosticsQuery.updatedAt).toLocaleTimeString("zh-CN") : "—"}
        </Text>
        <div className="agent-cockpit-toolbar-actions">
          <label className="agent-cockpit-auto">
            <Switch size="small" checked={autoRefresh} onChange={onAutoRefreshChange} aria-label="自动刷新" />
            <span>自动刷新</span>
          </label>
          <Button size="small" icon={<ReloadOutlined />} onClick={diagnosticsQuery.reload} loading={diagnosticsQuery.refreshing}>刷新诊断</Button>
          <Button size="small" icon={<CopyOutlined />} onClick={() => void copyReport()}>复制诊断报告</Button>
        </div>
      </div>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}><ActivityCard groupId={groupId} onNavigate={onNavigate} /></Col>
        <Col xs={24} xl={10}><PulseCard groupId={groupId} onNavigate={onNavigate} /></Col>
        <Col xs={24} xl={10}>
          <HealthCard groupId={groupId} data={data} onNavigate={onNavigate} onChanged={diagnosticsQuery.reload} />
        </Col>
        <Col xs={24} xl={14}><RhythmCard data={data} /></Col>
        <Col xs={24} lg={12} xl={8}><PersonaMoodCard groupId={groupId} onNavigate={onNavigate} /></Col>
        <Col xs={24} lg={12} xl={8}><MemoryHealthCard data={data} onNavigate={onNavigate} /></Col>
        <Col xs={24} xl={8}><ToolUsageCard groupId={groupId} onNavigate={onNavigate} /></Col>
        <Col xs={24} xl={12}><CapabilityCard groupId={groupId} /></Col>
        <Col xs={24} xl={12}><LlmRouteCard data={data} /></Col>
      </Row>
    </div>
  );
}
