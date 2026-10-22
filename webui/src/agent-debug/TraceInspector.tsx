import {
  DiffOutlined,
  DownloadOutlined,
  FileMarkdownOutlined,
  LinkOutlined,
  PlayCircleOutlined,
  StarFilled,
  StarOutlined,
} from "@ant-design/icons";
import { App as AntApp, Button, Skeleton, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentAudit, AgentExecutionTrace, AgentExecutionTraceStats } from "../types";
import {
  agentDebugModeLabel,
  copyText,
  DebugRawBlock,
  TRACE_STATUS_META,
  toolDisplayName,
  traceOutcomeLabel,
  triggerSourceLabel,
} from "./debug-utils";
import { diagnoseTrace, downloadJson, traceKpis, traceMarkdown } from "./trace-analysis";
import { TraceTimeline } from "./TraceTimeline";
import { PhaseBreakdownView, TraceDiagnosis, TraceKpiRow, TraceWaterfall } from "./TraceVisuals";
import type { ExecutionTracesState } from "./useExecutionTraces";

const { Text, Paragraph } = Typography;

export function TraceInspector({
  groupId,
  traces,
  stats,
  onReplay,
  onCompare,
}: {
  groupId: string;
  traces: ExecutionTracesState;
  stats: AgentExecutionTraceStats | null;
  onReplay: (trace: AgentExecutionTrace) => void;
  onCompare: (traceId: string) => void;
}): React.JSX.Element {
  const trace = traces.selectedTrace;
  if (traces.detailError) return <section className="dbg-panel dbg-inspector"><QueryErrorAlert error={traces.detailError} onRetry={traces.reloadSelected} /></section>;
  if (!trace) {
    return <section className="dbg-panel dbg-inspector">
      {traces.detailLoading || (traces.selectedTraceId && !traces.detailError)
        ? <Skeleton active paragraph={{ rows: 10 }} />
        : <AdminEmpty description="从左侧选择一条 Trace 查看完整事件" />}
    </section>;
  }
  return <TraceDetail
    key={trace.traceId}
    groupId={groupId}
    trace={trace}
    stats={stats}
    loading={traces.detailLoading}
    starred={traces.starred.includes(trace.traceId)}
    onToggleStar={() => traces.toggleStar(trace.traceId)}
    onReplay={() => onReplay(trace)}
    onCompare={() => onCompare(trace.traceId)}
  />;
}

function TraceDetail({
  groupId,
  trace,
  stats,
  loading,
  starred,
  onToggleStar,
  onReplay,
  onCompare,
}: {
  groupId: string;
  trace: AgentExecutionTrace;
  stats: AgentExecutionTraceStats | null;
  loading: boolean;
  starred: boolean;
  onToggleStar: () => void;
  onReplay: () => void;
  onCompare: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedEventId = searchParams.get("debug.event") ?? "";
  const [tab, setTab] = useState("timeline");
  const kpis = useMemo(() => traceKpis(trace), [trace]);
  const insights = useMemo(() => diagnoseTrace(trace), [trace]);
  const problems = insights.filter((item) => item.level === "error" || item.level === "warning").length;
  const status = TRACE_STATUS_META[trace.status] ?? { label: trace.status, color: "default" };
  const canReplay = Boolean(trace.messageId) || Boolean(trace.actorUserId);

  const selectEvent = (eventId: string): void => {
    const next = new URLSearchParams(searchParams);
    if (eventId) next.set("debug.event", eventId); else next.delete("debug.event");
    setSearchParams(next, { replace: true });
    if (eventId) setTab("timeline");
  };

  const copyLink = async (): Promise<void> => {
    const url = new URL(window.location.href);
    url.searchParams.set("tab", "debug");
    url.searchParams.set("debug.view", "trace");
    url.searchParams.set("debug.trace", trace.traceId);
    const ok = await copyText(url.toString());
    if (ok) message.success("已复制 Trace 链接"); else message.error("复制失败，请手动复制地址栏");
  };

  const copyMarkdown = async (): Promise<void> => {
    const ok = await copyText(traceMarkdown(trace, insights));
    if (ok) message.success("已复制 Markdown 诊断摘要"); else message.error("复制失败");
  };

  return <section className="dbg-panel dbg-inspector" aria-busy={loading}>
    <header className="dbg-detail-head">
      <div className="dbg-detail-title">
        <div className="dbg-detail-tags">
          <Tag color={trace.source === "runtime" ? "magenta" : "blue"}>{trace.source === "runtime" ? "真实执行" : "调试执行"}</Tag>
          <Tag className="dbg-tag-soft">{agentDebugModeLabel(trace.mode)}</Tag>
          {trace.triggerSource && <Tag className="dbg-tag-soft">{triggerSourceLabel(trace.triggerSource)}</Tag>}
          <Tag color={status.color}>{status.label}</Tag>
          {trace.outcome && <Text type="secondary">{traceOutcomeLabel(trace.outcome)}</Text>}
        </div>
        <div className="dbg-detail-meta">
          <Text type="secondary">{formatTime(trace.startedAt)}</Text>
          {trace.actorUserId && <Text type="secondary">发言人 <Text code copyable>{trace.actorUserId}</Text></Text>}
          {trace.messageId && <Text type="secondary">消息 <Text code copyable>{trace.messageId}</Text></Text>}
          <Text type="secondary">Trace <Text code copyable={{ text: trace.traceId, tooltips: ["复制完整 ID", "已复制"] }}>{trace.traceId.slice(0, 12)}</Text></Text>
        </div>
      </div>
      <div className="dbg-detail-actions">
        <Tooltip title={starred ? "取消收藏" : "收藏这条 Trace"}>
          <Button aria-label={starred ? "取消收藏" : "收藏"} icon={starred ? <StarFilled className="dbg-star-on" /> : <StarOutlined />} onClick={onToggleStar} />
        </Tooltip>
        <Tooltip title={canReplay ? "把这条 Trace 的触发消息带进模拟实验室" : "这条 Trace 没有可回放的消息"}>
          <Button icon={<PlayCircleOutlined />} disabled={!canReplay} onClick={onReplay}>回放</Button>
        </Tooltip>
        <Tooltip title="和另一条 Trace 并排对比">
          <Button icon={<DiffOutlined />} onClick={onCompare}>对比</Button>
        </Tooltip>
        <Tooltip title="复制深链"><Button aria-label="复制 Trace 链接" icon={<LinkOutlined />} onClick={copyLink} /></Tooltip>
        <Tooltip title="复制 Markdown 诊断摘要"><Button aria-label="复制 Markdown 摘要" icon={<FileMarkdownOutlined />} onClick={copyMarkdown} /></Tooltip>
        <Tooltip title="下载 Trace JSON"><Button aria-label="下载 Trace JSON" icon={<DownloadOutlined />} onClick={() => downloadJson(`trace-${groupId}-${trace.traceId.slice(0, 12)}.json`, trace)} /></Tooltip>
      </div>
    </header>

    <TraceKpiRow kpis={kpis} />
    <TraceDiagnosis insights={insights} onSelectEvent={selectEvent} compact />

    <Tabs
      className="dbg-tabs"
      activeKey={tab}
      onChange={setTab}
      items={[
        { key: "timeline", label: `时间线 ${trace.events.length}`, children: <TraceTimeline trace={trace} selectedEventId={selectedEventId} onSelectEvent={selectEvent} /> },
        { key: "waterfall", label: "瀑布图", children: <TraceWaterfall trace={trace} onSelectEvent={selectEvent} /> },
        { key: "phases", label: "阶段耗时", children: <PhaseBreakdownView trace={trace} stats={stats} /> },
        { key: "diagnosis", label: <span>诊断{problems > 0 && <span className="dbg-tab-badge">{problems}</span>}</span>, children: <TraceDiagnosis insights={insights} onSelectEvent={selectEvent} /> },
        { key: "audits", label: "关联审计", children: <TraceAudits groupId={groupId} traceId={trace.traceId} /> },
        { key: "raw", label: "原始 JSON", children: <DebugRawBlock value={trace} /> },
      ]}
    />
  </section>;
}

function TraceAudits({ groupId, traceId }: { groupId: string; traceId: string }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["agent-trace-audits", groupId, traceId],
    fetcher: (signal) => api<AgentAudit[]>(
      `/agent/groups/${groupId}/execution-traces/${encodeURIComponent(traceId)}/audits`,
      { signal },
    ).then((response) => response.data),
  });
  if (query.error) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (query.loading && !query.data) return <Skeleton active paragraph={{ rows: 3 }} />;
  const rows = query.data ?? [];
  if (rows.length === 0) return <AdminEmpty description="这条 Trace 没有产生特权工具审计。只读工具和普通发送不会写审计。" />;
  return <Table
    size="small"
    rowKey="id"
    pagination={false}
    dataSource={rows}
    scroll={{ x: 640 }}
    expandable={{ expandedRowRender: (row) => <DebugRawBlock value={row.arguments} /> }}
    columns={[
      { title: "时间", dataIndex: "createdAt", width: 170, render: formatTime },
      { title: "工具", dataIndex: "toolName", render: (value: string) => <><Text strong>{toolDisplayName(value)}</Text><br /><Text type="secondary" code>{value}</Text></> },
      { title: "结果", dataIndex: "result", width: 110, render: (value: string) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> },
      { title: "调用者", dataIndex: "actorUserId", width: 120, render: (value: string | null) => value ?? "—" },
      { title: "说明", dataIndex: "detail", render: (value: string | null) => value ? <Paragraph ellipsis={{ rows: 2, tooltip: value }} style={{ marginBottom: 0 }}>{value}</Paragraph> : "—" },
    ]}
  />;
}
