import { describe, expect, it } from "vitest";
import {
  describeScheduleAccess,
  draftFromRule,
  emptyRuleDraft,
  formatEvery,
  formatLocal,
  parseClockList,
  parseDateList,
  parseScheduleTab,
  relativeTo,
  resultColor,
  ruleFromDraft,
  RULE_PRESETS,
  sameRule,
  validateRuleDraft,
  validateScheduleSettings,
} from "./schedule-meta";
import type { ScheduleRule, ScheduleSettings } from "./types";

const settings: ScheduleSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  whoCanCreate: "members",
  allowAgentMode: true,
  maxActivePerGroup: 20,
  maxActivePerMember: 5,
  minIntervalMinutes: 10,
  maxHorizonDays: 366,
  groupDailyRuns: 60,
  misfireGraceMinutes: 10,
  missedPolicy: "run_once",
  catchUpMaxHours: 6,
  maxConsecutiveFailures: 3,
  quietHoursEnabled: false,
  quietStart: "01:00",
  quietEnd: "07:00",
  quietPolicy: "defer",
  concurrency: 2,
  runTimeoutSeconds: 300,
  notifyPrefix: "⏰ ",
  historyLimit: 20,
};

describe("schedule-meta", () => {
  it("parses tabs defensively", () => {
    expect(parseScheduleTab(null)).toBe("tasks");
    expect(parseScheduleTab("policy")).toBe("policy");
    expect(parseScheduleTab("nope")).toBe("tasks");
  });

  it("parses clock and date lists with normalisation, dedupe and errors", () => {
    expect(parseClockList("9:00, 21：30 09:00 25:00")).toEqual({ times: ["09:00", "21:30"], invalid: ["25:00"] });
    expect(parseDateList("2026-10-02 2026-10-01 10-03")).toEqual({ dates: ["2026-10-01", "2026-10-02"], invalid: ["10-03"] });
  });

  it("builds rules per kind and drops irrelevant fields", () => {
    const base = emptyRuleDraft("weekly");
    expect(ruleFromDraft({ ...base, weekdays: [5, 1], times: ["08:00"], months: [12] })).toEqual({
      kind: "weekly",
      times: ["08:00"],
      weekdays: [1, 5],
      months: [12],
    });
    expect(ruleFromDraft({ ...base, kind: "once", onceMode: "delay", delayMinutes: 15 })).toEqual({ kind: "once", delay_minutes: 15 });
    expect(ruleFromDraft({ ...base, kind: "interval", everyMinutes: 90, windowEnabled: true, startAt: "2026-11-01 00:00", months: [3] })).toEqual({
      kind: "interval",
      every_minutes: 90,
      window: { start: "09:00", end: "22:00" },
      start_at: "2026-11-01 00:00",
    });
    expect(ruleFromDraft({ ...base, kind: "cron", cron: " 0 9 * * 1-5 ", skipDates: ["2026-10-01"] })).toEqual({
      kind: "cron",
      cron: "0 9 * * 1-5",
      skip_dates: ["2026-10-01"],
    });
  });

  it("round-trips stored rules through the form", () => {
    const rules: ScheduleRule[] = [
      { kind: "monthly", times: ["20:00"], month_days: [1, -1], months: [1, 7] },
      { kind: "interval", every_minutes: 30, window: { start: "22:00", end: "02:00" }, end_at: "2026-12-31 23:59" },
      { kind: "once", at: "2026-10-27 08:00" },
      { kind: "cron", cron: "*/15 9-18 * * *", skip_dates: ["2026-10-01"] },
    ];
    for (const rule of rules) expect(sameRule(ruleFromDraft(draftFromRule(rule)), rule)).toBe(true);
    expect(sameRule({ kind: "daily", times: ["08:00"] }, { times: ["08:00"], kind: "daily" })).toBe(true);
    expect(sameRule({ kind: "daily", times: ["08:00"] }, { kind: "daily", times: ["09:00"] })).toBe(false);
  });

  it("pre-validates obvious mistakes", () => {
    expect(validateRuleDraft({ ...emptyRuleDraft("once"), onceMode: "at", at: "明天" })).toHaveProperty("at");
    expect(validateRuleDraft({ ...emptyRuleDraft("weekly"), weekdays: [] })).toHaveProperty("weekdays");
    expect(validateRuleDraft({ ...emptyRuleDraft("daily"), times: [] })).toHaveProperty("times");
    expect(validateRuleDraft({ ...emptyRuleDraft("monthly"), monthDays: [0] })).toHaveProperty("monthDays");
    expect(validateRuleDraft({ ...emptyRuleDraft("cron"), cron: "0 9 *" })).toHaveProperty("cron");
    expect(validateRuleDraft({ ...emptyRuleDraft("cron"), cron: "@daily" })).toEqual({});
    expect(validateRuleDraft({ ...emptyRuleDraft("interval"), windowEnabled: true, windowStart: "10:00", windowEnd: "10:00" })).toHaveProperty("window");
    expect(validateRuleDraft({ ...emptyRuleDraft("daily"), startAt: "2026-12-01 00:00", endAt: "2026-11-01 00:00" })).toHaveProperty("endAt");
    for (const preset of RULE_PRESETS) expect(validateRuleDraft(preset.apply(emptyRuleDraft()))).toEqual({});
  });

  it("formats Beijing-time strings without trusting the browser clock", () => {
    expect(formatLocal("2026-10-26T09:00")).toBe("10月26日 周一 09:00");
    expect(formatLocal(null)).toBe("—");
    expect(relativeTo("2026-10-26T11:00", "2026-10-26T09:00")).toBe("2 小时后");
    expect(relativeTo("2026-10-26 08:40", "2026-10-26T09:00")).toBe("20 分钟前");
    expect(relativeTo("2026-10-29T09:00", "2026-10-26T09:00")).toBe("3 天后");
    expect(formatEvery(90)).toBe("90 分钟");
    expect(formatEvery(120)).toBe("2 小时");
    expect(formatEvery(2880)).toBe("2 天");
  });

  it("colours results by tone", () => {
    expect(resultColor("spoke")).toBe("green");
    expect(resultColor("failed")).toBe("red");
    expect(resultColor("skipped_missed")).toBe("orange");
    expect(resultColor("anything")).toBe("default");
  });

  it("validates policy settings like the backend", () => {
    expect(validateScheduleSettings(settings)).toEqual({});
    const errors = validateScheduleSettings({
      ...settings,
      groupMode: "allowlist",
      maxActivePerMember: 30,
      concurrency: 9,
      quietHoursEnabled: true,
      quietEnd: "01:00",
      blockedGroups: ["12"],
    });
    expect(Object.keys(errors).sort()).toEqual(["allowedGroups", "blockedGroups", "concurrency", "maxActivePerMember", "quietEnd"]);
    expect(describeScheduleAccess({ ...settings, whoCanCreate: "admins", allowAgentMode: false })).toBe("所有开启 Agent 的群 · 仅群主/管理员可创建 · 只能直接提醒");
    expect(describeScheduleAccess({ ...settings, enabled: false })).toContain("已关闭");
  });
});
