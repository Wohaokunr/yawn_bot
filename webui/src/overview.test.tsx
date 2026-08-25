import { describe, expect, it } from "vitest";
import { aiOutcomeMeta, buildOverviewIssues, fanqieSummary, formatLatency, formatRate, formatUptime } from "./overview";
import type { Overview } from "./types";

describe("formatUptime", () => {
  it("按秒/分/时/天分级展示", () => {
    expect(formatUptime(0)).toBe("0 秒");
    expect(formatUptime(59)).toBe("59 秒");
    expect(formatUptime(60)).toBe("1 分 0 秒");
    expect(formatUptime(3725)).toBe("1 小时 2 分");
    expect(formatUptime(90061)).toBe("1 天 1 小时");
  });

  it("负数按 0 处理，不出现负时长", () => {
    expect(formatUptime(-5)).toBe("0 秒");
  });
});

describe("formatRate", () => {
  it("无数据时显示占位符", () => {
    expect(formatRate(null)).toBe("—");
  });

  it("百分比保留一位小数", () => {
    expect(formatRate(1)).toBe("100.0%");
    expect(formatRate(2 / 3)).toBe("66.7%");
    expect(formatRate(0)).toBe("0.0%");
  });
});

describe("formatLatency", () => {
  it("无数据时显示占位符", () => {
    expect(formatLatency(null)).toBe("—");
  });

  it("毫秒与秒分级展示", () => {
    expect(formatLatency(123.4)).toBe("123 ms");
    expect(formatLatency(999)).toBe("999 ms");
    expect(formatLatency(1500)).toBe("1.5 s");
    expect(formatLatency(20333)).toBe("20.3 s");
  });
});

describe("aiOutcomeMeta", () => {
  it("覆盖后端已知的全部 AI outcome 取值", () => {
    for (const outcome of [
      "success",
      "error",
      "timeout",
      "empty",
      "not_configured",
      "cancelled",
      "unsupported_multimodal",
    ]) {
      const meta = aiOutcomeMeta(outcome);
      expect(meta.label.length).toBeGreaterThan(0);
      expect(meta.color.length).toBeGreaterThan(0);
    }
  });

  it("未知取值回退为原值展示", () => {
    expect(aiOutcomeMeta("weird_future").label).toBe("weird_future");
  });
});

describe("fanqieSummary", () => {
  it("已知状态按固定顺序摘要，忽略未知状态", () => {
    expect(fanqieSummary({ queued: 2, running: 1, failed: 0, completed: 12 })).toBe(
      "排队 2 · 进行 1 · 失败 0 · 完成 12",
    );
    expect(fanqieSummary({ weird: 3, queued: 1 })).toBe("排队 1");
  });

  it("空任务显示占位符", () => {
    expect(fanqieSummary({})).toBe("暂无任务");
  });
});

function makeOverviewForIssues(): Overview {
  return {
    bots: ["123"],
    plugins: [{ name: "Core", state: "loaded", detail: null }],
    counts: { groups: 1, users: 1, enabledAgents: 1 },
    recentAgentActions: [],
    metrics: { counters: [], histograms: [] },
    generatedAt: "2026-08-24T17:00:00+08:00",
    stats: {
      ai: {
        requestsTotal: 1,
        success: 1,
        failed: 0,
        successRate: 1,
        byOutcome: [{ outcome: "success", count: 1 }],
        avgDurationMs: 10,
        p95DurationMs: 10,
        degradations: 0,
        health: [],
      },
      llm: { routes: [], unconfiguredProviders: [] },
      memory: {
        compactingGroups: 0,
        rebuildRequired: 0,
        failingGroups: 0,
        recentError: null,
      },
      jobs: { fanqie: { available: true, byStatus: {} }, reminderErrors: 0 },
      activity: {
        messages24h: 0,
        activeGroups24h: 0,
        agentResponseGroups24h: 0,
        proactiveToday: 0,
        adminToolToday: 0,
      },
      games: {
        live: { rpg: { available: true, count: 0 }, werewolf: { available: true, count: 0 } },
        endedToday: { rpg: 0, werewolf: 0 },
      },
      uptime: { startedAt: "2026-08-24T16:00:00+08:00", uptimeSeconds: 3600 },
    },
  };
}

describe("buildOverviewIssues", () => {
  it("健康快照不产生待处理项", () => {
    expect(buildOverviewIssues(makeOverviewForIssues())).toEqual([]);
  });

  it("聚合 Bot、Provider、AI、记忆与提醒异常并提供处理入口", () => {
    const overview = makeOverviewForIssues();
    overview.bots = [];
    overview.stats.ai.byOutcome = [{ outcome: "timeout", count: 2 }];
    overview.stats.ai.health = [{ operation: "agent_proactive", consecutiveFailures: 2, lastFailureOutcome: "timeout" }];
    overview.stats.llm.unconfiguredProviders = ["default"];
    overview.stats.memory.failingGroups = 1;
    overview.stats.memory.recentError = { groupId: "100", error: "memory failed", at: null };
    overview.stats.jobs.reminderErrors = 1;
    const issues = buildOverviewIssues(overview);
    expect(issues.map((item) => item.key)).toEqual([
      "bot-offline",
      "ai-failures",
      "llm-provider",
      "memory",
      "reminders",
    ]);
    expect(issues.find((item) => item.key === "memory")?.to).toBe("/agent/100?tab=memories");
    expect(issues.find((item) => item.key === "llm-provider")?.to).toBe("/environment");
  });

  it("历史累计失败在后续成功恢复后不再产生当前 AI 告警", () => {
    const overview = makeOverviewForIssues();
    overview.stats.ai.failed = 32;
    overview.stats.ai.byOutcome = [
      { outcome: "error", count: 32 },
      { outcome: "success", count: 4 },
    ];
    overview.stats.ai.health = [];
    expect(buildOverviewIssues(overview).some((item) => item.key === "ai-failures")).toBe(false);
  });
});
