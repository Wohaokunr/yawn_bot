import { Alert, Skeleton } from "antd";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { fetchTracePrompts } from "./prompt-api";
import { PromptSnapshotView } from "./PromptSnapshotView";

/** Trace 详情里的「完整提示词」：这条执行里每一次真实发给模型的请求。 */
export function TracePromptsPanel({ groupId, traceId }: { groupId: string; traceId: string }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["agent-trace-prompts", groupId, traceId],
    fetcher: (signal) => fetchTracePrompts(groupId, traceId, signal),
  });
  if (query.error) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (query.loading && !query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const data = query.data;
  if (!data) return <Skeleton active paragraph={{ rows: 6 }} />;
  return <div className="ps-trace">
    {!data.captureEnabled && <Alert type="info" showIcon message="完整提示词抓取已关闭" description="环境变量 AGENT_TRACE_PROMPT_CAPTURE=off。改回 full 并重启后，新的 Trace 会带上每次请求的完整 messages、tools 与回复。" />}
    {data.evicted > 0 && <Alert type="warning" showIcon message={`这条 Trace 的 ${data.evicted} 次请求已被清出内存`} description={`为控制内存，全进程只保留最近 ${data.limits.tracesWithPrompts} 条 Trace 的完整提示词，更早的只剩事件摘要。`} />}
    {data.dropped > 0 && <Alert type="info" showIcon message={`还有 ${data.dropped} 次请求没有记录`} description={`单条 Trace 最多记录 ${data.limits.perTrace} 次模型请求。`} />}
    <PromptSnapshotView
      snapshots={data.prompts}
      filenamePrefix={`trace-${traceId.slice(0, 12)}`}
      emptyHint={data.captureEnabled ? "这条执行没有调用模型（例如被策略拦下、命中确定性回复），或发生在升级之前" : "抓取已关闭"}
    />
  </div>;
}
