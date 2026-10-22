// 调试台的纯函数分析层：耗时分解、瀑布图布局、自动诊断、Trace 对齐、行级 Diff 与 Token 估算。
// 全部无副作用，便于单测；组件只负责渲染这些结果。
import type { AgentDebugResponse, AgentExecutionTrace, AgentExecutionTraceEvent, AgentExecutionTraceSummary } from "../types";
import { debugRecord, TRACE_PHASE_LABELS, toolDisplayName, traceMetric } from "./debug-utils";

/** 单条 Trace 的事件上限，与后端 execution_trace._MAX_EVENTS 保持一致。 */
export const TRACE_EVENT_CAP = 96;

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} 分 ${seconds} 秒`;
}

// 相对时间与驾驶舱共用同一套口径。
export { relativeTime } from "../agent-workspace/polling";

/** Token 等大数字的紧凑写法：12600 → 12.6k。 */
export function formatCompact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (Math.abs(value) < 10_000) return String(value);
  return `${(value / 1000).toFixed(value < 100_000 ? 1 : 0)}k`;
}

export function phaseLabel(phase: string): string {
  return TRACE_PHASE_LABELS[phase] ?? phase;
}

// ---------- 阶段耗时与瀑布图 ----------

export interface PhaseBreakdownRow {
  phase: string;
  label: string;
  count: number;
  totalMs: number;
  maxMs: number;
  share: number;
  failures: number;
}

export function phaseBreakdown(trace: AgentExecutionTrace): PhaseBreakdownRow[] {
  const rows = new Map<string, PhaseBreakdownRow>();
  for (const event of trace.events) {
    const row = rows.get(event.phase) ?? {
      phase: event.phase,
      label: phaseLabel(event.phase),
      count: 0,
      totalMs: 0,
      maxMs: 0,
      share: 0,
      failures: 0,
    };
    row.count += 1;
    if (event.durationMs != null) {
      row.totalMs += event.durationMs;
      row.maxMs = Math.max(row.maxMs, event.durationMs);
    }
    if (event.status === "failed") row.failures += 1;
    rows.set(event.phase, row);
  }
  const measured = [...rows.values()].reduce((sum, row) => sum + row.totalMs, 0);
  return [...rows.values()]
    .map((row) => ({ ...row, totalMs: round1(row.totalMs), share: measured > 0 ? row.totalMs / measured : 0 }))
    .sort((left, right) => right.totalMs - left.totalMs || right.count - left.count);
}

export interface WaterfallRow {
  event: AgentExecutionTraceEvent;
  startMs: number;
  endMs: number;
  startPct: number;
  widthPct: number;
  instant: boolean;
}

/**
 * 事件的 offsetMs 是“记录时刻”（工作完成之后），durationMs 是这段工作的耗时，
 * 因此条形图从 offset - duration 画到 offset；没有耗时的事件画成刻度点。
 */
export function waterfallRows(trace: AgentExecutionTrace): { totalMs: number; rows: WaterfallRow[] } {
  const lastOffset = trace.events.reduce((max, event) => Math.max(max, event.offsetMs), 0);
  const totalMs = Math.max(trace.durationMs ?? 0, lastOffset, 1);
  const rows = trace.events.map((event) => {
    const duration = event.durationMs ?? 0;
    const startMs = Math.max(0, event.offsetMs - duration);
    const endMs = Math.max(startMs, event.offsetMs);
    return {
      event,
      startMs,
      endMs,
      startPct: clampPct((startMs / totalMs) * 100),
      widthPct: Math.max(0.6, clampPct(((endMs - startMs) / totalMs) * 100)),
      instant: duration <= 0,
    };
  });
  return { totalMs, rows };
}

export function slowestEvents(trace: AgentExecutionTrace, limit = 3): AgentExecutionTraceEvent[] {
  return trace.events
    .filter((event) => event.durationMs != null && event.durationMs > 0)
    .sort((left, right) => (right.durationMs ?? 0) - (left.durationMs ?? 0))
    .slice(0, limit);
}

export interface TraceKpis {
  durationMs: number | null;
  events: number;
  llmRounds: number;
  toolCalls: number;
  failedEvents: number;
  promptTokens: number | null;
  completionTokens: number | null;
  cachedTokens: number | null;
}

export function traceKpis(trace: AgentExecutionTrace): TraceKpis {
  let promptTokens: number | null = null;
  let completionTokens: number | null = null;
  let cachedTokens: number | null = null;
  for (const event of trace.events) {
    if (event.phase !== "llm") continue;
    const usage = debugRecord(debugRecord(event.output).usage);
    // 真实执行把累计用量放在 usage.turn，调试试跑直接是 camelCase 的 usage。
    const turn = debugRecord(usage.turn);
    const source = Object.keys(turn).length > 0 ? turn : usage;
    const prompt = numberOrNull(source.prompt_tokens ?? source.promptTokens);
    const completion = numberOrNull(source.completion_tokens ?? source.completionTokens);
    const cached = numberOrNull(source.cached_tokens ?? source.cachedTokens);
    if (Object.keys(turn).length > 0) {
      // 回合累计值：取最后一次即可。
      if (prompt != null) promptTokens = prompt;
      if (completion != null) completionTokens = completion;
      if (cached != null) cachedTokens = cached;
    } else {
      if (prompt != null) promptTokens = (promptTokens ?? 0) + prompt;
      if (completion != null) completionTokens = (completionTokens ?? 0) + completion;
      if (cached != null) cachedTokens = (cachedTokens ?? 0) + cached;
    }
  }
  return {
    durationMs: trace.durationMs,
    events: trace.events.length,
    llmRounds: trace.events.filter((event) => event.phase === "llm").length,
    toolCalls: trace.events.filter((event) => event.phase === "tool").length,
    failedEvents: trace.events.filter((event) => event.status === "failed").length,
    promptTokens,
    completionTokens,
    cachedTokens,
  };
}

// ---------- 自动诊断 ----------

export type InsightLevel = "error" | "warning" | "info" | "success";

export interface TraceInsight {
  key: string;
  level: InsightLevel;
  title: string;
  detail: string;
  eventId?: string;
}

export const SLOW_MODEL_MS = 8_000;
export const SLOW_TRACE_MS = 15_000;
const LOW_CACHE_PROMPT_TOKENS = 2_000;
const LOW_CACHE_RATIO = 0.2;

export const INSIGHT_ORDER: Record<InsightLevel, number> = { error: 0, warning: 1, info: 2, success: 3 };

export function diagnoseTrace(trace: AgentExecutionTrace): TraceInsight[] {
  const insights: TraceInsight[] = [];
  const push = (insight: TraceInsight): void => {
    if (!insights.some((item) => item.key === insight.key)) insights.push(insight);
  };

  if (trace.outcome === "timeout") {
    push({ key: "timeout", level: "error", title: "模型调用超时", detail: "本轮在等待模型时超过时限。检查模型路由的超时、Provider 可用性，或缩短上下文。" });
  } else if (trace.status === "failed" || trace.outcome === "error") {
    push({ key: "trace-failed", level: "error", title: "执行以失败告终", detail: "整条 Trace 标记为失败，请从下方第一个失败事件开始排查。" });
  }
  if (trace.status === "running") {
    push({ key: "running", level: "info", title: "仍在执行中", detail: "Trace 还没有结束，刷新后可以看到后续事件。" });
  }

  for (const event of trace.events) {
    const input = debugRecord(event.input);
    const output = debugRecord(event.output);
    if (event.status === "failed") {
      const error = [output.error_type, output.error_message, output.error, event.detail].filter(Boolean).map(String)[0];
      push({ key: `failed-${event.id}`, level: "error", title: `${phaseLabel(event.phase)}失败：${event.label}`, detail: error ?? "事件没有附带错误信息。", eventId: event.id });
    }
    if (event.phase === "llm" && (event.durationMs ?? 0) >= SLOW_MODEL_MS) {
      push({ key: `slow-llm-${event.id}`, level: "warning", title: "模型响应偏慢", detail: `${event.label} 用了 ${formatDuration(event.durationMs)}。可以考虑换更快的模型路由或减少 Prompt。`, eventId: event.id });
    }
    if (event.phase === "llm" && String(output.finish_reason ?? output.finishReason ?? "") === "length") {
      push({ key: `length-${event.id}`, level: "warning", title: "模型输出被截断", detail: "结束原因是 length，回复可能不完整。可以调高最大输出 Token。", eventId: event.id });
    }
    if (event.phase === "llm") {
      const turn = debugRecord(debugRecord(output.usage).turn);
      const prompt = traceMetric(turn.prompt_tokens);
      const cached = traceMetric(turn.cached_tokens);
      if (prompt >= LOW_CACHE_PROMPT_TOKENS && cached / prompt < LOW_CACHE_RATIO) {
        push({ key: "low-cache", level: "info", title: "Prompt 缓存命中率低", detail: `本回合输入 ${prompt} Token，只命中 ${cached}（${Math.round((cached / prompt) * 100)}%）。稳定前缀变化会让缓存失效。`, eventId: event.id });
      }
    }
    if (event.phase === "prompt" && output.prompt_cache === "miss") {
      push({ key: "prompt-cache-miss", level: "info", title: "Prompt 前缀缓存未命中", detail: "人设、工具或稳定上下文发生了变化，这一轮需要完整计算前缀。", eventId: event.id });
    }
    if (event.phase === "capability") {
      const blocked = Array.isArray(output.blocked_tools) ? output.blocked_tools.length : 0;
      if (blocked > 0) {
        push({ key: "blocked-tools", level: "info", title: `${blocked} 个工具未开放`, detail: "权限等级、Bot 群身份或白名单限制了部分工具。到“工具”页签看具体原因。", eventId: event.id });
      }
    }
    if (event.phase === "media") {
      const items = Array.isArray(output.items) ? output.items.map(debugRecord) : [];
      const dropped = items.filter((item) => item.status === "dropped_unavailable").length;
      if (dropped > 0) {
        push({ key: `media-dropped-${event.id}`, level: "warning", title: `${dropped} 张图片无法读取`, detail: "图片下载失败且无法直传 URL，模型没有看到这些图片。", eventId: event.id });
      }
    }
    if (event.phase === "outbound") {
      const state = String(output.delivery_state ?? "");
      if (state === "confirmed_failure") {
        push({ key: `send-failed-${event.id}`, level: "error", title: "消息发送失败", detail: "OneBot 明确返回发送失败，群里没有收到这条回复。", eventId: event.id });
      } else if (state === "unknown" || event.status === "unknown") {
        push({ key: `send-unknown-${event.id}`, level: "warning", title: "投递结果未知", detail: "OneBot 没有确认是否发送成功，可能超时或连接抖动。", eventId: event.id });
      } else if (output.degraded_from) {
        push({ key: `send-degraded-${event.id}`, level: "info", title: "发送已降级", detail: `原方案 ${String(output.degraded_from)} 不可用，改用降级方案发送。`, eventId: event.id });
      }
    }
    if (event.status === "degraded" && event.phase !== "outbound" && event.phase !== "media") {
      push({ key: `degraded-${event.id}`, level: "warning", title: `${phaseLabel(event.phase)}降级：${event.label}`, detail: event.detail ?? "该步骤使用了降级路径。", eventId: event.id });
    }
    if (event.phase === "tool" && output.executed === false) {
      push({ key: "planned-tools", level: "info", title: "工具意图未执行", detail: "这是 dry-run 调试，模型计划的工具调用只记录为 skipped / planned，没有产生副作用。", eventId: event.id });
    }
    if (event.phase === "tool" && input.arguments === undefined && event.status === "failed") {
      push({ key: `tool-args-${event.id}`, level: "warning", title: "工具调用缺少参数", detail: "模型调用了工具但没有提供参数，可能是 Schema 描述不清。", eventId: event.id });
    }
  }

  if ((trace.durationMs ?? 0) >= SLOW_TRACE_MS) {
    push({ key: "slow-trace", level: "warning", title: "整轮耗时偏长", detail: `总耗时 ${formatDuration(trace.durationMs)}，群友可能已经等不及了。看看“瀑布图”里哪一段最长。` });
  }
  if (trace.events.length >= TRACE_EVENT_CAP) {
    push({ key: "truncated", level: "warning", title: "事件已截断", detail: `单条 Trace 最多记录 ${TRACE_EVENT_CAP} 个事件，后面的步骤没有被记录。` });
  }
  if (insights.length === 0) {
    push({ key: "healthy", level: "success", title: "没有发现问题", detail: "所有事件都正常完成，耗时也在合理范围内。" });
  }
  return insights.sort((left, right) => INSIGHT_ORDER[left.level] - INSIGHT_ORDER[right.level]);
}

// ---------- 过滤与搜索 ----------

export interface EventFilter {
  phases: string[];
  statuses: string[];
  query: string;
}

export function eventSearchText(event: AgentExecutionTraceEvent): string {
  return [event.label, event.phase, phaseLabel(event.phase), event.status, event.detail, JSON.stringify(event.input ?? {}), JSON.stringify(event.output ?? {})]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function filterEvents(events: AgentExecutionTraceEvent[], filter: EventFilter): AgentExecutionTraceEvent[] {
  const query = filter.query.trim().toLowerCase();
  return events.filter((event) => (
    (filter.phases.length === 0 || filter.phases.includes(event.phase))
    && (filter.statuses.length === 0 || filter.statuses.includes(event.status))
    && (!query || eventSearchText(event).includes(query))
  ));
}

export type TraceSort = "newest" | "slowest" | "events" | "problems";

export const TRACE_SORT_OPTIONS: Array<{ value: TraceSort; label: string }> = [
  { value: "newest", label: "最新优先" },
  { value: "problems", label: "问题优先" },
  { value: "slowest", label: "最慢优先" },
  { value: "events", label: "事件最多" },
];

function problemScore(trace: AgentExecutionTraceSummary): number {
  return (trace.hasFailure ? 4 : 0) + (trace.hasOutboundProblem ? 2 : 0) + (trace.hasDegradation ? 1 : 0);
}

export function sortSummaries(rows: AgentExecutionTraceSummary[], sort: TraceSort): AgentExecutionTraceSummary[] {
  const indexed = rows.map((row, index) => ({ row, index }));
  const byIndex = (left: { index: number }, right: { index: number }): number => left.index - right.index;
  if (sort === "slowest") indexed.sort((left, right) => (right.row.durationMs ?? -1) - (left.row.durationMs ?? -1) || byIndex(left, right));
  else if (sort === "events") indexed.sort((left, right) => right.row.eventCount - left.row.eventCount || byIndex(left, right));
  else if (sort === "problems") indexed.sort((left, right) => problemScore(right.row) - problemScore(left.row) || byIndex(left, right));
  return indexed.map((item) => item.row);
}

export function summarySearchText(trace: AgentExecutionTraceSummary): string {
  return [
    trace.traceId,
    trace.messageId,
    trace.actorUserId,
    trace.mode,
    trace.triggerSource,
    trace.status,
    trace.outcome,
    trace.preview,
    ...(trace.toolNames ?? []),
    ...(trace.toolNames ?? []).map(toolDisplayName),
  ].filter(Boolean).join(" ").toLowerCase();
}

export const TRACE_STATUS_FILTERS: Array<{ value: string; label: string; match: (trace: AgentExecutionTraceSummary) => boolean }> = [
  { value: "", label: "全部", match: () => true },
  { value: "failed", label: "失败", match: (trace) => trace.hasFailure },
  { value: "degraded", label: "降级", match: (trace) => trace.hasDegradation },
  { value: "outbound", label: "发送异常", match: (trace) => trace.hasOutboundProblem },
  { value: "tool", label: "工具", match: (trace) => trace.hasTool },
  { value: "media", label: "媒体", match: (trace) => trace.hasMedia },
  { value: "running", label: "执行中", match: (trace) => trace.status === "running" },
  { value: "completed", label: "已完成", match: (trace) => trace.status === "completed" },
];

export function statusFilterCounts(rows: AgentExecutionTraceSummary[]): Record<string, number> {
  return Object.fromEntries(TRACE_STATUS_FILTERS.map((filter) => [filter.value, rows.filter(filter.match).length]));
}

// ---------- Trace 对比 ----------

export interface AlignedEventRow {
  key: string;
  phase: string;
  label: string;
  left: AgentExecutionTraceEvent | null;
  right: AgentExecutionTraceEvent | null;
  deltaMs: number | null;
  statusChanged: boolean;
}

/** 按“阶段 + 标签 + 第 N 次出现”对齐两条 Trace 的事件，保持左侧顺序，右侧多出的追加在相邻位置。 */
export function alignTraces(left: AgentExecutionTrace, right: AgentExecutionTrace): AlignedEventRow[] {
  const keyed = (events: AgentExecutionTraceEvent[]): Array<{ key: string; event: AgentExecutionTraceEvent }> => {
    const seen = new Map<string, number>();
    return events.map((event) => {
      const base = `${event.phase}|${event.label}`;
      const occurrence = (seen.get(base) ?? 0) + 1;
      seen.set(base, occurrence);
      return { key: `${base}|${occurrence}`, event };
    });
  };
  const leftRows = keyed(left.events);
  const rightRows = keyed(right.events);
  const rightIndex = new Map(rightRows.map((row, index) => [row.key, index]));
  const leftKeys = new Set(leftRows.map((row) => row.key));
  const rows: AlignedEventRow[] = [];
  let cursor = 0;
  const emitRightOnly = (until: number): void => {
    for (; cursor < until; cursor += 1) {
      const candidate = rightRows[cursor];
      if (!leftKeys.has(candidate.key)) rows.push(buildAligned(candidate.key, null, candidate.event));
    }
  };
  for (const { key, event } of leftRows) {
    const index = rightIndex.get(key);
    if (index !== undefined && index >= cursor) {
      emitRightOnly(index);
      cursor = index + 1;
    }
    rows.push(buildAligned(key, event, index !== undefined ? rightRows[index].event : null));
  }
  emitRightOnly(rightRows.length);
  return rows;
}

function buildAligned(key: string, left: AgentExecutionTraceEvent | null, right: AgentExecutionTraceEvent | null): AlignedEventRow {
  const sample = (left ?? right) as AgentExecutionTraceEvent;
  return {
    key,
    phase: sample.phase,
    label: sample.label,
    left,
    right,
    deltaMs: left?.durationMs != null && right?.durationMs != null ? round1(right.durationMs - left.durationMs) : null,
    statusChanged: Boolean(left && right && left.status !== right.status),
  };
}

// ---------- 文本 Diff 与 Token 估算 ----------

export interface DiffLine {
  type: "same" | "add" | "del";
  text: string;
}

const LCS_CELL_LIMIT = 2_000_000;

/** 逐行 LCS Diff；超大文本退化为“整体替换”，避免浏览器卡死。 */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  if (before === after) return a.map((text) => ({ type: "same", text }));
  if (a.length * b.length > LCS_CELL_LIMIT) {
    return [...a.map((text) => ({ type: "del" as const, text })), ...b.map((text) => ({ type: "add" as const, text }))];
  }
  const rows = a.length + 1;
  const cols = b.length + 1;
  const table = new Uint32Array(rows * cols);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i * cols + j] = a[i] === b[j]
        ? table[(i + 1) * cols + j + 1] + 1
        : Math.max(table[(i + 1) * cols + j], table[i * cols + j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ type: "same", text: a[i] });
      i += 1;
      j += 1;
    } else if (table[(i + 1) * cols + j] >= table[i * cols + j + 1]) {
      out.push({ type: "del", text: a[i] });
      i += 1;
    } else {
      out.push({ type: "add", text: b[j] });
      j += 1;
    }
  }
  while (i < a.length) out.push({ type: "del", text: a[i++] });
  while (j < b.length) out.push({ type: "add", text: b[j++] });
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number; same: number } {
  return {
    added: lines.filter((line) => line.type === "add").length,
    removed: lines.filter((line) => line.type === "del").length,
    same: lines.filter((line) => line.type === "same").length,
  };
}

/** 折叠长段未变化的行，只保留变化附近的上下文。 */
export function collapseDiff(lines: DiffLine[], context = 2): Array<DiffLine | { type: "skip"; count: number }> {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.type === "same") return;
    for (let offset = -context; offset <= context; offset += 1) {
      const target = index + offset;
      if (target >= 0 && target < lines.length) keep[target] = true;
    }
  });
  const out: Array<DiffLine | { type: "skip"; count: number }> = [];
  let skipped = 0;
  lines.forEach((line, index) => {
    if (keep[index]) {
      if (skipped > 0) out.push({ type: "skip", count: skipped });
      skipped = 0;
      out.push(line);
    } else {
      skipped += 1;
    }
  });
  if (skipped > 0) out.push({ type: "skip", count: skipped });
  return out;
}

export function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((part) => {
      const row = debugRecord(part);
      if (typeof row.text === "string") return row.text;
      if (row.type === "image_url") return "[图片]";
      return JSON.stringify(part);
    }).join("\n");
  }
  if (content == null) return "";
  return JSON.stringify(content, null, 2);
}

const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]/g;

/** 粗略 Token 估算：中日韩字符按 1 个 Token，其余按 4 字符 1 个 Token。只用于相对比较。 */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  const cjk = text.match(CJK_RE)?.length ?? 0;
  const rest = text.length - cjk;
  return cjk + Math.ceil(rest / 4);
}

export interface PromptMessageStat {
  index: number;
  role: string;
  chars: number;
  tokens: number;
  text: string;
}

export function promptStats(messages: AgentDebugResponse["promptMessages"]): { rows: PromptMessageStat[]; chars: number; tokens: number; byRole: Record<string, number> } {
  const rows = messages.map((message, index) => {
    const text = contentText(message.content);
    return { index, role: message.role, chars: text.length, tokens: estimateTokens(text), text };
  });
  const byRole: Record<string, number> = {};
  rows.forEach((row) => { byRole[row.role] = (byRole[row.role] ?? 0) + row.tokens; });
  return {
    rows,
    chars: rows.reduce((sum, row) => sum + row.chars, 0),
    tokens: rows.reduce((sum, row) => sum + row.tokens, 0),
    byRole,
  };
}

export function promptAsText(messages: AgentDebugResponse["promptMessages"]): string {
  return messages.map((message) => `### ${message.role}\n${contentText(message.content)}`).join("\n\n");
}

export function percentChange(before: number | null | undefined, after: number | null | undefined): string {
  if (before == null || after == null) return "";
  if (before === 0) return after === 0 ? "±0%" : "新增";
  const change = ((after - before) / before) * 100;
  return `${change > 0 ? "+" : ""}${change.toFixed(Math.abs(change) < 10 ? 1 : 0)}%`;
}

// ---------- 导出 ----------

export function traceMarkdown(trace: AgentExecutionTrace, insights: TraceInsight[] = diagnoseTrace(trace)): string {
  const kpis = traceKpis(trace);
  const lines = [
    `## Agent Trace ${trace.traceId.slice(0, 12)}`,
    "",
    `- 群：${trace.groupId}`,
    `- 场景：${trace.mode} · 来源：${trace.source}${trace.triggerSource ? ` · 触发：${trace.triggerSource}` : ""}`,
    `- 开始：${trace.startedAt}`,
    `- 状态：${trace.status}${trace.outcome ? ` / ${trace.outcome}` : ""}`,
    `- 耗时：${formatDuration(kpis.durationMs)} · 事件 ${kpis.events} · 模型 ${kpis.llmRounds} 轮 · 工具 ${kpis.toolCalls} 次`,
    "",
    "### 诊断",
    ...insights.map((item) => `- [${item.level}] ${item.title}：${item.detail}`),
    "",
    "### 事件",
    "| # | 阶段 | 事件 | 状态 | +ms | 耗时 |",
    "|---|---|---|---|---|---|",
    ...trace.events.map((event, index) => `| ${index + 1} | ${phaseLabel(event.phase)} | ${event.label.replace(/\|/g, "\\|")} | ${event.status} | ${event.offsetMs.toFixed(1)} | ${formatDuration(event.durationMs)} |`),
  ];
  return lines.join("\n");
}

export function downloadJson(filename: string, value: unknown): void {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

function clampPct(value: number): number {
  return Math.min(100, Math.max(0, value));
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
