import { Alert, Descriptions, Space, Tag, Timeline, Typography } from "antd";
import type { AgentExecutionTrace } from "../types";
import { DebugRawBlock, TRACE_PHASE_LABELS, TRACE_STATUS_META, traceOutcomeLabel, triggerSourceLabel } from "./debug-utils";
import { TraceDiagnosticFields, TraceHumanSummary } from "./TraceEventInspector";

const { Text } = Typography;

export function TracePipeline({
  trace,
  compact = false,
}: {
  trace: AgentExecutionTrace;
  compact?: boolean;
}): React.JSX.Element {
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
