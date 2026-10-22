import { describe, expect, it } from "vitest";
import type { AgentExecutionTrace, AgentExecutionTraceEvent, AgentExecutionTraceSummary } from "../types";
import {
  alignTraces,
  collapseDiff,
  diagnoseTrace,
  diffStats,
  estimateTokens,
  filterEvents,
  formatDuration,
  lineDiff,
  percentChange,
  phaseBreakdown,
  promptStats,
  relativeTime,
  sortSummaries,
  statusFilterCounts,
  TRACE_EVENT_CAP,
  traceKpis,
  traceMarkdown,
  waterfallRows,
} from "./trace-analysis";

function event(id: string, phase: string, partial: Partial<AgentExecutionTraceEvent> = {}): AgentExecutionTraceEvent {
  return {
    id,
    phase,
    label: partial.label ?? `${phase} 事件`,
    status: partial.status ?? "success",
    offsetMs: partial.offsetMs ?? 0,
    durationMs: partial.durationMs ?? null,
    input: partial.input ?? {},
    output: partial.output ?? {},
    detail: partial.detail ?? null,
    round: partial.round ?? null,
  };
}

function trace(events: AgentExecutionTraceEvent[], partial: Partial<AgentExecutionTrace> = {}): AgentExecutionTrace {
  return {
    traceId: partial.traceId ?? "t1",
    groupId: "1",
    mode: "dialogue",
    source: "runtime",
    triggerSource: "mention",
    actorUserId: "10",
    messageId: "20",
    startedAt: "2026-10-22T12:00:00+08:00",
    status: partial.status ?? "completed",
    outcome: partial.outcome ?? "completed",
    durationMs: partial.durationMs ?? 1000,
    events,
  };
}

function summary(traceId: string, partial: Partial<AgentExecutionTraceSummary> = {}): AgentExecutionTraceSummary {
  return {
    traceId,
    groupId: "1",
    mode: "dialogue",
    source: "runtime",
    triggerSource: "mention",
    actorUserId: "10",
    messageId: "20",
    startedAt: "2026-10-22T12:00:00+08:00",
    status: "completed",
    outcome: "completed",
    durationMs: 100,
    eventCount: 3,
    hasFailure: false,
    hasDegradation: false,
    hasTool: false,
    hasMedia: false,
    hasOutboundProblem: false,
    ...partial,
  };
}

describe("格式化", () => {
  it("按量级切换时长单位", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0.4)).toBe("<1 ms");
    expect(formatDuration(5.25)).toBe("5.3 ms");
    expect(formatDuration(420)).toBe("420 ms");
    expect(formatDuration(1234)).toBe("1.23 s");
    expect(formatDuration(25_000)).toBe("25.0 s");
    expect(formatDuration(125_000)).toBe("2 分 5 秒");
  });

  it("相对时间", () => {
    const now = new Date("2026-10-22T12:10:00+08:00").getTime();
    expect(relativeTime("2026-10-22T12:09:55+08:00", now)).toBe("刚刚");
    expect(relativeTime("2026-10-22T12:09:00+08:00", now)).toBe("1 分钟前");
    expect(relativeTime("2026-10-22T09:00:00+08:00", now)).toBe("3 小时前");
    expect(relativeTime(null, now)).toBe("—");
  });

  it("百分比变化", () => {
    expect(percentChange(100, 150)).toBe("+50%");
    expect(percentChange(100, 95)).toBe("-5.0%");
    expect(percentChange(0, 0)).toBe("±0%");
    expect(percentChange(0, 3)).toBe("新增");
    expect(percentChange(null, 3)).toBe("");
  });
});

describe("耗时分析", () => {
  const sample = trace([
    event("e1", "intake", { offsetMs: 2 }),
    event("e2", "context", { offsetMs: 120, durationMs: 100 }),
    event("e3", "llm", { offsetMs: 900, durationMs: 700 }),
    event("e4", "llm", { offsetMs: 980, durationMs: 60, status: "failed" }),
  ]);

  it("按阶段汇总并按耗时排序", () => {
    const rows = phaseBreakdown(sample);
    expect(rows.map((row) => row.phase)).toEqual(["llm", "context", "intake"]);
    expect(rows[0]).toMatchObject({ count: 2, totalMs: 760, maxMs: 700, failures: 1 });
    expect(rows[0].share).toBeCloseTo(760 / 860);
    expect(rows[2].totalMs).toBe(0);
  });

  it("瀑布图从 offset - duration 画到 offset", () => {
    const { totalMs, rows } = waterfallRows(sample);
    expect(totalMs).toBe(1000);
    expect(rows[1]).toMatchObject({ startMs: 20, endMs: 120, startPct: 2, instant: false });
    expect(rows[1].widthPct).toBeCloseTo(10);
    expect(rows[0].instant).toBe(true);
  });

  it("汇总 KPI 并优先使用回合累计 Token", () => {
    const withUsage = trace([
      event("a", "llm", { output: { usage: { turn: { prompt_tokens: 1000, completion_tokens: 20, cached_tokens: 100 } } } }),
      event("b", "llm", { output: { usage: { turn: { prompt_tokens: 2200, completion_tokens: 50, cached_tokens: 900 } } } }),
      event("c", "tool"),
    ]);
    expect(traceKpis(withUsage)).toMatchObject({ llmRounds: 2, toolCalls: 1, promptTokens: 2200, completionTokens: 50, cachedTokens: 900 });
    const debugUsage = trace([event("a", "llm", { output: { usage: { promptTokens: 300, completionTokens: 9 } } })]);
    expect(traceKpis(debugUsage)).toMatchObject({ promptTokens: 300, completionTokens: 9 });
  });
});

describe("自动诊断", () => {
  it("健康 Trace 给出成功结论", () => {
    expect(diagnoseTrace(trace([event("e1", "intake")])).map((item) => item.key)).toEqual(["healthy"]);
  });

  it("按严重度排序并能定位到事件", () => {
    const insights = diagnoseTrace(trace([
      event("e1", "capability", { output: { blocked_tools: [{ name: "kick_member", reason: "bot_not_admin" }] } }),
      event("e2", "llm", { durationMs: 9_000, output: { finish_reason: "length" } }),
      event("e3", "media", { output: { items: [{ status: "dropped_unavailable" }] } }),
      event("e4", "outbound", { status: "unknown", output: { delivery_state: "unknown" } }),
      event("e5", "tool", { status: "failed", output: { error: "权限不足" } }),
    ], { durationMs: 20_000 }));
    const levels = insights.map((item) => item.level);
    expect(levels).toEqual([...levels].sort((a, b) => ["error", "warning", "info", "success"].indexOf(a) - ["error", "warning", "info", "success"].indexOf(b)));
    const keys = insights.map((item) => item.key);
    expect(keys).toContain("failed-e5");
    expect(keys).toContain("slow-llm-e2");
    expect(keys).toContain("length-e2");
    expect(keys).toContain("media-dropped-e3");
    expect(keys).toContain("send-unknown-e4");
    expect(keys).toContain("blocked-tools");
    expect(keys).toContain("slow-trace");
    expect(insights.find((item) => item.key === "failed-e5")?.detail).toBe("权限不足");
    expect(insights.find((item) => item.key === "failed-e5")?.eventId).toBe("e5");
  });

  it("提示超时、低缓存命中和事件截断", () => {
    const events = Array.from({ length: TRACE_EVENT_CAP }, (_, index) => event(`e${index}`, "parse"));
    events[1] = event("e1", "llm", { output: { usage: { turn: { prompt_tokens: 5000, cached_tokens: 100 } } } });
    const keys = diagnoseTrace(trace(events, { outcome: "timeout", status: "failed" })).map((item) => item.key);
    expect(keys).toEqual(expect.arrayContaining(["timeout", "low-cache", "truncated"]));
    expect(keys).not.toContain("trace-failed");
  });
});

describe("过滤与排序", () => {
  it("按阶段、状态和关键字过滤事件", () => {
    const events = [
      event("e1", "llm", { label: "模型调用" }),
      event("e2", "tool", { label: "工具 mute_member", status: "failed", output: { error: "bot_not_admin" } }),
    ];
    expect(filterEvents(events, { phases: ["tool"], statuses: [], query: "" }).map((item) => item.id)).toEqual(["e2"]);
    expect(filterEvents(events, { phases: [], statuses: ["success"], query: "" }).map((item) => item.id)).toEqual(["e1"]);
    expect(filterEvents(events, { phases: [], statuses: [], query: "BOT_NOT" }).map((item) => item.id)).toEqual(["e2"]);
  });

  it("排序稳定且问题优先", () => {
    const rows = [
      summary("a", { durationMs: 10 }),
      summary("b", { durationMs: 300, hasDegradation: true }),
      summary("c", { durationMs: 50, hasFailure: true, eventCount: 9 }),
    ];
    expect(sortSummaries(rows, "newest").map((row) => row.traceId)).toEqual(["a", "b", "c"]);
    expect(sortSummaries(rows, "slowest").map((row) => row.traceId)).toEqual(["b", "c", "a"]);
    expect(sortSummaries(rows, "events").map((row) => row.traceId)).toEqual(["c", "a", "b"]);
    expect(sortSummaries(rows, "problems").map((row) => row.traceId)).toEqual(["c", "b", "a"]);
    expect(statusFilterCounts(rows)).toMatchObject({ "": 3, failed: 1, degraded: 1, completed: 3, running: 0 });
  });
});

describe("Trace 对齐", () => {
  it("按阶段+标签+出现次数对齐，并标出增删与状态变化", () => {
    const left = trace([
      event("l1", "intake", { label: "输入" }),
      event("l2", "llm", { label: "模型", durationMs: 100 }),
      event("l3", "tool", { label: "工具 a" }),
    ]);
    const right = trace([
      event("r1", "intake", { label: "输入" }),
      event("r2", "media", { label: "图片" }),
      event("r3", "llm", { label: "模型", durationMs: 160, status: "degraded" }),
      event("r4", "llm", { label: "模型", durationMs: 50 }),
    ], { traceId: "t2" });
    const rows = alignTraces(left, right);
    expect(rows.map((row) => `${row.left?.id ?? "-"}:${row.right?.id ?? "-"}`)).toEqual(["l1:r1", "-:r2", "l2:r3", "l3:-", "-:r4"]);
    expect(rows[2]).toMatchObject({ deltaMs: 60, statusChanged: true });
  });
});

describe("文本 Diff 与 Token", () => {
  it("逐行 LCS 并折叠未变化的长段", () => {
    const before = ["a", "b", "c", "d", "e", "f", "g", "h"].join("\n");
    const after = ["a", "b", "c", "X", "e", "f", "g", "h", "i"].join("\n");
    const lines = lineDiff(before, after);
    expect(diffStats(lines)).toEqual({ added: 2, removed: 1, same: 7 });
    const collapsed = collapseDiff(lines, 1);
    expect(collapsed[0]).toEqual({ type: "skip", count: 2 });
    expect(collapsed.some((line) => line.type === "skip" && line.count === 2)).toBe(true);
    expect(lineDiff("same", "same")).toEqual([{ type: "same", text: "same" }]);
  });

  it("中文按字、其余按 4 字符估算 Token", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("你好")).toBe(2);
    expect(estimateTokens("hello world!")).toBe(3);
    const stats = promptStats([
      { role: "system", content: "你是猫娘" },
      { role: "user", content: [{ type: "text", text: "hi" }, { type: "image_url", image_url: {} }] },
    ]);
    expect(stats.rows[1].text).toBe("hi\n[图片]");
    expect(stats.byRole.system).toBe(4);
    expect(stats.tokens).toBe(stats.rows[0].tokens + stats.rows[1].tokens);
  });
});

describe("导出", () => {
  it("Markdown 摘要包含诊断与事件表，并转义竖线", () => {
    const markdown = traceMarkdown(trace([event("e1", "tool", { label: "a|b", durationMs: 12 })]));
    expect(markdown).toContain("## Agent Trace t1");
    expect(markdown).toContain("### 诊断");
    expect(markdown).toContain("a\\|b");
    expect(markdown).toContain("12 ms");
  });
});
