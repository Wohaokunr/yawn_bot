import { Alert, Button, Card, List, Select, Space, Switch, Tag, Typography } from "antd";
import { AdminEmpty, formatTime, QueryErrorAlert } from "../shared";
import type { AgentExecutionTraceSummary } from "../types";
import { agentDebugModeLabel, TRACE_STATUS_META, traceOutcomeLabel } from "./debug-utils";
import type { ExecutionTracesState } from "./useExecutionTraces";

const { Text } = Typography;

const STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  { value: "completed", label: "完成" },
  { value: "failed", label: "失败" },
  { value: "running", label: "执行中" },
];

export function TraceSidebar({ traces }: { traces: ExecutionTracesState }): React.JSX.Element {
  return <Card
    className="agent-trace-sidebar"
    title="Trace Navigator"
    extra={<Button onClick={traces.reloadSelected} loading={traces.listRefreshing || traces.detailLoading}>刷新</Button>}
  >
    <div className="agent-trace-sidebar-layout">
      <div className="agent-trace-sidebar-controls">
        <Space wrap>
          <Select value={traces.status} onChange={traces.setStatus} options={STATUS_OPTIONS} style={{ width: 128 }} />
          <Space size={6}>
            <Switch size="small" checked={traces.autoRefresh} onChange={traces.setAutoRefresh} />
            <Text type="secondary">自动刷新（3 秒）</Text>
          </Space>
        </Space>
        <Text type="secondary" className="agent-trace-buffer-note">
          Trace 仅保存在当前 Bot 进程；列表加载摘要，选中后才请求完整事件。
        </Text>
      </div>

      <div className="agent-trace-list-scroll">
        {traces.selectedTraceUnavailable && <Alert
          type="info"
          showIcon
          className="section-alert"
          message="当前 Trace 已离开缓冲 / 当前筛选"
          description="已加载详情会继续保留，不会自动跳回最新 Trace。"
        />}
        {traces.listError && traces.summaries.length === 0
          ? <QueryErrorAlert error={traces.listError} onRetry={traces.reload} />
          : traces.summaries.length === 0
            ? <AdminEmpty description="暂无真实执行 Trace；让 Agent 实际处理一条触发消息后刷新这里" />
            : <List
              className="agent-debug-list"
              loading={traces.listLoading}
              dataSource={traces.summaries}
              renderItem={(trace) => <TraceListItem
                key={trace.traceId}
                trace={trace}
                selected={trace.traceId === traces.selectedTraceId}
                onSelect={() => traces.setSelectedTraceId(trace.traceId)}
              />}
            />}
        {traces.listError && traces.summaries.length > 0 && <Text type="danger">刷新列表失败：{traces.listError}</Text>}
      </div>
    </div>
  </Card>;
}

function TraceListItem({
  trace,
  selected,
  onSelect,
}: {
  trace: AgentExecutionTraceSummary;
  selected: boolean;
  onSelect: () => void;
}): React.JSX.Element {
  const status = TRACE_STATUS_META[trace.status] ?? { label: trace.status, color: "default" };
  return <List.Item className={selected ? "agent-trace-list-item is-selected" : "agent-trace-list-item"} onClick={onSelect}>
    <div className="agent-debug-list-item">
      <Space wrap size={6}>
        <Text strong>{formatTime(trace.startedAt)}</Text>
        <Tag>{agentDebugModeLabel(trace.mode)}</Tag>
        <Tag color={status.color}>{status.label}</Tag>
      </Space>
      <Space wrap size={6}>
        <Text type="secondary">{traceOutcomeLabel(trace.outcome ?? trace.status)}</Text>
        <Text type="secondary">{trace.eventCount} 个事件</Text>
      </Space>
    </div>
  </List.Item>;
}
