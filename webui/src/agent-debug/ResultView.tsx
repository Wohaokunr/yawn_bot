import { DownloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Descriptions, List, Progress, Segmented, Space, Tabs, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import type { AgentDebugResponse, AgentDeliveryPreview } from "../types";
import { ContextBudgetInspector, ContextInspector, CurrentTurnInspector } from "./ContextInspector";
import {
  AGENT_DEBUG_MODES,
  debugDisplay,
  debugRecord,
  DebugRawBlock,
  toolDisplayName,
  traceOutcomeLabel,
} from "./debug-utils";
import { PromptInspector } from "./PromptInspector";
import { TraceCompareView } from "./ResultDiff";
import { ToolInspector } from "./ToolInspector";
import { downloadJson, formatDuration, promptStats } from "./trace-analysis";
import { TraceTimeline } from "./TraceTimeline";
import { TraceWaterfall } from "./TraceVisuals";
import type { DebugRun } from "./useDebugRuns";

const { Paragraph, Text } = Typography;

function DebugModelInspector({ result }: { result: AgentDebugResponse["result"] }): React.JSX.Element {
  if (!result) return <Alert type="info" showIcon message="本次只生成提示词快照，没有调用模型" description="打开“调用真实模型”后重新运行即可看到模型输出；依旧不会执行任何工具或发送。" />;
  const decision = result.decision ? debugRecord(result.decision) : null;
  const cacheRate = result.usage.promptTokens && result.usage.cachedTokens != null
    ? Math.round((result.usage.cachedTokens / result.usage.promptTokens) * 100)
    : null;
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
      { key: "outcome", label: "结果", children: <Tag color={result.outcome === "success" ? "green" : result.outcome === "timeout" ? "red" : "orange"}>{traceOutcomeLabel(result.outcome)}</Tag> },
      { key: "duration", label: "耗时", children: formatDuration(result.durationMs) },
      { key: "finish", label: "结束原因", children: result.finishReason === "length" ? <Text type="warning">length（输出被截断）</Text> : result.finishReason || "—" },
      { key: "tokens", label: "Token（输入 / 输出）", children: `${result.usage.promptTokens ?? "—"} / ${result.usage.completionTokens ?? "—"}` },
      { key: "cacheHit", label: "缓存命中 / 未命中", children: `${result.usage.cachedTokens ?? "—"} / ${result.usage.cacheMissTokens ?? "—"}` },
      { key: "cacheRate", label: "Prompt 缓存命中率", children: cacheRate == null ? "—" : <div className="dbg-mini-progress"><Progress percent={cacheRate} size="small" showInfo={false} /><span>{cacheRate}%</span></div> },
    ]} />
    {decision && <Card size="small" title="主动发言决策"><Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      { key: "action", label: "动作", children: debugDisplay(decision.action) },
      { key: "target", label: "目标成员", children: debugDisplay(decision.targetUserId) },
      { key: "topic", label: "话题", children: debugDisplay(decision.topic) },
      { key: "reason", label: "原因", children: debugDisplay(decision.reason) },
      { key: "confidence", label: "决策置信度", children: typeof decision.confidence === "number" ? `${Math.round(decision.confidence * 100)}%` : debugDisplay(decision.confidence) },
      { key: "segments", label: "消息段", children: debugDisplay(decision.segments) },
    ]} /></Card>}
    <Card size="small" title="模型文本"><Paragraph copyable={Boolean(result.text)} style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>{result.text || "（无文本输出）"}</Paragraph></Card>
    {result.delivery && <DeliveryPreview delivery={result.delivery} />}
    {result.toolCalls.length > 0 && <Card size="small" title={`工具意图（${result.toolCalls.length}）`}><List
      size="small"
      dataSource={result.toolCalls}
      renderItem={(call) => <List.Item><div className="agent-debug-list-item">
        <Space wrap>
          <Text strong>{toolDisplayName(call.name)}</Text>
          <Text type="secondary" code>{call.name}</Text>
          <Tag color="blue">模型计划 · 未执行</Tag>
        </Space>
        <details className="agent-debug-details"><summary>查看工具参数（JSON）</summary><DebugRawBlock value={call.arguments} /></details>
      </div></List.Item>}
    /></Card>}
  </Space>;
}

const RHYTHM_SOURCE_LABELS: Record<string, string> = {
  model: "模型用 ||| 标的",
  model_clauses: "模型要连发，按短句拆",
  model_sentences: "模型要分条，按句子拆",
  pop_downgraded: "太长不适合蹦字，改成连发",
  auto_paragraph: "按段落兜底拆分",
  auto_line: "按换行兜底拆分",
  auto_sentence: "按句子兜底拆分",
  structured: "含代码/列表，整条发",
  single: "一条发完",
  disabled: "分条已关闭",
  segments: "按消息段分组",
};

function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)}s`;
}

/** 模拟 QQ 里一条条冒出来的样子：每条前面标出停顿。 */
export function DeliveryPreview({ delivery }: { delivery: AgentDeliveryPreview }): React.JSX.Element {
  const multi = delivery.parts.length > 1;
  return <Card size="small" title={<Space wrap size={6}>
    <span>发送节奏预览</span>
    <Tag color={multi ? "magenta" : "default"}>{delivery.rhythmLabel}</Tag>
    <Text type="secondary">{RHYTHM_SOURCE_LABELS[delivery.source] ?? delivery.source}</Text>
  </Space>} extra={multi ? <Text type="secondary">共 {delivery.parts.length} 条 · 历时约 {formatSeconds(delivery.spreadMs)}</Text> : null}>
    <ol className="dbg-delivery" aria-label="分条发送预览">
      {delivery.parts.map((part, index) => <li key={`${index}-${part.text}`} className="dbg-delivery-item">
        {index > 0 && <span className="dbg-delivery-gap">停 {formatSeconds(part.delayMs)}</span>}
        <span className="dbg-delivery-bubble">{part.text}</span>
      </li>)}
    </ol>
    {delivery.settings.mode === "off" && <Alert type="info" showIcon message="AGENT_REPLY_SPLIT=off：所有回复都一条发完" />}
  </Card>;
}

// 与 PersonaBehavior.as_dict 的字段对齐；source 在标题里单独展示。
const BEHAVIOR_LABELS: Record<string, string> = {
  sociability: "活跃度",
  followupTendency: "续聊倾向",
  reactionTendency: "接梗",
  warmupProbabilityScale: "暖场系数",
  activeProbabilityScale: "主动候选系数",
  maxFollowupBotTurns: "最多续聊轮数",
  allowSpontaneousReaction: "主动 reaction",
  reactionMode: "reaction 档位",
};

function PersonaInspector({ persona }: { persona: AgentDebugResponse["persona"] }): React.JSX.Element {
  const persisted = debugRecord(persona.persistedBehavior);
  const applied = debugRecord(persona.appliedBehavior);
  const keys = [...new Set([...Object.keys(persisted), ...Object.keys(applied)])].filter((key) => key !== "source");
  const changed = keys.filter((key) => JSON.stringify(persisted[key]) !== JSON.stringify(applied[key]));
  const persistedEmotion = debugRecord(persona.persistedEmotion);
  const appliedEmotion = debugRecord(persona.appliedEmotion);
  return <Card size="small" title={<Space>人设<Tag color={persona.source === "draft" ? "orange" : "default"}>{persona.source === "draft" ? "使用草稿" : "使用已保存人设"}</Tag></Space>}>
    <Space orientation="vertical" size="small" style={{ width: "100%" }}>
      {persona.persistedSummary && <Paragraph type="secondary" ellipsis={{ rows: 3, expandable: true, symbol: "展开" }} style={{ marginBottom: 0 }}>{persona.persistedSource ? `${persona.persistedSource} · ` : ""}{persona.persistedSummary}</Paragraph>}
      {persona.source === "draft" && persona.appliedCard && <Text type="secondary">草稿：{persona.appliedCard.name}{persona.appliedCard.selfName ? `（自称${persona.appliedCard.selfName}）` : ""}</Text>}
      {keys.length > 0 && <div className="dbg-behavior-grid">
        {keys.map((key) => <div key={key} className={`dbg-behavior${changed.includes(key) ? " is-changed" : ""}`}>
          <span className="dbg-kpi-label">{BEHAVIOR_LABELS[key] ?? key}</span>
          <span>{changed.includes(key) ? <>{debugDisplay(persisted[key])} → <strong>{debugDisplay(applied[key])}</strong></> : debugDisplay(applied[key])}</span>
        </div>)}
      </div>}
      {Object.keys(appliedEmotion).length > 0 && <Space wrap size={[6, 6]}>
        <Text type="secondary">情绪：</Text>
        {Object.entries(appliedEmotion).slice(0, 8).map(([key, value]) => {
          const before = persistedEmotion[key];
          const diff = JSON.stringify(before) !== JSON.stringify(value);
          return <Tag key={key} color={diff ? "orange" : undefined}>{key} {typeof value === "object" ? "…" : String(value)}</Tag>;
        })}
      </Space>}
    </Space>
  </Card>;
}

export function ResultView({
  run,
  baseline,
  onPickBaseline,
}: {
  run: DebugRun;
  baseline: DebugRun | null;
  onPickBaseline: () => void;
}): React.JSX.Element {
  const result = run.result;
  const [traceMode, setTraceMode] = useState<"timeline" | "waterfall">("timeline");
  const stats = useMemo(() => promptStats(result.promptMessages), [result.promptMessages]);
  const compareAvailable = baseline && baseline.id !== run.id;
  const modeLabel = AGENT_DEBUG_MODES.find((item) => item.value === result.mode)?.label ?? result.mode;
  const exposedTools = result.toolPermissions.filter((item) => item.exposed).length;

  return <div className="dbg-result">
    {!result.route.configured && <Alert type="warning" showIcon message="这个场景的模型路由还没有配置完整" description="缺少 Provider 地址、API Key 或模型名。快照仍可生成，但“调用真实模型”会失败，请先到“环境配置”补齐。" />}
    {result.warnings.length > 0 && <Alert type="info" showIcon message="调试提示" description={<ul className="dbg-plain-list">{result.warnings.map((item) => <li key={item}>{item}</li>)}</ul>} />}

    <div className="dbg-result-summary">
      <div className="dbg-kpis">
        <div className="dbg-kpi"><span className="dbg-kpi-label">场景</span><strong className="dbg-kpi-value">{modeLabel}</strong><span className="dbg-kpi-hint">Prompt {result.promptVersion}</span></div>
        <div className="dbg-kpi"><span className="dbg-kpi-label">模型</span><strong className="dbg-kpi-value dbg-ellipsis" title={result.route.model}>{result.route.model || "—"}</strong><span className="dbg-kpi-hint">{result.route.provider || "未知 Provider"}{result.route.profile ? ` · ${result.route.profile}` : ""}</span></div>
        <div className="dbg-kpi"><span className="dbg-kpi-label">Prompt</span><strong className="dbg-kpi-value">{result.promptMessages.length} 条</strong><span className="dbg-kpi-hint">约 {stats.tokens.toLocaleString()} Token</span></div>
        <div className="dbg-kpi"><span className="dbg-kpi-label">工具</span><strong className="dbg-kpi-value">{exposedTools}</strong><span className="dbg-kpi-hint">共 {result.toolPermissions.length} 个候选</span></div>
        <div className="dbg-kpi"><span className="dbg-kpi-label">模型结果</span><strong className="dbg-kpi-value">{result.result ? traceOutcomeLabel(result.result.outcome) : "未调用"}</strong><span className="dbg-kpi-hint">{result.result ? formatDuration(result.result.durationMs) : "仅快照"}</span></div>
        <div className="dbg-kpi"><span className="dbg-kpi-label">请求耗时</span><strong className="dbg-kpi-value">{formatDuration(run.elapsedMs)}</strong><span className="dbg-kpi-hint">Thinking {result.route.thinking || "—"} · 多模态 {result.route.multimodal || "—"}</span></div>
      </div>
      <div className="dbg-result-actions">
        <Tooltip title="下载完整调试结果 JSON">
          <Button icon={<DownloadOutlined />} onClick={() => downloadJson(`agent-debug-${result.mode}-${new Date(run.createdAt).toISOString().replace(/[:.]/g, "-")}.json`, result)}>下载结果</Button>
        </Tooltip>
      </div>
    </div>

    <Tabs
      className="dbg-tabs"
      defaultActiveKey="overview"
      items={[
        {
          key: "overview",
          label: "概览",
          // 当前消息的 Descriptions 按视口断点分列，放进窄栏会被挤成竖排，所以单独占满一行。
          children: <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
            <CurrentTurnInspector value={result.currentTurn} />
            <div className="agent-debug-inspector-grid">
              <PersonaInspector persona={result.persona} />
              <ContextBudgetInspector stats={result.stats} />
            </div>
          </Space>,
        },
        {
          key: "trace",
          label: `执行轨迹 ${result.executionTrace.events.length}`,
          children: <Space orientation="vertical" size="small" style={{ width: "100%" }}>
            <Segmented size="small" value={traceMode} onChange={(value) => setTraceMode(value as typeof traceMode)} options={[{ value: "timeline", label: "时间线" }, { value: "waterfall", label: "瀑布图" }]} />
            {traceMode === "timeline" ? <TraceTimeline trace={result.executionTrace} /> : <TraceWaterfall trace={result.executionTrace} />}
          </Space>,
        },
        { key: "context", label: "上下文", children: <ContextInspector context={result.context} selection={result.contextSelection} budget={result.contextBudget} /> },
        { key: "prompt", label: `Prompt ${result.promptMessages.length}`, children: <PromptInspector messages={result.promptMessages} /> },
        { key: "tools", label: `工具 ${exposedTools}/${result.toolPermissions.length}`, children: <ToolInspector tools={result.tools} permissions={result.toolPermissions} /> },
        { key: "model", label: "模型结果", children: <DebugModelInspector result={result.result} /> },
        {
          key: "compare",
          label: "Diff",
          children: compareAvailable
            ? <TraceCompareView baseline={baseline.result} current={result} />
            : <Alert type="info" showIcon message="还没有选对比基准" description="在运行历史里把另一次结果设为基准（A），这里就会按 Context、Prompt、Tools、Speech、Token、Model 六个维度对比。" action={<Button size="small" onClick={onPickBaseline}>把这次设为基准</Button>} />,
        },
        { key: "raw", label: "原始数据", children: <DebugRawBlock value={result} /> },
      ]}
    />
  </div>;
}
