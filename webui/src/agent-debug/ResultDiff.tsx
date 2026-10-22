import { Alert, Card, List, Segmented, Space, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import { formatTime } from "../shared";
import type { AgentDebugResponse } from "../types";
import { debugRecord } from "./debug-utils";
import { toolNames } from "./ToolInspector";
import {
  collapseDiff,
  contentText,
  type DiffLine,
  diffStats,
  estimateTokens,
  lineDiff,
  percentChange,
  promptAsText,
} from "./trace-analysis";

const { Text } = Typography;

interface DiffRow {
  key: string;
  label: string;
  kind: "added" | "removed" | "changed";
  before?: string;
  after?: string;
  change?: string;
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
  const numeric = typeof before === "number" && typeof after === "number";
  return {
    key: label,
    label,
    kind: before == null ? "added" : after == null ? "removed" : "changed",
    before: compactValue(before),
    after: compactValue(after),
    change: numeric ? percentChange(before, after) : undefined,
  };
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
    const row = scalarDiff(`${name}权限`, oldPermissions.get(name), newPermissions.get(name));
    if (row) rows.push(row);
  }
  return rows;
}

function decisionField(result: AgentDebugResponse["result"], field: string): unknown {
  return result?.decision ? debugRecord(result.decision)[field] : null;
}

function speechDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  return [
    scalarDiff("最终文本", before.result?.text, after.result?.text),
    scalarDiff("话语动作", decisionField(before.result, "action"), decisionField(after.result, "action")),
    scalarDiff("目标成员", decisionField(before.result, "targetUserId"), decisionField(after.result, "targetUserId")),
    scalarDiff("话题", decisionField(before.result, "topic"), decisionField(after.result, "topic")),
    scalarDiff("决策原因", decisionField(before.result, "reason"), decisionField(after.result, "reason")),
    scalarDiff("消息段", decisionField(before.result, "segments"), decisionField(after.result, "segments")),
  ].filter((row): row is DiffRow => row !== null);
}

function tokenDiff(before: AgentDebugResponse, after: AgentDebugResponse): DiffRow[] {
  const oldUsage = before.result?.usage;
  const newUsage = after.result?.usage;
  const promptEstimate = (value: AgentDebugResponse): number => value.promptMessages.reduce((sum, item) => sum + estimateTokens(contentText(item.content)), 0);
  return [
    scalarDiff("Prompt 估算 Token", promptEstimate(before), promptEstimate(after)),
    scalarDiff("输入 Token", oldUsage?.promptTokens, newUsage?.promptTokens),
    scalarDiff("输出 Token", oldUsage?.completionTokens, newUsage?.completionTokens),
    scalarDiff("缓存命中 Token", oldUsage?.cachedTokens, newUsage?.cachedTokens),
    scalarDiff("缓存未命中 Token", oldUsage?.cacheMissTokens, newUsage?.cacheMissTokens),
    scalarDiff("模型耗时 (ms)", before.result?.durationMs, after.result?.durationMs),
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
  return <Card size="small" title={<Space>{title}{rows.length > 0 ? <Tag color="orange">{rows.length}</Tag> : <Tag>无变化</Tag>}</Space>}>
    {rows.length === 0 ? <Text type="secondary">没有变化</Text> : <List
      size="small"
      dataSource={rows}
      renderItem={(row) => {
        const meta = DIFF_KIND_META[row.kind];
        return <List.Item key={row.key}>
          <div className="agent-debug-list-item">
            <Space wrap><Tag color={meta.color}>{meta.label}</Tag><Text strong>{row.label}</Text>{row.change && <span className="dbg-delta">{row.change}</span>}</Space>
            {row.before !== undefined && <Text type="secondary">基准：{row.before}</Text>}
            {row.after !== undefined && <Text type={row.kind === "removed" ? "secondary" : undefined}>当前：{row.after}</Text>}
          </div>
        </List.Item>;
      }}
    />}
  </Card>;
}

export function PromptLineDiff({ before, after }: { before: string; after: string }): React.JSX.Element {
  const [mode, setMode] = useState<"changes" | "full">("changes");
  const lines = useMemo(() => lineDiff(before, after), [after, before]);
  const stats = diffStats(lines);
  const shown = mode === "changes" ? collapseDiff(lines) : lines;
  return <div className="dbg-linediff">
    <div className="dbg-toolbar">
      <Space size={6}>
        <Tag color="green">+{stats.added}</Tag>
        <Tag color="red">-{stats.removed}</Tag>
        <Text type="secondary">{stats.same} 行未变</Text>
      </Space>
      <Segmented size="small" value={mode} onChange={(value) => setMode(value as "changes" | "full")} options={[{ value: "changes", label: "只看变化" }, { value: "full", label: "完整" }]} />
    </div>
    {stats.added + stats.removed === 0
      ? <Text type="secondary">两次 Prompt 完全一致</Text>
      : <pre className="dbg-linediff-body">
        {shown.map((line, index) => line.type === "skip"
          ? <div key={`skip-${index}`} className="dbg-ld-skip">… 省略 {line.count} 行未变化 …</div>
          : <div key={index} className={`dbg-ld-${(line as DiffLine).type}`}><span className="dbg-ld-sign">{line.type === "add" ? "+" : line.type === "del" ? "-" : " "}</span>{(line as DiffLine).text || " "}</div>)}
      </pre>}
  </div>;
}

export function TraceCompareView({
  baseline,
  current,
}: {
  baseline: AgentDebugResponse;
  current: AgentDebugResponse;
}): React.JSX.Element {
  const sections = useMemo(() => [
    { key: "context", title: "Context Diff", rows: contextDiff(baseline, current) },
    { key: "tools", title: "Tools Diff", rows: toolDiff(baseline, current) },
    { key: "speech", title: "Speech Diff", rows: speechDiff(baseline, current) },
    { key: "token", title: "Token Diff", rows: tokenDiff(baseline, current) },
    { key: "model", title: "Model Diff", rows: modelDiff(baseline, current) },
  ], [baseline, current]);
  const before = useMemo(() => promptAsText(baseline.promptMessages), [baseline.promptMessages]);
  const after = useMemo(() => promptAsText(current.promptMessages), [current.promptMessages]);
  const changedSections = sections.filter((section) => section.rows.length > 0).length + (before === after ? 0 : 1);
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Alert
      type={changedSections > 0 ? "info" : "success"}
      showIcon
      message={`基准：${formatTime(baseline.executionTrace.startedAt)} · 当前：${formatTime(current.executionTrace.startedAt)}`}
      description={changedSections > 0 ? `${changedSections} / 6 个维度有变化。对比只读取两次调试快照，不会重新执行模型或任何工具。` : "两次结果在六个维度上完全一致。"}
    />
    <DiffSection title={sections[0].title} rows={sections[0].rows} />
    <Card size="small" title={<Space>Prompt Diff{before === after ? <Tag>无变化</Tag> : <Tag color="orange">逐行</Tag>}</Space>}>
      <PromptLineDiff before={before} after={after} />
    </Card>
    {sections.slice(1).map((section) => <DiffSection key={section.key} title={section.title} rows={section.rows} />)}
  </Space>;
}
