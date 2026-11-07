import { describe, expect, it } from "vitest";
import type { ChatMessageRow, NoteItem, TodoItem } from "./me-space-api";
import { countdownLabel, dueLabel, filterNotes, filterTodos, formatBytes, greeting, groupByDay, isBirthday, isClock, moodByDay, pickTab, quietLabel, todoChanges, trendLabel } from "./me-space-meta";

const todo = (patch: Partial<TodoItem>): TodoItem => ({ id: 1, content: "x", priority: "normal", priorityLabel: "普通", done: false, due: null, dueInDays: null, overdue: false, createdAt: null, doneAt: null, ...patch });

describe("我的空间纯逻辑", () => {
  it("greets by hour", () => {
    expect(greeting(3)).toBe("夜深了");
    expect(greeting(8)).toBe("早上好");
    expect(greeting(12)).toBe("中午好");
    expect(greeting(15)).toBe("下午好");
    expect(greeting(21)).toBe("晚上好");
  });

  it("labels due dates and countdowns", () => {
    expect(dueLabel(null)).toBeNull();
    expect(dueLabel(-2)).toBe("逾期 2 天");
    expect(dueLabel(0)).toBe("今天到期");
    expect(dueLabel(1)).toBe("明天到期");
    expect(dueLabel(5)).toBe("还有 5 天");
    expect(countdownLabel(0)).toBe("就是今天");
    expect(countdownLabel(3)).toBe("还有 3 天");
    expect(countdownLabel(-4)).toBe("已过去 4 天");
  });

  it("diffs a todo edit and refuses blank content or a half-typed date", () => {
    const item = todo({ content: "交作业", priority: "high", due: "2026-11-07" });
    expect(todoChanges(item, { content: " 交作业 ", priority: "high", due: "2026-11-07" })).toEqual({});
    expect(todoChanges(item, { content: "交作业", priority: "low", due: "" })).toEqual({ priority: "low", due: "" });
    expect(todoChanges(item, { content: "  ", priority: "high", due: "2026-11-07" })).toBeNull();
    expect(todoChanges(item, { content: "交作业", priority: "high", due: "2026-11" })).toBeNull();
  });

  it("filters todos", () => {
    const items = [todo({ id: 1, content: "交作业", dueInDays: -1, overdue: true }), todo({ id: 2, content: "今天", dueInDays: 0 }), todo({ id: 3, content: "买菜", done: true })];
    expect(filterTodos(items, "open").map((item) => item.id)).toEqual([1, 2]);
    expect(filterTodos(items, "today").map((item) => item.id)).toEqual([1, 2]);
    expect(filterTodos(items, "overdue").map((item) => item.id)).toEqual([1]);
    expect(filterTodos(items, "done").map((item) => item.id)).toEqual([3]);
    expect(filterTodos(items, "all", "作业").map((item) => item.id)).toEqual([1]);
  });

  it("filters notes by keyword and tag", () => {
    const notes: NoteItem[] = [
      { id: 1, content: "WiFi 密码", tag: "家", pinned: false, at: null, editedAt: null },
      { id: 2, content: "单词", tag: "学习", pinned: false, at: null, editedAt: null },
    ];
    expect(filterNotes(notes, "wifi", null).map((n) => n.id)).toEqual([1]);
    expect(filterNotes(notes, "", "学习").map((n) => n.id)).toEqual([2]);
    expect(filterNotes(notes, "家", null).map((n) => n.id)).toEqual([1]);
  });

  it("averages moods per day over a window", () => {
    const days = moodByDay([{ at: "2026-11-04 09:00", score: 5 }, { at: "2026-11-04 21:00", score: 2 }, { at: "2026-11-02 10:00", score: 3 }], "2026-11-04", 3);
    expect(days).toEqual([{ day: "2026-11-02", score: 3 }, { day: "2026-11-03", score: null }, { day: "2026-11-04", score: 3.5 }]);
    expect(trendLabel("up")).toBe("在变好");
    expect(trendLabel(null)).toBe("记录还不够");
  });

  it("validates clock and birthday like the backend", () => {
    expect(isClock("7:30")).toBe(true);
    expect(isClock("23：59")).toBe(true);
    expect(isClock("24:00")).toBe(false);
    expect(isBirthday("03-14")).toBe(true);
    expect(isBirthday("3/4")).toBe(true);
    expect(isBirthday("13-01")).toBe(false);
    expect(quietLabel("23:00", "08:00")).toBe("23:00 到次日 08:00");
    expect(quietLabel("13:00", "14:00")).toBe("13:00 到 14:00");
    expect(quietLabel("", "")).toBe("没有设置");
  });

  it("groups chat messages by day", () => {
    const rows: ChatMessageRow[] = [
      { id: "1", role: "user", content: "a", at: "2026-11-03T23:59:00" },
      { id: "2", role: "assistant", content: "b", at: "2026-11-04T00:01:00" },
      { id: "3", role: "user", content: "c", at: "2026-11-04T00:02:00" },
    ];
    expect(groupByDay(rows).map((group) => [group.day, group.rows.length])).toEqual([["2026-11-03", 1], ["2026-11-04", 2]]);
  });

  it("formats bytes and picks tabs", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(pickTab("notes", ["todos", "notes"] as const, "todos")).toBe("notes");
    expect(pickTab("evil", ["todos", "notes"] as const, "todos")).toBe("todos");
    expect(pickTab(null, ["todos"] as const, "todos")).toBe("todos");
  });
});
