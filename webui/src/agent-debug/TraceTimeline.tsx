import { FireOutlined } from "@ant-design/icons";
import { Button, Input, Select, Space, Switch, Tag, Typography } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { AdminEmpty } from "../shared";
import type { AgentExecutionTrace, AgentExecutionTraceEvent } from "../types";
import { CopyButton, debugJson, DebugRawBlock, TRACE_STATUS_META } from "./debug-utils";
import { filterEvents, formatDuration, phaseLabel, slowestEvents } from "./trace-analysis";
import { TraceDiagnosticFields, TraceHumanSummary } from "./TraceEventInspector";

const { Text } = Typography;
const PAGE_SIZE = 40;

const STATUS_OPTIONS = ["success", "failed", "degraded", "unknown", "skipped", "planned"].map((value) => ({
  value,
  label: TRACE_STATUS_META[value]?.label ?? value,
}));

export function TraceTimeline({
  trace,
  selectedEventId = "",
  onSelectEvent,
}: {
  trace: AgentExecutionTrace;
  selectedEventId?: string;
  onSelectEvent?: (eventId: string) => void;
}): React.JSX.Element {
  const [phases, setPhases] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [expandAll, setExpandAll] = useState(false);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const listRef = useRef<HTMLOListElement>(null);

  const availablePhases = useMemo(() => [...new Set(trace.events.map((event) => event.phase))], [trace.events]);
  const slowest = useMemo(() => new Set(slowestEvents(trace, 1).map((event) => event.id)), [trace]);
  const filtered = useMemo(() => filterEvents(trace.events, { phases, statuses, query }), [phases, query, statuses, trace.events]);
  const selectedIndex = filtered.findIndex((event) => event.id === selectedEventId);
  const visible = filtered.slice(0, Math.max(limit, selectedIndex + 1));
  const filtering = phases.length > 0 || statuses.length > 0 || query.trim() !== "";

  useEffect(() => {
    setLimit(PAGE_SIZE);
  }, [trace.traceId]);

  useEffect(() => {
    if (!selectedEventId) return;
    const node = listRef.current?.querySelector<HTMLElement>(`[data-event-id="${selectedEventId.replace(/"/g, "")}"]`);
    node?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [selectedEventId, trace.traceId]);

  if (trace.events.length === 0) return <AdminEmpty description="这条 Trace 没有记录到事件" />;

  return <div className="dbg-timeline">
    <div className="dbg-toolbar">
      <Input
        allowClear
        size="small"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索事件名称、字段或错误信息"
        aria-label="搜索事件"
        className="dbg-toolbar-search"
      />
      <Select
        size="small"
        mode="multiple"
        allowClear
        maxTagCount="responsive"
        value={phases}
        onChange={setPhases}
        options={availablePhases.map((phase) => ({ value: phase, label: phaseLabel(phase) }))}
        placeholder="全部阶段"
        aria-label="按阶段过滤"
        className="dbg-toolbar-select"
      />
      <Select
        size="small"
        mode="multiple"
        allowClear
        maxTagCount="responsive"
        value={statuses}
        onChange={setStatuses}
        options={STATUS_OPTIONS}
        placeholder="全部状态"
        aria-label="按状态过滤"
        className="dbg-toolbar-select"
      />
      <label className="dbg-inline-switch">
        <Switch size="small" checked={expandAll} onChange={setExpandAll} />
        <span>展开原始字段</span>
      </label>
    </div>
    {filtering && <Text type="secondary" className="dbg-small">匹配 {filtered.length} / {trace.events.length} 个事件</Text>}
    {filtered.length === 0
      ? <AdminEmpty description="没有符合过滤条件的事件" />
      : <ol className="dbg-events" ref={listRef}>
        {visible.map((event) => <TimelineEvent
          key={event.id}
          event={event}
          index={trace.events.indexOf(event)}
          slowest={slowest.has(event.id)}
          selected={event.id === selectedEventId}
          expandAll={expandAll}
          onSelect={onSelectEvent ? () => onSelectEvent(event.id === selectedEventId ? "" : event.id) : undefined}
        />)}
      </ol>}
    {visible.length < filtered.length && <Button block onClick={() => setLimit((value) => value + PAGE_SIZE)}>
      再显示 {Math.min(PAGE_SIZE, filtered.length - visible.length)} 个事件（剩余 {filtered.length - visible.length}）
    </Button>}
  </div>;
}

function TimelineEvent({
  event,
  index,
  slowest,
  selected,
  expandAll,
  onSelect,
}: {
  event: AgentExecutionTraceEvent;
  index: number;
  slowest: boolean;
  selected: boolean;
  expandAll: boolean;
  onSelect?: () => void;
}): React.JSX.Element {
  const meta = TRACE_STATUS_META[event.status] ?? { label: event.status, color: "default" };
  const hasInput = Object.keys(event.input ?? {}).length > 0;
  const hasOutput = Object.keys(event.output ?? {}).length > 0;
  return <li
    data-event-id={event.id}
    className={`dbg-event status-${event.status}${selected ? " is-selected" : ""}`}
  >
    <span className="dbg-event-dot" aria-hidden="true" />
    <div className="dbg-event-body">
      <div className="dbg-event-head">
        <button type="button" className="dbg-event-index" onClick={onSelect} disabled={!onSelect} aria-label={`定位到事件 ${index + 1}`} aria-pressed={selected}>
          #{index + 1}
        </button>
        <Tag className={`dbg-phase-tag phase-${event.phase}`}>{phaseLabel(event.phase)}</Tag>
        <Text strong className="dbg-event-label">{event.label}</Text>
        <Tag color={meta.color}>{meta.label}</Tag>
        {event.round != null && <Tag>第 {event.round} 轮</Tag>}
        {slowest && <Tag color="magenta" icon={<FireOutlined />}>最慢</Tag>}
        <span className="dbg-event-timing">
          <Text type="secondary">+{formatDuration(event.offsetMs)}</Text>
          {event.durationMs != null && <Text type="secondary">耗时 {formatDuration(event.durationMs)}</Text>}
        </span>
        <CopyButton text={() => debugJson(event)} label="复制事件 JSON" />
      </div>
      <TraceHumanSummary event={event} />
      {event.detail && <Text type={event.status === "failed" ? "danger" : "secondary"}>{event.detail}</Text>}
      <TraceDiagnosticFields input={event.input ?? {}} output={event.output ?? {}} />
      {(hasInput || hasOutput) && <details className="agent-debug-details" open={expandAll || undefined}>
        <summary>原始诊断字段（JSON）</summary>
        <Space orientation="vertical" size={6} style={{ width: "100%" }}>
          {hasInput && <><Text type="secondary">输入</Text><DebugRawBlock value={event.input} /></>}
          {hasOutput && <><Text type="secondary">输出</Text><DebugRawBlock value={event.output} /></>}
        </Space>
      </details>}
    </div>
  </li>;
}
