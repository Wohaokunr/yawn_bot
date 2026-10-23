import { Button, Card, Empty, List, Segmented, Space, Spin, Tag, Typography } from "antd";
import { useState } from "react";
import { toolDisplayName } from "../agent-debug/debug-utils";
import { AUDIT_OUTCOME_META, MEMORY_TYPE_META, auditOutcome, auditOutcomeLabel, memoryTypeLabel } from "../agent-meta";
import { api } from "../api";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentAudit, AgentDiagnostics, AgentToolUsage, Persona } from "../types";
import { relativeTime } from "./polling";

const { Text } = Typography;

type Navigate = (tab: string, extra?: Record<string, string>) => void;

// 0–100 的健康分：错误阻塞扣得最重，记忆连续失败按次扣分，用于一眼判断“要不要管”。
export function healthScore(data: AgentDiagnostics): number {
  if (!data.effective.enabled) return 0;
  let score = 100;
  for (const blocker of data.blockers) {
    score -= blocker.severity === "error" ? 35 : blocker.severity === "warning" ? 12 : 3;
  }
  score -= Math.min(data.memory.consecutiveFailures * 5, 20);
  return Math.max(0, Math.min(100, score));
}

export function healthTone(score: number): "ok" | "warning" | "error" {
  if (score >= 80) return "ok";
  if (score >= 50) return "warning";
  return "error";
}

// 一段可直接贴到群里或 issue 的纯文本诊断摘要。
export function diagnosticsReport(groupLabel: string, data: AgentDiagnostics): string {
  const effective = data.effective;
  const lines = [
    `【YawnBot Agent 诊断】${groupLabel}`,
    `生成时间：${new Date(data.generatedAt).toLocaleString("zh-CN")}`,
    `健康分：${healthScore(data)} / 100`,
    `总开关：${effective.enabled ? "开启" : "关闭"} · 叫名唤醒：${effective.explicitWakeupEnabled ? "开" : "关"} · 主动参与：${effective.proactiveEnabled ? "开" : "关"} · 短会话：${effective.shortConversation.enabled ? "开" : "关"}`,
    `主动发言：${effective.proactiveToday}/${effective.dailyLimit}，冷却剩余 ${effective.cooldownRemainingMinutes} 分钟`,
  ];
  if (data.tools) {
    lines.push(`特权工具：${data.tools.adminToday}/${data.tools.adminLimit} · 高风险：${data.tools.criticalToday}/${data.tools.criticalLimit} · 已开放 ${data.tools.allowlist.length} 项`);
  }
  lines.push(`记忆：共 ${data.memory.total} 条，待整理 ${data.memory.pendingMessages} 条，连续失败 ${data.memory.consecutiveFailures} 次${data.memory.rebuildRequired ? "，需要重建" : ""}`);
  lines.push(`LLM 路由：${data.llm.routes.map((route) => `${route.task}=${route.configured ? route.model || "?" : "不可用"}`).join("，") || "—"}`);
  lines.push(data.blockers.length
    ? `阻塞：\n${data.blockers.map((item) => `- [${item.severity}] ${item.title}：${item.detail}`).join("\n")}`
    : "阻塞：无");
  return lines.join("\n");
}

export function ToolUsageCard({ groupId, onNavigate }: { groupId: string; onNavigate: Navigate }): React.JSX.Element {
  const [days, setDays] = useState(7);
  const usage = useApiQuery({
    queryKey: ["agent-tool-usage", groupId, days],
    fetcher: (signal) => api<AgentToolUsage>(`/agent/groups/${groupId}/tool-usage?days=${days}`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const recent = useApiQuery({
    queryKey: ["agent-recent-audits", groupId],
    fetcher: (signal) => api<AgentAudit[]>(`/agent/audits?groupId=${groupId}&page=1&pageSize=5&result=`, { signal }).then((r) => r.data),
  });
  const data = usage.data;
  const maxTotal = Math.max(1, ...(data?.tools ?? []).map((tool) => tool.total));
  const dayTotal = (day: AgentToolUsage["daily"][number]) => day.success + (day.skipped ?? 0) + day.failed + (day.unknown ?? 0);
  const maxDaily = Math.max(1, ...(data?.daily ?? []).map(dayTotal));
  return (
    <Card
      className="agent-cockpit-card"
      title="特权工具使用"
      extra={<Segmented size="small" value={days} onChange={(value) => setDays(Number(value))} options={[{ value: 7, label: "7 天" }, { value: 30, label: "30 天" }]} />}
    >
      {!data
        ? usage.error ? <QueryErrorAlert error={usage.error} onRetry={usage.reload} /> : <Spin />
        : <>
          <div className="agent-tool-daily" role="img" aria-label={`最近 ${data.days} 天共调用 ${data.total} 次，失败 ${data.failed} 次`}>
            {data.daily.map((day) => {
              const total = dayTotal(day);
              return (
                <div key={day.day} className="agent-tool-daily-col" title={`${day.day} · 成功 ${day.success} · 跳过 ${day.skipped ?? 0} · 失败 ${day.failed}${day.unknown ? ` · 待确认 ${day.unknown}` : ""}`}>
                  <div className="agent-tool-daily-bar" style={{ height: `${(total / maxDaily) * 100}%` }}>
                    {day.failed > 0 && <span className="is-failed" style={{ height: `${(day.failed / Math.max(total, 1)) * 100}%` }} />}
                  </div>
                </div>
              );
            })}
          </div>
          <Space size={12} className="agent-tool-summary">
            <Text>共 <strong>{data.total}</strong> 次</Text>
            {(data.skipped ?? 0) > 0 && <Text type="secondary">跳过 {data.skipped} 次</Text>}
            <Text type={data.failed ? "danger" : "secondary"}>失败 {data.failed} 次</Text>
            {(data.unknown ?? 0) > 0 && <Text type="warning">待确认 {data.unknown} 次</Text>}
          </Space>
          {data.tools.length === 0
            ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段时间没有调用特权工具" />
            : <div className="agent-tool-rank">
              {data.tools.slice(0, 6).map((tool) => (
                <div key={tool.toolName} className="agent-tool-rank-row">
                  <span className="agent-tool-rank-name">{toolDisplayName(tool.toolName)}</span>
                  <div className="agent-tool-rank-bar" aria-hidden>
                    {(["success", "skipped", "unknown", "failed"] as const).map((bucket) => (tool[bucket] ?? 0) > 0 && (
                      <span key={bucket} className={`is-${bucket}`} style={{ width: `${((tool[bucket] ?? 0) / maxTotal) * 100}%` }} />
                    ))}
                  </div>
                  <span className="agent-tool-rank-count">
                    {tool.total}
                    {tool.skipped ? <span className="agent-tool-rank-skipped"> · 跳过 {tool.skipped}</span> : null}
                    {tool.failed ? <em> · 失败 {tool.failed}</em> : null}
                  </span>
                </div>
              ))}
            </div>}
        </>}
      <div className="agent-cockpit-subtitle agent-tool-recent-title">
        最近特权操作
        <Button type="link" size="small" onClick={() => onNavigate("audit")}>全部审计</Button>
      </div>
      <List
        size="small"
        dataSource={recent.data ?? []}
        loading={!recent.data && !recent.error}
        locale={{ emptyText: "暂无记录" }}
        renderItem={(row) => (
          <List.Item
            className={row.traceId ? "agent-pulse-item" : undefined}
            onClick={row.traceId ? () => onNavigate("debug", { "debug.trace": row.traceId ?? "" }) : undefined}
          >
            <div className="agent-feed-row">
              <Tag color={AUDIT_OUTCOME_META[auditOutcome(row)].color}>{auditOutcomeLabel(row)}</Tag>
              <span className="agent-feed-main">{toolDisplayName(row.toolName)}</span>
              <span className="agent-feed-time">{relativeTime(row.createdAt)}</span>
            </div>
          </List.Item>
        )}
      />
    </Card>
  );
}

export function MemoryHealthCard({ data, onNavigate }: { data: AgentDiagnostics; onNavigate: Navigate }): React.JSX.Element {
  const memory = data.memory;
  const types = Object.entries(memory.countsByType).sort((a, b) => b[1] - a[1]);
  return (
    <Card
      className="agent-cockpit-card"
      title="记忆健康"
      extra={<Button size="small" onClick={() => onNavigate("memories")}>管理记忆</Button>}
    >
      <div className="agent-cockpit-kpis is-2col">
        <div><span>记忆总数</span><strong>{memory.total}</strong></div>
        <div><span>待整理消息</span><strong>{memory.pendingMessages}</strong></div>
        <div><span>连续失败</span><strong className={memory.consecutiveFailures ? "is-danger" : undefined}>{memory.consecutiveFailures}</strong></div>
        <div><span>最近成功</span><strong className="is-small">{relativeTime(memory.lastSuccessAt)}</strong></div>
      </div>
      <Space wrap size={[6, 6]}>
        {types.length === 0
          ? <Text type="secondary">还没有沉淀记忆</Text>
          : types.map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} · {count}</Tag>)}
        {memory.rebuildRequired && <Tag color="orange">需要重建</Tag>}
        {memory.inFlight && <Tag color="processing">整理中</Tag>}
      </Space>
      {memory.lastError && <Text type="danger" className="agent-cockpit-footnote">最近错误：{memory.lastError}</Text>}
    </Card>
  );
}

export function PersonaMoodCard({ groupId, onNavigate }: { groupId: string; onNavigate: Navigate }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["agent-persona-mood", groupId],
    fetcher: (signal) => api<Persona>(`/agent/groups/${groupId}/persona`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_config"], scope: { groupId } },
  });
  const persona = query.data;
  return (
    <Card
      className="agent-cockpit-card"
      title="人设与此刻情绪"
      extra={<Button size="small" onClick={() => onNavigate("persona")}>编辑人设</Button>}
    >
      {!persona
        ? query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />
        : <div className="agent-mood">
          <div className="agent-mood-face" aria-hidden="true">{MOOD_FACES[persona.emotion.label] ?? "=ω="}</div>
          <div className="agent-mood-body">
            <Space size={6} wrap>
              <Tag color="magenta">{persona.emotion.displayLabel}</Tag>
              <Tag>{persona.enabled ? "本群专属人设" : "跟随全局人设"}</Tag>
              <Text type="secondary">{persona.emotion.updatedAt ? `${relativeTime(persona.emotion.updatedAt)}更新` : "尚无情绪事件"}</Text>
            </Space>
            <Progress percent={Math.round(persona.emotion.expressionIntensity * 100)} size="small" strokeColor="#e4558a" format={(value) => `表达 ${value}%`} />
            <Text type="secondary" className="agent-mood-hint">{persona.emotion.expressionHint}</Text>
            {persona.summary && <Text className="agent-mood-summary">{persona.summary}</Text>}
          </div>
        </div>}
    </Card>
  );
}

const MOOD_FACES: Record<string, string> = {
  neutral: "=ω=",
  warm: "^ω^",
  amused: "≧ω≦",
  curious: "·ω·?",
  concerned: "´･ω･`",
  guarded: "-ω-",
  irritated: "｀ω´",
};
