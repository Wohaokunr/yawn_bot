import { SwapOutlined } from "@ant-design/icons";
import { Button, Select, Skeleton, Switch, Table, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentExecutionTrace, AgentExecutionTraceSummary } from "../types";
import { agentDebugModeLabel, TRACE_STATUS_META, triggerSourceLabel } from "./debug-utils";
import { alignTraces, type AlignedEventRow, formatDuration, percentChange, phaseLabel, traceKpis } from "./trace-analysis";

const { Text } = Typography;

export function parseCompareParam(value: string | null): [string, string] {
  const [left = "", right = ""] = (value ?? "").split(",");
  return [left, right];
}

function summaryLabel(trace: AgentExecutionTraceSummary): string {
  const preview = trace.preview ? ` · ${trace.preview.slice(0, 24)}` : "";
  return `${formatTime(trace.startedAt)} · ${agentDebugModeLabel(trace.mode)} · ${formatDuration(trace.durationMs)}${preview}`;
}

function useTraceDetail(groupId: string, traceId: string) {
  return useApiQuery({
    queryKey: ["agent-execution-trace-detail", groupId, traceId],
    fetcher: (signal) => traceId
      ? api<AgentExecutionTrace>(`/agent/groups/${groupId}/execution-traces/${encodeURIComponent(traceId)}`, { signal }).then((response) => response.data)
      : Promise.resolve(null),
  });
}

export function TraceCompare({
  groupId,
  summaries,
}: {
  groupId: string;
  summaries: AgentExecutionTraceSummary[];
}): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const [leftId, rightId] = parseCompareParam(searchParams.get("debug.compare"));
  const [diffOnly, setDiffOnly] = useState(false);
  const left = useTraceDetail(groupId, leftId);
  const right = useTraceDetail(groupId, rightId);

  const setPair = (nextLeft: string, nextRight: string): void => {
    const next = new URLSearchParams(searchParams);
    if (nextLeft || nextRight) next.set("debug.compare", `${nextLeft},${nextRight}`); else next.delete("debug.compare");
    setSearchParams(next, { replace: true });
  };

  const options = summaries.map((trace) => ({ value: trace.traceId, label: summaryLabel(trace) }));
  const ready = left.data && right.data;

  return <section className="dbg-panel dbg-compare" aria-label="Trace 对比">
    <header className="dbg-panel-head">
      <div className="dbg-panel-title"><span>Trace 对比</span></div>
      <Text type="secondary" className="dbg-small">按“阶段 + 事件名 + 第几次出现”对齐两条 Trace 的事件，只读取缓冲里的快照。</Text>
    </header>
    <div className="dbg-compare-pickers">
      <div className="dbg-compare-picker">
        <span className="dbg-compare-side is-a">A</span>
        <Select showSearch optionFilterProp="label" value={leftId || undefined} onChange={(value: string) => setPair(value, rightId)} options={options} placeholder="选择基准 Trace" aria-label="基准 Trace" />
      </div>
      <Button aria-label="交换 A 和 B" icon={<SwapOutlined />} onClick={() => setPair(rightId, leftId)} disabled={!leftId && !rightId} />
      <div className="dbg-compare-picker">
        <span className="dbg-compare-side is-b">B</span>
        <Select showSearch optionFilterProp="label" value={rightId || undefined} onChange={(value: string) => setPair(leftId, value)} options={options} placeholder="选择对比 Trace" aria-label="对比 Trace" />
      </div>
    </div>
    {summaries.length < 2 && !leftId && <AdminEmpty description="缓冲里至少要有两条 Trace 才能对比" />}
    {left.error && <QueryErrorAlert error={left.error} onRetry={left.reload} />}
    {right.error && <QueryErrorAlert error={right.error} onRetry={right.reload} />}
    {(left.loading || right.loading) && !ready && (leftId && rightId) && <Skeleton active paragraph={{ rows: 6 }} />}
    {(!leftId || !rightId) && summaries.length >= 2 && <AdminEmpty description="选好 A 和 B 两条 Trace 后开始对比" />}
    {ready && <CompareBody left={left.data as AgentExecutionTrace} right={right.data as AgentExecutionTrace} diffOnly={diffOnly} onDiffOnlyChange={setDiffOnly} />}
  </section>;
}

function CompareBody({
  left,
  right,
  diffOnly,
  onDiffOnlyChange,
}: {
  left: AgentExecutionTrace;
  right: AgentExecutionTrace;
  diffOnly: boolean;
  onDiffOnlyChange: (value: boolean) => void;
}): React.JSX.Element {
  const rows = useMemo(() => alignTraces(left, right), [left, right]);
  const a = traceKpis(left);
  const b = traceKpis(right);
  const changed = rows.filter((row) => !row.left || !row.right || row.statusChanged || (row.deltaMs != null && Math.abs(row.deltaMs) >= 50));
  const metrics: Array<{ key: string; label: string; a: number | null; b: number | null; format?: (value: number | null) => string; lowerIsBetter?: boolean }> = [
    { key: "duration", label: "总耗时", a: a.durationMs, b: b.durationMs, format: formatDuration, lowerIsBetter: true },
    { key: "events", label: "事件数", a: a.events, b: b.events },
    { key: "llm", label: "模型轮数", a: a.llmRounds, b: b.llmRounds, lowerIsBetter: true },
    { key: "tools", label: "工具调用", a: a.toolCalls, b: b.toolCalls },
    { key: "failed", label: "失败事件", a: a.failedEvents, b: b.failedEvents, lowerIsBetter: true },
    { key: "prompt", label: "输入 Token", a: a.promptTokens, b: b.promptTokens, lowerIsBetter: true },
    { key: "completion", label: "输出 Token", a: a.completionTokens, b: b.completionTokens },
  ];
  return <>
    <div className="dbg-compare-heads">
      {[left, right].map((trace, index) => {
        const status = TRACE_STATUS_META[trace.status] ?? { label: trace.status, color: "default" };
        return <div key={trace.traceId} className="dbg-compare-head">
          <span className={`dbg-compare-side ${index === 0 ? "is-a" : "is-b"}`}>{index === 0 ? "A" : "B"}</span>
          <Text strong>{formatTime(trace.startedAt)}</Text>
          <Tag className="dbg-tag-soft">{agentDebugModeLabel(trace.mode)}</Tag>
          {trace.triggerSource && <Tag className="dbg-tag-soft">{triggerSourceLabel(trace.triggerSource)}</Tag>}
          <Tag color={status.color}>{status.label}</Tag>
        </div>;
      })}
    </div>
    <div className="dbg-compare-metrics">
      {metrics.map((metric) => {
        const format = metric.format ?? ((value: number | null) => (value == null ? "—" : String(value)));
        const delta = metric.a != null && metric.b != null ? metric.b - metric.a : null;
        const better = delta != null && delta !== 0 && metric.lowerIsBetter !== undefined ? (delta < 0) === metric.lowerIsBetter : null;
        return <div key={metric.key} className="dbg-compare-metric">
          <span className="dbg-kpi-label">{metric.label}</span>
          <span>{format(metric.a)} → <strong>{format(metric.b)}</strong></span>
          {delta != null && delta !== 0 && <span className={better === null ? "dbg-delta" : better ? "dbg-delta is-better" : "dbg-delta is-worse"}>{percentChange(metric.a, metric.b)}</span>}
        </div>;
      })}
    </div>
    <div className="dbg-toolbar">
      <Text type="secondary">共 {rows.length} 行，{changed.length} 行有差异（新增、缺失、状态变化或耗时差 ≥ 50 ms）</Text>
      <label className="dbg-inline-switch"><Switch size="small" checked={diffOnly} onChange={onDiffOnlyChange} /><span>只看差异</span></label>
    </div>
    <Table<AlignedEventRow>
      size="small"
      rowKey="key"
      pagination={false}
      scroll={{ x: 720 }}
      dataSource={diffOnly ? changed : rows}
      rowClassName={(row) => !row.left ? "dbg-row-added" : !row.right ? "dbg-row-removed" : row.statusChanged ? "dbg-row-changed" : ""}
      columns={[
        { title: "阶段", width: 90, render: (_, row) => <Tag className={`dbg-phase-tag phase-${row.phase}`}>{phaseLabel(row.phase)}</Tag> },
        { title: "事件", render: (_, row) => <Text>{row.label}</Text> },
        { title: "A", width: 150, render: (_, row) => <EventCell event={row.left} /> },
        { title: "B", width: 150, render: (_, row) => <EventCell event={row.right} /> },
        {
          title: "耗时差",
          width: 110,
          render: (_, row) => row.deltaMs == null
            ? (!row.left ? <Tag color="green">仅 B</Tag> : !row.right ? <Tag color="red">仅 A</Tag> : "—")
            : Math.abs(row.deltaMs) < 1
              ? <span className="dbg-delta">±0</span>
              : <span className={row.deltaMs > 0 ? "dbg-delta is-worse" : "dbg-delta is-better"}>{row.deltaMs > 0 ? "+" : "-"}{formatDuration(Math.abs(row.deltaMs))}</span>,
        },
      ]}
    />
  </>;
}

function EventCell({ event }: { event: AgentExecutionTrace["events"][number] | null }): React.JSX.Element {
  if (!event) return <Text type="secondary">—</Text>;
  const meta = TRACE_STATUS_META[event.status] ?? { label: event.status, color: "default" };
  return <span className="dbg-event-cell"><Tag color={meta.color}>{meta.label}</Tag>{event.durationMs != null && <Text type="secondary">{formatDuration(event.durationMs)}</Text>}</span>;
}
