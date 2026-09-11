import { Alert, Button, Card, List, Select, Space, Switch, Tag, Typography } from "antd";
import { AdminEmpty, formatTime, QueryErrorAlert } from "../shared";
import type { AgentExecutionTraceSummary } from "../types";
import { agentDebugModeLabel, TRACE_STATUS_META, traceOutcomeLabel, triggerSourceLabel } from "./debug-utils";
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
    title="最近真实执行"
    extra={<Button onClick={traces.reloadSelected} loading={traces.listRefreshing || traces.detailLoading}>刷新</Button>}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <Space wrap>
        <Button onClick={traces.selectLatest}>查看最新</Button>
        <Select aria-label="筛选执行状态" value={traces.status} onChange={traces.setStatus} options={STATUS_OPTIONS} style={{ width: 128 }} />
        <Space size={6}><Switch size="small" checked={traces.autoRefresh} onChange={traces.setAutoRefresh} /><Text type="secondary">自动刷新（3 秒）</Text></Space>
      </Space>
      <details className="agent-debug-details"><summary>Trace 保留与隐私说明</summary>仅保存在当前进程，重启清空；仅按需加载详情，不保留完整 URL、本机路径与原始 OneBot payload。</details>
      {traces.selectedTraceUnavailable && <Alert
        type="info"
        showIcon
        message="当前选中的 Trace 已离开当前缓冲 / 不在当前筛选结果"
        description="仍保留已加载的详情，不会自动跳回最新 Trace。你可以调整筛选条件，或手动选择列表中的另一条 Trace。"
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
    </Space>
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
  return <List.Item className={selected ? "agent-trace-list-item is-selected" : "agent-trace-list-item"} role="button" tabIndex={0} aria-pressed={selected} onClick={onSelect} onKeyDown={(event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); }
  }}>
    <div className="agent-debug-list-item">
      <Space wrap size={6}>
        <Text strong>{formatTime(trace.startedAt)}</Text>
        <Tag>{agentDebugModeLabel(trace.mode)}</Tag>
        <Tag color={status.color}>{status.label}</Tag>
      </Space>
      <Space wrap size={6}>
        <Text type="secondary">{traceOutcomeLabel(trace.outcome ?? trace.status)}</Text>
        <Text type="secondary">{trace.eventCount} 个事件</Text>
        <Text type="secondary">{trace.durationMs == null ? "耗时 —" : `${trace.durationMs.toFixed(1)} ms`}</Text>
        <Text type="secondary">{trace.triggerSource ? triggerSourceLabel(trace.triggerSource) : "触发原因 —"}</Text>
        <Text type="secondary">发言人 {trace.actorUserId || "—"}</Text>
      </Space>
    </div>
  </List.Item>;
}
