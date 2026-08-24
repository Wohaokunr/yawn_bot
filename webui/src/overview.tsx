import {
  ApiOutlined,
  CrownOutlined,
  MessageOutlined,
  RobotOutlined,
  TeamOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Flex,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
} from "antd";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AgentAuditTable } from "./agent-audit-table";
import { api } from "./api";
import { AdminEmpty, formatTime, PageHeader } from "./shared";
import type { Overview } from "./types";

const { Text } = Typography;

type StatTone = "sakura" | "mint" | "sky" | "lavender" | "tomato";

function StatCard({
  icon,
  tone,
  title,
  value,
  suffix,
}: {
  icon: React.ReactNode;
  tone: StatTone;
  title: string;
  value: number | string;
  suffix?: string;
}): React.JSX.Element {
  return (
    <Card className="stat-card">
      <Flex align="center" gap={16}>
        <span className={`stat-badge tone-${tone}`}>{icon}</span>
        <Statistic title={title} value={value} suffix={suffix} />
      </Flex>
    </Card>
  );
}

export function formatUptime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${secs} 秒`;
  return `${secs} 秒`;
}

export function formatRate(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

export function formatLatency(ms: number | null): string {
  if (ms === null) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export const AI_OUTCOME_META: Record<string, { label: string; color: string }> = {
  success: { label: "成功", color: "green" },
  error: { label: "请求错误", color: "red" },
  timeout: { label: "超时", color: "red" },
  empty: { label: "空回复", color: "orange" },
  not_configured: { label: "未配置密钥", color: "default" },
  cancelled: { label: "已取消", color: "default" },
  unsupported_multimodal: { label: "不支持的多模态", color: "orange" },
};

export function aiOutcomeMeta(outcome: string): { label: string; color: string } {
  return AI_OUTCOME_META[outcome] ?? { label: outcome, color: "default" };
}

const FANQIE_STATUS_LABELS: Record<string, string> = {
  queued: "排队",
  running: "进行",
  failed: "失败",
  completed: "完成",
  cancelled: "已取消",
};

export function fanqieSummary(byStatus: Record<string, number>): string {
  const parts = Object.entries(FANQIE_STATUS_LABELS)
    .filter(([status]) => byStatus[status] !== undefined)
    .map(([status, label]) => `${label} ${byStatus[status]}`);
  return parts.length > 0 ? parts.join(" · ") : "暂无任务";
}

export interface OverviewIssue {
  key: string;
  severity: "error" | "warning" | "info";
  title: string;
  description: string;
  to: string;
}

export function buildOverviewIssues(data: Overview): OverviewIssue[] {
  const issues: OverviewIssue[] = [];
  const { stats } = data;
  if (data.bots.length === 0) {
    issues.push({
      key: "bot-offline",
      severity: "error",
      title: "没有在线 Bot",
      description: "当前没有连接的 Bot 账号，消息、主动发言和群内任务都会不可用。",
      to: "#plugin-status",
    });
  }
  const brokenPlugins = data.plugins.filter((plugin) => plugin.state !== "loaded");
  if (brokenPlugins.length > 0) {
    issues.push({
      key: "plugins",
      severity: brokenPlugins.some((plugin) => plugin.state === "failed") ? "error" : "warning",
      title: `${brokenPlugins.length} 个插件未正常加载`,
      description: brokenPlugins.map((plugin) => `${plugin.name}: ${plugin.state}`).join("；"),
      to: "#plugin-status",
    });
  }
  const aiFailures = stats.ai.byOutcome
    .filter((item) => ["error", "timeout", "empty", "unsupported_multimodal"].includes(item.outcome))
    .reduce((sum, item) => sum + item.count, 0);
  if (aiFailures > 0) {
    issues.push({
      key: "ai-failures",
      severity: "warning",
      title: `AI 请求累计异常 ${aiFailures} 次`,
      description: "包含请求错误、超时、空回复或多模态不兼容；建议先检查 Provider 与模型路由。",
      to: "/environment",
    });
  }
  if (stats.llm.unconfiguredProviders.length > 0) {
    issues.push({
      key: "llm-provider",
      severity: "error",
      title: "LLM Provider 未完整配置",
      description: `缺少可用密钥或模型：${stats.llm.unconfiguredProviders.join("、")}`,
      to: "/environment",
    });
  }
  if (stats.memory.failingGroups > 0 || stats.memory.rebuildRequired > 0) {
    const recent = stats.memory.recentError;
    issues.push({
      key: "memory",
      severity: stats.memory.failingGroups > 0 ? "error" : "warning",
      title: `记忆治理异常 ${stats.memory.failingGroups + stats.memory.rebuildRequired} 群`,
      description: recent?.error ?? "存在连续整理失败或待重建的群记忆。",
      to: recent ? `/agent/${recent.groupId}?tab=memories` : "/agent",
    });
  }
  if (stats.jobs.reminderErrors > 0) {
    issues.push({
      key: "reminders",
      severity: "warning",
      title: `提醒任务异常 ${stats.jobs.reminderErrors} 个`,
      description: "存在提醒任务执行错误，建议核对运行配置和相关任务状态。",
      to: "#jobs-status",
    });
  }
  return issues;
}

function liveGameLabel(live: { available: boolean; count: number }): string {
  return live.available ? `${live.count} 局` : "未加载";
}

function endedTodayLabel(ended: number | null): string {
  return ended === null ? "未加载" : `${ended} 局`;
}

export function OverviewPage(): React.JSX.Element {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const result = await api<Overview>("/overview");
      setData(result.data);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const update = (event: Event) => setData((event as CustomEvent<Overview>).detail);
    window.addEventListener("yawnbot-overview", update);
    return () => window.removeEventListener("yawnbot-overview", update);
  }, []);

  if (!data) return error ? <Alert type="error" message={error} /> : <Spin />;

  const { stats } = data;
  const liveAvailable = stats.games.live.rpg.available || stats.games.live.werewolf.available;
  const liveGames = liveAvailable
    ? stats.games.live.rpg.count + stats.games.live.werewolf.count
    : "—";
  const failures = stats.ai.byOutcome.filter((item) => item.outcome !== "success");
  const issues = buildOverviewIssues(data);

  return <>
    <PageHeader
      title="运行概览"
      subtitle={`快照更新于 ${formatTime(data.generatedAt)} · 已运行 ${formatUptime(stats.uptime.uptimeSeconds)}`}
      onRefresh={() => void load()}
      refreshing={refreshing}
      status={issues.length > 0 ? <Tag color="red">{issues.length} 项需处理</Tag> : <Tag color="green">运行正常</Tag>}
    />
    <Card title="需要处理的问题" className="ops-issues-card">
      {issues.length === 0
        ? <AdminEmpty description="当前没有需要处理的问题" />
        : <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
          {issues.map((issue) => (
            <Alert
              key={issue.key}
              type={issue.severity}
              showIcon
              message={issue.title}
              description={issue.description}
              action={issue.to.startsWith("#") ? <a href={issue.to}>查看</a> : <Link to={issue.to}>去处理</Link>}
            />
          ))}
        </Space>}
    </Card>
    <Row gutter={[16, 16]} className="section-row">
      <Col xs={24} sm={12} lg={8} xl={4}><StatCard tone="sakura" icon={<RobotOutlined />} title="在线 Bot" value={data.bots.length} suffix="个" /></Col>
      <Col xs={24} sm={12} lg={8} xl={4}><StatCard tone="mint" icon={<MessageOutlined />} title="24 小时消息量" value={stats.activity.messages24h} suffix="条" /></Col>
      <Col xs={24} sm={12} lg={8} xl={4}><StatCard tone="sky" icon={<TeamOutlined />} title="24 小时活跃群" value={stats.activity.activeGroups24h} suffix="个" /></Col>
      <Col xs={24} sm={12} xl={4}><StatCard tone="lavender" icon={<ApiOutlined />} title="AI 成功率" value={formatRate(stats.ai.successRate)} suffix={stats.ai.requestsTotal > 0 ? `均 ${formatLatency(stats.ai.avgDurationMs)}` : undefined} /></Col>
      <Col xs={24} sm={12} xl={4}><StatCard tone="sky" icon={<CrownOutlined />} title="进行中对局" value={liveGames} suffix={liveAvailable ? "局" : undefined} /></Col>
      <Col xs={24} sm={12} xl={4}><StatCard tone={stats.memory.failingGroups > 0 || stats.memory.rebuildRequired > 0 ? "tomato" : "mint"} icon={<WarningOutlined />} title="记忆异常群" value={stats.memory.failingGroups + stats.memory.rebuildRequired} suffix="个" /></Col>
    </Row>
    <Row gutter={[16, 16]} className="section-row">
      <Col xs={24} lg={12}>
        <Card title="AI 服务健康">
          <Descriptions column={2} size="small" items={[
            { key: "total", label: "总调用", children: stats.ai.requestsTotal },
            { key: "ok", label: "成功", children: stats.ai.success },
            { key: "fail", label: "失败", children: stats.ai.failed },
            { key: "rate", label: "成功率", children: formatRate(stats.ai.successRate) },
            { key: "avg", label: "平均延迟", children: formatLatency(stats.ai.avgDurationMs) },
            { key: "p95", label: "P95 延迟", children: formatLatency(stats.ai.p95DurationMs) },
            { key: "deg", label: "降级 / 兜底", children: stats.ai.degradations },
          ]} />
          {failures.length > 0 && (
            <Flex wrap="wrap" gap={8} className="status-line" justify="flex-start">
              <Text type="secondary">失败分布：</Text>
              {failures.map((item) => (
                <Tag key={item.outcome} color={aiOutcomeMeta(item.outcome).color}>
                  {aiOutcomeMeta(item.outcome).label} {item.count}
                </Tag>
              ))}
            </Flex>
          )}
          <Text type="secondary">指标为当前进程累计值，重启后清零。</Text>
        </Card>
      </Col>
      <Col xs={24} lg={12}>
        <Card title="记忆治理" extra={<Link to="/agent">Agent 管理</Link>}>
          <Descriptions column={3} size="small" items={[
            { key: "compacting", label: "整理中", children: `${stats.memory.compactingGroups} 群` },
            { key: "rebuild", label: "待重建", children: `${stats.memory.rebuildRequired} 群` },
            { key: "failing", label: "连续失败", children: stats.memory.failingGroups > 0 ? <Tag color="red">{stats.memory.failingGroups} 群</Tag> : `${stats.memory.failingGroups} 群` },
          ]} />
          {stats.memory.recentError ? (
            <Alert
              type="error"
              showIcon
              message={`群 ${stats.memory.recentError.groupId}：${stats.memory.recentError.error}`}
              description={`最近整理失败于 ${stats.memory.recentError.at ? formatTime(stats.memory.recentError.at) : "未知时间"}`}
            />
          ) : (
            <Text type="secondary">最近一次记忆整理没有记录到错误。</Text>
          )}
        </Card>
      </Col>
    </Row>
    <Row gutter={[16, 16]} className="section-row">
      <Col xs={24} lg={12}>
        <Card title="活动与 Agent">
          <Descriptions column={2} size="small" items={[
            { key: "proactive", label: "今日主动消息", children: `${stats.activity.proactiveToday} 条` },
            { key: "admintool", label: "今日管理工具", children: `${stats.activity.adminToolToday} 次` },
            { key: "responded", label: "24h 有回复的群", children: `${stats.activity.agentResponseGroups24h} 个` },
            { key: "enabled", label: "启用 Agent", children: `${data.counts.enabledAgents} 个` },
            { key: "groups", label: "已知群组", children: `${data.counts.groups} 个` },
            { key: "users", label: "已知用户", children: `${data.counts.users} 人` },
          ]} />
        </Card>
      </Col>
      <Col xs={24} lg={12}>
        <Card id="jobs-status" title="游戏与任务" extra={<Space><Link to="/games">对局中心</Link><Link to="/fanqie">番茄任务</Link></Space>}>
          <Descriptions column={2} size="small" items={[
            { key: "liveRpg", label: "进行中跑团", children: liveGameLabel(stats.games.live.rpg) },
            { key: "liveWw", label: "进行中狼人杀", children: liveGameLabel(stats.games.live.werewolf) },
            { key: "endedRpg", label: "今日完局跑团", children: endedTodayLabel(stats.games.endedToday.rpg) },
            { key: "endedWw", label: "今日完局狼人杀", children: endedTodayLabel(stats.games.endedToday.werewolf) },
            { key: "fanqie", label: "番茄任务", children: stats.jobs.fanqie.available ? fanqieSummary(stats.jobs.fanqie.byStatus) : "未加载" },
            { key: "reminders", label: "提醒异常", children: stats.jobs.reminderErrors > 0 ? <Tag color="red">{stats.jobs.reminderErrors} 个</Tag> : `${stats.jobs.reminderErrors} 个` },
          ]} />
        </Card>
      </Col>
    </Row>
    <Row gutter={[16, 16]} className="section-row">
      <Col xs={24} lg={10}>
        <Card id="plugin-status" title="插件状态">
          {data.plugins.map((plugin) => (
            <Flex key={plugin.name} justify="space-between" className="status-line">
              <span>{plugin.name}</span>
              <Tag color={plugin.state === "loaded" ? "green" : plugin.state === "failed" ? "red" : "default"}>{plugin.state}</Tag>
            </Flex>
          ))}
          <Flex justify="space-between" className="status-line">
            <span>Bot 账号</span>
            <Text type="secondary">{data.bots.join(", ") || "未连接"}</Text>
          </Flex>
        </Card>
      </Col>
      <Col xs={24} lg={14}>
        <Card title="近期 Agent 操作"><AgentAuditTable data={data.recentAgentActions} /></Card>
      </Col>
    </Row>
  </>;
}
