import { describe, expect, it } from "vitest";
import type { MoodItem, TodoItem, WerewolfGameRow } from "./me-space-api";
import {
  addDays, bucketOf, calendarCells, compare, completionPercent, daysBetween, filterGames, formatDuration, groupTodos, heatColumns, heatLevel,
  highlightParts, hourLabel, insightHighlights, monthLabel, moodByWeekday, moodDistribution, moodStreak, onboardingSteps, parseQuickTodo,
  recentForm, secondsBetween, shiftMonth, sortTodos, sparkPoints, suggestPassword, todosToMarkdown, weekdayOf, werewolfTitle,
} from "./me-hub-meta";

const TODAY = "2026-11-10"; // 周二

const todo = (patch: Partial<TodoItem>): TodoItem => ({ id: 1, content: "x", priority: "normal", priorityLabel: "", done: false, due: null, dueInDays: null, overdue: false, createdAt: null, doneAt: null, ...patch });
const mood = (at: string, score: number): MoodItem => ({ index: 0, at, score, label: "", note: "" });
const game = (patch: Partial<WerewolfGameRow>): WerewolfGameRow => ({ gameId: "1", groupId: "1", groupName: null, board: "预女猎白", players: 9, startedAt: null, endedAt: "2026-11-01", role: "狼人", faction: "wolf", seat: 1, sheriff: false, won: true, deathRound: null, deathCause: null, rounds: 3, winner: "wolf", ...patch });

describe("日期小工具", () => {
  it("adds days across months and knows the weekday", () => {
    expect(addDays("2026-11-30", 1)).toBe("2026-12-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(daysBetween("2026-11-10", "2026-11-17")).toBe(7);
    expect(weekdayOf(TODAY)).toBe(1);
  });

  it("shifts months over the year boundary", () => {
    expect(shiftMonth("2026-12", 1)).toBe("2027-01");
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(monthLabel("2026-11")).toBe("2026 年 11 月");
  });

  it("pads calendar cells to whole weeks", () => {
    const cells = calendarCells(6, Array.from({ length: 30 }, (_value, index) => index + 1));
    expect(cells.length % 7).toBe(0);
    expect(cells.slice(0, 7)).toEqual([null, null, null, null, null, null, 1]);
  });
});

describe("parseQuickTodo", () => {
  it("reads relative dates and priority marks at the edges", () => {
    expect(parseQuickTodo("明天 交报告 !重要", TODAY)).toMatchObject({ content: "交报告", due: "2026-11-11", priority: "high" });
    expect(parseQuickTodo("买菜 后天", TODAY)).toMatchObject({ content: "买菜", due: "2026-11-12" });
    expect(parseQuickTodo("3天后 复诊 !不急", TODAY)).toMatchObject({ content: "复诊", due: "2026-11-13", priority: "low" });
  });

  it("reads weekdays: this week rolls forward, 下周 adds seven", () => {
    expect(parseQuickTodo("周五 聚餐", TODAY).due).toBe("2026-11-13");
    expect(parseQuickTodo("周一 例会", TODAY).due).toBe("2026-11-16");
    expect(parseQuickTodo("下周二 体检", TODAY).due).toBe("2026-11-17");
  });

  it("reads month-day forms and rolls a passed date into next year", () => {
    expect(parseQuickTodo("12月25日 寄礼物", TODAY).due).toBe("2026-12-25");
    expect(parseQuickTodo("1-5 交房租", TODAY).due).toBe("2027-01-05");
    expect(parseQuickTodo("2027-02-30 不存在的日子", TODAY)).toMatchObject({ due: null, content: "2027-02-30 不存在的日子" });
  });

  it("leaves date words in the middle of a sentence alone", () => {
    expect(parseQuickTodo("准备 明天 的见面礼", TODAY)).toMatchObject({ content: "准备 明天 的见面礼", due: null });
    expect(parseQuickTodo("明天见面礼", TODAY)).toMatchObject({ content: "明天见面礼", due: null });
  });

  it("never produces an empty todo", () => {
    expect(parseQuickTodo("明天", TODAY)).toMatchObject({ content: "明天", due: null, priority: "normal" });
    expect(parseQuickTodo("  ", TODAY).content).toBe("");
  });
});

describe("待办分组与排序", () => {
  const items = [
    todo({ id: 1, dueInDays: -2, due: "2026-11-08", overdue: true }),
    todo({ id: 2, dueInDays: 0, due: TODAY }),
    todo({ id: 3, dueInDays: 1, due: "2026-11-11", priority: "high" }),
    todo({ id: 4, dueInDays: 5, due: "2026-11-15" }),
    todo({ id: 5, dueInDays: 20, due: "2026-11-30", priority: "low" }),
    todo({ id: 6 }),
    todo({ id: 7, done: true, dueInDays: -3, due: "2026-11-07" }),
  ];

  it("buckets by due date, done wins over overdue", () => {
    expect(items.map(bucketOf)).toEqual(["overdue", "today", "tomorrow", "week", "later", "someday", "done"]);
    expect(groupTodos(items.slice(0, 2)).map((group) => group.label)).toEqual(["逾期", "今天"]);
  });

  it("sorts without mutating and keeps done last", () => {
    const byPriority = sortTodos(items, "priority").map((item) => item.id);
    expect(byPriority[0]).toBe(3);
    expect(byPriority.at(-1)).toBe(7);
    expect(sortTodos(items, "newest")[0].id).toBe(7);
    expect(sortTodos(items, "smart")).toBe(items);
    expect(items[0].id).toBe(1);
  });

  it("exports a markdown checklist", () => {
    expect(todosToMarkdown([todo({ content: "交报告", priority: "high", due: TODAY }), todo({ content: "买奶", done: true })])).toBe("- [ ] 交报告 ❗ （2026-11-10）\n- [x] 买奶");
    expect(completionPercent(1, 3)).toBe(25);
    expect(completionPercent(0, 0)).toBe(0);
  });
});

describe("图形数据", () => {
  it("levels heat cells against the max", () => {
    expect(heatLevel(0, 10)).toBe(0);
    expect(heatLevel(1, 10)).toBe(1);
    expect(heatLevel(10, 10)).toBe(4);
    expect(heatLevel(3, 0)).toBe(0);
  });

  it("lays days out in Monday-first week columns ending today", () => {
    const columns = heatColumns(TODAY, 14, { [TODAY]: 3 });
    const flat = columns.flat().filter((cell) => cell !== null);
    expect(flat).toHaveLength(14);
    expect(flat.at(-1)).toEqual({ day: TODAY, value: 3 });
    expect(columns.every((column) => column.length === 7)).toBe(true);
    expect(columns.at(-1)?.[1]).toEqual({ day: TODAY, value: 3 });
  });

  it("draws a sparkline inside the box", () => {
    const points = sparkPoints([0, 5, 10], 100, 20).split(" ").map((pair) => pair.split(",").map(Number));
    expect(points).toHaveLength(3);
    expect(points.every(([x, y]) => x >= 0 && x <= 100 && y >= 0 && y <= 20)).toBe(true);
    expect(points[2][1]).toBeLessThan(points[0][1]);
  });

  it("compares this week with last week in words", () => {
    expect(compare(12, 8)).toMatchObject({ direction: "up", diff: 4 });
    expect(compare(3, 8).direction).toBe("down");
    expect(compare(5, 5).direction).toBe("flat");
    expect(compare(5, null).direction).toBe("flat");
  });

  it("writes highlights only for things that happened", () => {
    const quiet = { mine: 0, todosDone: 0, checkins: 0, affinity: 0, activeDays: 0, mood: null };
    expect(insightHighlights(quiet, null, null, 0)).toEqual([]);
    const lines = insightHighlights({ ...quiet, mine: 20, todosDone: 3, activeDays: 5 }, { ...quiet, mine: 10 }, 22, 4);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join("")).toContain(hourLabel(22));
  });
});

describe("心情统计", () => {
  const items = [mood("2026-11-10 09:00", 5), mood("2026-11-09 09:00", 3), mood("2026-11-08 21:00", 1), mood("2026-11-03 09:00", 5)];

  it("counts the share of each score", () => {
    const rows = moodDistribution(items);
    expect(rows.map((row) => row.score)).toEqual([5, 4, 3, 2, 1]);
    expect(rows[0]).toMatchObject({ count: 2, percent: 50 });
    expect(moodDistribution([]).every((row) => row.percent === 0)).toBe(true);
  });

  it("averages per weekday and leaves unlogged days null", () => {
    const byDay = moodByWeekday(items);
    expect(byDay[1]).toBe(5);
    expect(byDay[0]).toBe(3);
    expect(byDay[4]).toBeNull();
  });

  it("counts the streak up to today, or yesterday if today is not logged yet", () => {
    expect(moodStreak(items, TODAY)).toBe(3);
    expect(moodStreak(items, "2026-11-11")).toBe(3);
    expect(moodStreak(items, "2026-11-12")).toBe(0);
  });
});

describe("文本与时间", () => {
  it("splits text around case-insensitive hits", () => {
    expect(highlightParts("Hello hello", "HELLO")).toEqual([{ text: "Hello", hit: true }, { text: " ", hit: false }, { text: "hello", hit: true }]);
    expect(highlightParts("abc", "")).toEqual([{ text: "abc", hit: false }]);
    expect(highlightParts("abc", "x")).toEqual([{ text: "abc", hit: false }]);
  });

  it("formats durations", () => {
    expect(formatDuration(42)).toBe("42 秒");
    expect(formatDuration(125)).toBe("2 分 5 秒");
    expect(formatDuration(3720)).toBe("1 小时 2 分");
    expect(formatDuration(-1)).toBe("—");
    expect(secondsBetween("2026-11-10 09:00:00", "2026-11-10 09:01:30")).toBe(90);
    expect(secondsBetween(null, "2026-11-10 09:00")).toBeNull();
  });

  it("suggests a password that passes the site's own rules", () => {
    let seed = 7;
    const password = suggestPassword((max) => (seed = (seed * 31 + 11) % 997) % max);
    expect(password.length).toBeGreaterThanOrEqual(12);
    expect(password).toMatch(/[a-z]/);
    expect(password).toMatch(/\d/);
    expect(password).toMatch(/[-_.+]/);
  });
});

describe("狼人杀", () => {
  it("shows recent form oldest first", () => {
    expect(recentForm([game({ won: true }), game({ won: false }), game({ won: null, endedAt: null })])).toEqual(["-", "L", "W"]);
  });

  it("filters by faction, result and board together", () => {
    const rows = [game({ gameId: "a" }), game({ gameId: "b", faction: "good", won: false }), game({ gameId: "c", won: null, endedAt: null, board: "禁言骑士" })];
    expect(filterGames(rows, { faction: "wolf", result: "win", board: "" }).map((row) => row.gameId)).toEqual(["a"]);
    expect(filterGames(rows, { faction: "all", result: "open", board: "" }).map((row) => row.gameId)).toEqual(["c"]);
    expect(filterGames(rows, { faction: "all", result: "all", board: "禁言骑士" })).toHaveLength(1);
  });

  it("gives a title that needs enough games first", () => {
    expect(werewolfTitle({ available: true, finished: 2, winRate: 1 })).toBe("新手村民");
    expect(werewolfTitle({ available: true, finished: 10, winRate: 0.6, bestStreak: 5 })).toBe("连胜机器");
    expect(werewolfTitle({ available: true, finished: 10, winRate: 0.6, byFaction: { wolf: { games: 4, wins: 3 } } })).toBe("影帝级狼人");
    expect(werewolfTitle({ available: true, finished: 10, winRate: 0.2 })).toBe("越挫越勇");
  });
});

describe("上手清单", () => {
  it("marks each step from real data", () => {
    const steps = onboardingSteps({ todos: 1, notes: 0, moods: 0, checkedToday: 0, callMe: true, hasFacts: false, levelIndex: 0 });
    expect(steps.filter((step) => step.done).map((step) => step.key)).toEqual(["todo", "call"]);
    expect(onboardingSteps({ todos: 1, notes: 1, moods: 1, checkedToday: 1, callMe: true, hasFacts: true, levelIndex: 2 }).every((step) => step.done)).toBe(true);
  });
});
