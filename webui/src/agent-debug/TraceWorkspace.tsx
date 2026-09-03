import { Alert, Button, Card, Descriptions, List, Space, Tabs, Tag, Typography } from "antd";
import { formatTime, QueryErrorAlert } from "../shared";
import type { AgentDebugResponse, AgentExecutionTrace } from "../types";
import { ContextBudgetInspector, ContextInspector, CurrentTurnInspector } from "./ContextInspector";
import {
  AGENT_DEBUG_MODES,
  debugDisplay,
  debugRecord,
  DebugRawBlock,
  traceOutcomeLabel,
  toolDisplayName,
} from "./debug-utils";
import { PromptInspector } from "./PromptInspector";
import { ToolInspector, toolNames } from "./ToolInspector";
import { TracePipeline } from "./TracePipeline";

const { Paragraph, Text } = Typography;

interface DiffRow {
  key: string;
  label: string;
  kind: "added" | "removed" | "changed";
  before?: string;
  after?: string;
}

function serialize(value: unknown): string {
  return JSON.stringify(value) ?? String(value);
}

function compactValue(value: unknown, limit = 320): string {
  if (value == null || value === "") return "—";
  const text = typeof value === "string" ? value : serialize(value);
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function scalarDiff(label: string, before: unknown, after: unknown): DiffRow | null {
  if (serialize(before) === serialize(after)) return null;
  return {
    key: label,
    label,
    kind: before == null ? "added" : after == null ? "removed" : "changed",
    before: compactValue(before),
    after: compactValue(after),
  };
}

function promptDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const rows: DiffRow[] = [];
  const previous = before.promptMessages;
  const current = after.promptMessages;
  const count = Math.max(previous.length, current.length);
  for (let index = 0; index < count; index += 1) {
    const oldItem = previous[index];
    const newItem = current[index];
    const label = `第 ${index + 1} 条 ${newItem?.role ?? oldItem?.role ?? "消息"}`;
    if (!oldItem && newItem) {
      rows.push({ key: `added-${index}`, label, kind: "added", after: compactValue(newItem.content, 1200) });
    } else if (oldItem && !newItem) {
      rows.push({ key: `removed-${index}`, label, kind: "removed", before: compactValue(oldItem.content, 1200) });
    } else if (oldItem && newItem && serialize(oldItem) !== serialize(newItem)) {
      rows.push({ key: `changed-${index}`, label, kind: "changed", before: compactValue(oldItem.content, 1200), after: compactValue(newItem.content, 1200) });
    }
  }
  return rows;
}

function contextDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const rows: DiffRow[] = [];
  const labels: Array<[keyof AgentDebugResponse["context"], string]> = [
    ["messages", "历史消息"],
    ["members", "成员"],
    ["memories", "记忆"],
    ["relations", "关系"],
  ];
  for (const [key, label] of labels) {
    const oldItems = before.context[key] ?? [];
    const newItems = after.context[key] ?? [];
    const row = scalarDiff(`${label}数量`, Array.isArray(oldItems) ? oldItems.length : 0, Array.isArray(newItems) ? newItems.length : 0);
    if (row) rows.push(row);
    const oldSet = new Set((Array.isArray(oldItems) ? oldItems : []).map(serialize));
    const newSet = new Set((Array.isArray(newItems) ? newItems : []).map(serialize));
    [...newSet].filter((item) => !oldSet.has(item)).slice(0, 6).forEach((item, index) => rows.push({ key: `${key}-added-${index}`, label: `${label}新增`, kind: "added", after: compactValue(item) }));
    [...oldSet].filter((item) => !newSet.has(item)).slice(0, 6).forEach((item, index) => rows.push({ key: `${key}-removed-${index}`, label: `${label}移除`, kind: "removed", before: compactValue(item) }));
  }
  return rows;
}

function toolDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const rows: DiffRow[] = [];
  const oldNames = new Set(toolNames(before));
  const newNames = new Set(toolNames(after));
  [...newNames].filter((name) => !oldNames.has(name)).forEach((name) => rows.push({ key: `added-${name}`, label: name, kind: "added", after: "开放" }));
  [...oldNames].filter((name) => !newNames.has(name)).forEach((name) => rows.push({ key: `removed-${name}`, label: name, kind: "removed", before: "开放" }));
  const oldPermissions = new Map(before.toolPermissions.map((item) => [item.name, item]));
  const newPermissions = new Map(after.toolPermissions.map((item) => [item.name, item]));
  for (const name of [...oldNames].filter((item) => newNames.has(item))) {
    const oldPermission = oldPermissions.get(name);
    const newPermission = newPermissions.get(name);
    const row = scalarDiff(`${name}权限`, oldPermission, newPermission);
    if (row) rows.push(row);
  }
  return rows;
}

function speechDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const oldResult = before.result;
  const newResult = after.result;
  return [
    scalarDiff("最终文本", oldResult?.text, newResult?.text),
    scalarDiff("话语动作", oldResult?.decision ? debugRecord(oldResult.decision).action : null, newResult?.decision ? debugRecord(newResult.decision).action : null),
    scalarDiff("目标成员", oldResult?.decision ? debugRecord(oldResult.decision).targetUserId : null, newResult?.decision ? debugRecord(newResult.decision).targetUserId : null),
    scalarDiff("话题", oldResult?.decision ? debugRecord(oldResult.decision).topic : null, newResult?.decision ? debugRecord(newResult.decision).topic : null),
    scalarDiff("决策原因", oldResult?.decision ? debugRecord(oldResult.decision).reason : null, newResult?.decision ? debugRecord(newResult.decision).reason : null),
    scalarDiff("消息段", oldResult?.decision ? debugRecord(oldResult.decision).segments : null, newResult?.decision ? debugRecord(newResult.decision).segments : null),
  ].filter((row): row is DiffRow => row !== null);
}

function tokenDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const oldUsage = before.result?.usage;
  const newUsage = after.result?.usage;
  return [
    scalarDiff("输入 Token", oldUsage?.promptTokens, newUsage?.promptTokens),
    scalarDiff("输出 Token", oldUsage?.completionTokens, newUsage?.completionTokens),
    scalarDiff("缓存命中 Token", oldUsage?.cachedTokens, newUsage?.cachedTokens),
    scalarDiff("缓存未命中 Token", oldUsage?.cacheMissTokens, newUsage?.cacheMissTokens),
    scalarDiff("模型耗时", before.result?.durationMs, after.result?.durationMs),
  ].filter((row): row is DiffRow => row !== null);
}

function modelDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  return [
    scalarDiff("Provider", before.route.provider, after.route.provider),
    scalarDiff("模型", before.route.model, after.route.model),
    scalarDiff("Thinking", before.route.thinking, after.route.thinking),
    scalarDiff("多模态", before.route.multimodal, after.route.multimodal),
    scalarDiff("模型结果", before.result?.outcome, after.result?.outcome),
    scalarDiff("结束原因", before.result?.finishReason, after.result?.finishReason),
  ].filter((row): row is DiffRow => row !== null);
}

const DIFF_KIND_META: Record<DiffRow["kind"], { label: string; color: string }> = {
  added: { label: "新增", color: "green" },
  removed: { label: "移除", color: "red" },
  changed: { label: "变化", color: "orange" },
};

function DiffSection({ title, rows }: { title: string; rows: DiffRow[] }): React.JSX.Element {
  return <Card size="small" title={title}>
    {rows.length === 0 ? <Text type="secondary">没有变化</Text> : <List
      size="small"
      dataSource={rows}
      renderItem={(row) => {
        const meta = DIFF_KIND_META[row.kind];
        return <List.Item key={row.key}>
          <div className="agent-debug-list-item">
            <Space wrap><Tag color={meta.color}>{meta.label}</Tag><Text strong>{row.label}</Text></Space>
            {row.before !== undefined && <Text type="secondary">基准：{row.before}</Text>}
            {row.after !== undefined && <Text type={row.kind === "removed" ? "secondary" : undefined}>当前：{row.after}</Text>}
          </div>
        </List.Item>;
      }}
    />}
  </Card>;
}

export function TraceCompareView({
  baseline,
  current,
}: {
  baseline: AgentDebugResponse;
  current: AgentDebugResponse;
}): React.JSX.Element {
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Alert type="info" showIcon message={`基准：${formatTime(baseline.executionTrace.startedAt)} · 当前：${formatTime(current.executionTrace.startedAt)}`} description="对比只读取两次调试快照，不会重新执行模型或任何工具。" />
    <DiffSection title="Context Diff" rows={contextDiff(baseline, current)} />
    <DiffSection title="Prompt Diff" rows={promptDiff(baseline, current)} />
    <DiffSection title="Tools Diff" rows={toolDiff(baseline, current)} />
    <DiffSection title="Speech Diff" rows={speechDiff(baseline, current)} />
    <DiffSection title="Token Diff" rows={tokenDiff(baseline, current)} />
    <DiffSection title="Model Diff" rows={modelDiff(baseline, current)} />
  </Space>;
}

function DebugModelInspector({ result }: { result: AgentDebugResponse["result"] }): React.JSX.Element {
  if (!result) return <Text type="secondary">本次只生成提示词，没有调用模型</Text>;
  const decision = result.decision ? debugRecord(result.decision) : null;
  const cacheHitRate = result.usage.promptTokens && result.usage.cachedTokens != null
    ? `${Math.round((result.usage.cachedTokens / result.usage.promptTokens) * 100)}%`
    : "—";
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "outcome", label: "结果", children: <Tag color={result.outcome === "success" ? "green" : result.outcome === "timeout" ? "red" : "orange"}>{traceOutcomeLabel(result.outcome)}</Tag> },
      { key: "duration", label: "耗时", children: `${result.durationMs} ms` },
      { key: "finish", label: "结束原因", children: result.finishReason || "—" },
      { key: "tokens", label: "Token（输入 / 输出）", children: `${result.usage.promptTokens ?? "—"} / ${result.usage.completionTokens ?? "—"}` },
      { key: "cacheHit", label: "缓存命中 Token", children: result.usage.cachedTokens ?? "—" },
      { key: "cacheMiss", label: "缓存未命中 Token", children: result.usage.cacheMissTokens ?? "—" },
      { key: "cacheRate", label: "Prompt 缓存命中率", children: cacheHitRate },
    ]} />
    {decision && <Card size="small" title="主动发言决策"><Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      { key: "action", label: "动作", children: debugDisplay(decision.action) },
      { key: "target", label: "目标成员", children: debugDisplay(decision.targetUserId) },
      { key: "topic", label: "话题", children: debugDisplay(decision.topic) },
      { key: "reason", label: "原因", children: debugDisplay(decision.reason) },
      { key: "confidence", label: "决策置信度", children: debugDisplay(decision.confidence) },
      { key: "segments", label: "消息段", children: debugDisplay(decision.segments) },
    ]} /></Card>}
    <Card size="small" title="模型文本"><Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>{result.text || "（无文本输出）"}</Paragraph></Card>
    {result.toolCalls.length > 0 && <Card size="small" title={`工具意图（${result.toolCalls.length}）`}><List
      size="small"
      dataSource={result.toolCalls}
      renderItem={(call) => <List.Item><div className="agent-debug-list-item">
        <Space wrap>
          <Text strong>{toolDisplayName(call.name)}</Text>
          <Text type="secondary" code>{call.name}</Text>
          <Tag color="blue">模型计划</Tag>
        </Space>
        <details className="agent-debug-details"><summary>查看工具参数（JSON）</summary><DebugRawBlock value={call.arguments} /></details>
      </div></List.Item>}
    /></Card>}
  </Space>;
}

export interface TraceWorkspaceProps {
  runtimeTrace: AgentExecutionTrace | null;
  runtimeLoading: boolean;
  runtimeError: string;
  onReloadRuntime: () => void;
  result: AgentDebugResponse | null;
  baseline: AgentDebugResponse | null;
  onPinBaseline: () => void;
  onClearBaseline: () => void;
}

export function TraceWorkspace({
  runtimeTrace,
  runtimeLoading,
  runtimeError,
  onReloadRuntime,
  result,
  baseline,
  onPinBaseline,
  onClearBaseline,
}: TraceWorkspaceProps): React.JSX.Element {
  return <Space orientation="vertical" size="large" style={{ width: "100%" }}>
    <Card title="真实 Trace 详情">
      {runtimeError ? <QueryErrorAlert error={runtimeError} onRetry={onReloadRuntime} /> : runtimeLoading ? <Text type="secondary">正在加载 Trace 详情…</Text> : runtimeTrace ? <TracePipeline trace={runtimeTrace} /> : <Text type="secondary">从左侧选择一条 Trace 查看完整事件。</Text>}
    </Card>
    {result && <>
      <Card title="本次调试摘要" extra={<Space wrap>
        {baseline ? <Tag color="purple">已固定基准</Tag> : null}
        <Button onClick={onPinBaseline} disabled={Boolean(baseline)}>固定当前为基准</Button>
        {baseline && <Button onClick={onClearBaseline}>清除基准</Button>}
      </Space>}>
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
          { key: "mode", label: "调试场景", children: AGENT_DEBUG_MODES.find((item) => item.value === result.mode)?.label ?? result.mode },
          { key: "prompt", label: "Prompt 版本", children: result.promptVersion },
          { key: "provider", label: "Provider", children: result.route.provider || "—" },
          { key: "model", label: "模型", children: result.route.model || "—" },
          { key: "profile", label: "路由配置", children: result.route.profile || "—" },
          { key: "thinking", label: "Thinking", children: result.route.thinking || "—" },
          { key: "multimodal", label: "多模态", children: result.route.multimodal || "—" },
          { key: "result", label: "模型结果", children: result.result ? <Tag color={result.result.outcome === "success" ? "green" : "orange"}>{result.result.outcome}</Tag> : <Tag>未调用</Tag> },
        ]} />
        {baseline && <Text type="secondary">当前结果会与固定基准按 Context、Prompt、Tools、Speech、Token、Model 六个维度比较。</Text>}
      </Card>
      <Card title="调试详情" className="agent-debug-detail-card">
        <Tabs items={[
          { key: "trace", label: `执行轨迹 ${result.executionTrace.events.length}`, children: <TracePipeline trace={result.executionTrace} /> },
          { key: "overview", label: "概览", children: <div className="agent-debug-inspector-grid"><CurrentTurnInspector value={result.currentTurn} /><ContextBudgetInspector stats={result.stats} /></div> },
          { key: "context", label: "上下文", children: <ContextInspector context={result.context} selection={result.contextSelection} /> },
          { key: "prompt", label: `Prompt ${result.promptMessages.length}`, children: <PromptInspector messages={result.promptMessages} /> },
          { key: "tools", label: `工具 ${result.tools.length}`, children: <ToolInspector tools={result.tools} permissions={result.toolPermissions} /> },
          { key: "model", label: "模型结果", children: <DebugModelInspector result={result.result} /> },
          ...(baseline ? [{ key: "compare", label: "Diff", children: <TraceCompareView baseline={baseline} current={result} /> }] : []),
          { key: "raw", label: "原始数据", children: <DebugRawBlock value={result} /> },
        ]} />
      </Card>
    </>}
  </Space>;
}
