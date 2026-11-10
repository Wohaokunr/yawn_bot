import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  setCsrfToken: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

import { resetHubStores } from "./hub-ui";
import { MeHomePage, resolveBlockOrder } from "./MeHomePage";
import { MeHeaderTools } from "./MeShellExtras";
import { MyAchievementsPage } from "./MyAchievementsPage";
import { MyCalendarPage } from "./MyCalendarPage";
import { MyInsightsPage } from "./MyInsightsPage";
import { MySearchPage } from "./MySearchPage";

const ok = (data: unknown, meta: Record<string, unknown> = {}) => Promise.resolve({ data, meta });

const UI_PREFS = { homeHidden: [], homeOrder: [], landing: "/me", pinnedChats: [], starred: [], onboardingDismissed: false, seenAchievements: [], seenNotices: [], homeBlocks: ["notices", "onboarding", "today", "quests", "week", "groups", "more"], landings: ["/me", "/me/assistant"] };
const INBOX = {
  now: "2026-11-04 09:00",
  items: [
    { id: "todo_overdue", kind: "todo_overdue", tone: "danger", title: "2 条待办逾期了", detail: "交作业、还书", to: "/me/assistant", count: 2 },
    { id: "quest_claim", kind: "quest_claim", tone: "success", title: "有 1 个任务奖励可以领", detail: "", to: "/me/affinity?tab=play", count: 1 },
  ],
  total: 2,
  badges: { "/me/assistant": 2, "/me/affinity": 1 },
};
const DASHBOARD = {
  now: "2026-11-04 09:00",
  todos: { pending: 1, overdue: 0, dueToday: 1, top: [{ id: 1, content: "交作业", priority: "high", priorityLabel: "重要", done: false, due: "2026-11-04", dueInDays: 0, overdue: false, createdAt: null, doneAt: null }] },
  reminders: [],
  countdowns: [],
  mood: { last: null, week: { days: 7, count: 0, average: null, averageLabel: null, trend: null }, loggedToday: false },
  affinity: { available: true, level: "熟人", nextLevel: "朋友", progress: 0.4, today: 2 },
  checkin: { groups: 2, checkedToday: 1, bestStreak: 5, pendingGroups: [{ groupId: "900002", groupName: "摸鱼群", streakDays: 0 }] },
  chat: { enabled: true, loaded: true, paused: false, turnsToday: 3, dailyTurns: 200 },
  tasks: { running: 0, waitingYou: 0, total: 0 },
  counts: { notes: 0, todos: 1, reminders: 0, countdowns: 0, moods: 0 },
};
const WINDOW = { mine: 12, todosDone: 3, checkins: 4, affinity: 9, activeDays: 5, mood: 4 };
const INSIGHTS = {
  days: 30,
  today: "2026-11-04",
  series: Array.from({ length: 30 }, (_value, index) => ({ day: `2026-10-${String(index + 6).padStart(2, "0")}`.replace(/2026-10-(3[2-9])/, (_all, day: string) => `2026-11-${String(Number(day) - 31).padStart(2, "0")}`), mine: index % 5, bot: index % 5, todosAdded: 0, todosDone: index % 2, mood: index % 3 === 0 ? 4 : null, checkins: index % 2, affinity: 1 })),
  hours: Array.from({ length: 24 }, (_value, hour) => (hour === 22 ? 9 : 1)),
  weekdays: [3, 4, 5, 6, 7, 8, 9],
  totals: { ...WINDOW, mine: 60, activeDays: 20 },
  thisWeek: WINDOW,
  lastWeek: { ...WINDOW, mine: 6 },
  busiestHour: 22,
  bestDay: { day: "2026-11-01", mine: 9 },
  activeStreak: 6,
  ranks: [{ groupId: "900001", groupName: "猫猫群", members: 30, groupAffinity: 40, affinityRank: 2, points: 300, pointsRank: 5, streakDays: 4, totalDays: 20 }],
};
const DAY = (date: string, patch: object = {}) => ({ date, todos: [], done: 0, reminders: [], countdowns: [], mood: null, moodCount: 0, checkins: 0, affinity: 0, ...patch });
const CALENDAR = {
  month: "2026-11",
  today: "2026-11-04",
  firstWeekday: 6,
  days: Array.from({ length: 30 }, (_value, index) => {
    const date = `2026-11-${String(index + 1).padStart(2, "0")}`;
    if (index === 3) return DAY(date, { todos: [{ id: 7, content: "交作业", done: false, priority: "high" }], reminders: [{ ref: "p1", content: "喝水", time: "10:00", repeat: true }], mood: 4, moodCount: 1, checkins: 2, affinity: 3 });
    if (index === 10) return DAY(date, { countdowns: [{ id: 2, title: "双十一" }] });
    return DAY(date);
  }),
  totals: { todos: 1, done: 0, reminders: 1, countdowns: 1, moodDays: 1, checkinDays: 1, affinity: 3 },
};
const ACH = (key: string, title: string, done: boolean, value = 1, target = 7) => ({ key, title, description: `${title}的说明`, icon: "🔥", value, target, done, unlockedAt: done ? "2026-11-01" : null, progress: done ? 1 : value / target });
const ACHIEVEMENTS = {
  total: 3,
  unlocked: 1,
  fresh: [],
  groups: [{ label: "签到", unlocked: 1, items: [ACH("first_checkin", "初来乍到", true), ACH("streak_7", "七日之约", false, 4, 7)] }, { label: "小本本", unlocked: 0, items: [ACH("todo_10", "行动派", false, 2, 10)] }],
  next: [ACH("streak_7", "七日之约", false, 4, 7)],
  metrics: {},
};
const SEARCH = {
  q: "作业",
  total: 2,
  groups: [
    { kind: "todos", label: "待办", total: 1, items: [{ id: "todo-7", title: "交作业", sub: "今天到期", to: "/me/assistant?find=7" }] },
    { kind: "chats", label: "聊天记录", total: 1, items: [{ id: "msg-9", title: "作业写完了吗", sub: "Bot · 11-03", to: "/me/chats?chat=5&at=9&q=作业" }] },
  ],
};

function Where(): React.JSX.Element {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}{location.search}</output>;
}

function renderAt(path: string, extra?: React.ReactNode): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        {extra}
        <Where />
        <Routes>
          <Route path="me" element={<MeHomePage />} />
          <Route path="me/search" element={<MySearchPage />} />
          <Route path="me/calendar" element={<MyCalendarPage />} />
          <Route path="me/achievements" element={<MyAchievementsPage />} />
          <Route path="me/insights" element={<MyInsightsPage />} />
          <Route path="*" element={<div>别的页面</div>} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

describe("我的空间 3.0", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class { observe(): void {} unobserve(): void {} disconnect(): void {} });
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    window.localStorage.clear();
    resetHubStores();
    state.api.mockReset();
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (path === "/me/ui-prefs" && method === "GET") return ok(UI_PREFS);
      if (path === "/me/ui-prefs" && method === "PUT") return ok({ ...UI_PREFS, ...(JSON.parse(String(init?.body)) as object) });
      if (path === "/me/inbox") return ok(INBOX);
      if (path === "/me") return ok({ userId: "10001", nickname: "小明", account: null, managedGroupCount: 0, groupCount: 2, persona: null, sessionExpiresAt: 0, isSuperuser: false });
      if (path === "/me/groups") return ok([]);
      if (path === "/me/dashboard") return ok(DASHBOARD);
      if (path === "/me/chat") return ok({ prefs: { callMe: "" } });
      if (path === "/me/affinity/perks") return Promise.reject(new Error("好感玩法没开"));
      if (path.startsWith("/me/insights")) return ok({ ...INSIGHTS, days: Number(new URLSearchParams(path.split("?")[1]).get("days") ?? 30) });
      if (path === "/me/checkin" && method === "POST") return ok({ results: [], signed: 1, already: 1, disabled: 0, reward: 10, affinityGain: 2, levelUp: null, quests: { claimed: [], affinity: 0, bonus_item: null } });
      if (path === "/me/assistant/todos" && method === "POST") return ok({ items: [], pending: 2, done: 0, limit: 100 });
      if (path === "/me/assistant/todos/1" && method === "PATCH") return ok({ items: [], pending: 0, done: 1, limit: 100 });
      if (path === "/me/assistant/todos/7" && method === "PATCH") return ok({ items: [], pending: 0, done: 1, limit: 100 });
      if (path === "/me/assistant/notes" && method === "POST") return ok({ items: [], tags: [], limit: 100 });
      if (path.startsWith("/me/calendar")) return ok(path.includes("month=2026-12") ? { ...CALENDAR, month: "2026-12", firstWeekday: 1, days: [DAY("2026-12-01")] } : CALENDAR);
      if (path === "/me/achievements") return ok(ACHIEVEMENTS);
      if (path.startsWith("/me/search")) return ok(SEARCH);
      return Promise.reject(new Error(`unexpected API: ${method} ${path}`));
    });
  });

  it("keeps a saved block order and appends blocks the user never arranged", () => {
    expect(resolveBlockOrder(undefined)).toEqual(["notices", "onboarding", "today", "quests", "week", "groups", "more"]);
    expect(resolveBlockOrder(["more", "bogus", "today"]).slice(0, 3)).toEqual(["more", "today", "notices"]);
  });

  it("shows urgent notices, the onboarding list and the week strip on the home page", async () => {
    renderAt("/me");
    expect(await screen.findByText("2 条待办逾期了")).toBeInTheDocument();
    expect(screen.queryByText("有 1 个任务奖励可以领")).not.toBeInTheDocument();
    expect(await screen.findByText(/上手清单 2\/6/)).toBeInTheDocument();
    expect(await screen.findByText("这七天")).toBeInTheDocument();
    expect(screen.getByText("12 句")).toBeInTheDocument();
  });

  it("signs in, finishes a todo and adds one without leaving the home page", async () => {
    renderAt("/me");
    fireEvent.click(await screen.findByRole("button", { name: "一键签到" }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/checkin", expect.objectContaining({ method: "POST" })));
    fireEvent.click(screen.getByLabelText("完成：交作业"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/todos/1", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ done: true }) })));
    const add = screen.getByLabelText("快速加一条待办");
    fireEvent.change(add, { target: { value: "买牛奶 !重要" } });
    fireEvent.keyDown(add, { key: "Enter", code: "Enter", keyCode: 13 });
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/todos", expect.objectContaining({ method: "POST", body: JSON.stringify({ content: "买牛奶", priority: "high", due: null }) })));
  });

  it("hides a home block and stars an entry, saving both to the account", async () => {
    renderAt("/me");
    await screen.findByText("这七天");
    fireEvent.click(screen.getByRole("button", { name: "自定义主页" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByLabelText("显示这七天"));
    fireEvent.click(within(dialog).getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/ui-prefs", expect.objectContaining({ method: "PUT", body: expect.stringContaining('"homeHidden":["week"]') })));
    await waitFor(() => expect(screen.queryByLabelText("最近七天的活跃情况")).not.toBeInTheDocument());
    fireEvent.click(screen.getByLabelText("设为常用：成就"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/ui-prefs", expect.objectContaining({ method: "PUT", body: JSON.stringify({ starred: ["/me/achievements"] }) })));
  });

  it("dismisses the onboarding list for good", async () => {
    renderAt("/me");
    fireEvent.click(await screen.findByRole("button", { name: "不再显示上手清单" }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/ui-prefs", expect.objectContaining({ method: "PUT", body: JSON.stringify({ onboardingDismissed: true }) })));
    await waitFor(() => expect(screen.queryByText(/上手清单/)).not.toBeInTheDocument());
  });

  it("searches as you type and links hits to the exact item", async () => {
    renderAt("/me/search");
    fireEvent.change(screen.getByLabelText("搜索我的东西"), { target: { value: "作业" } });
    expect(await screen.findByText("待办 · 1")).toBeInTheDocument();
    expect(state.api).toHaveBeenCalledWith("/me/search?q=%E4%BD%9C%E4%B8%9A");
    expect(screen.getAllByText("作业", { selector: "mark" })[0].closest("a")).toHaveAttribute("href", "/me/assistant?find=7");
    fireEvent.click(screen.getByText("聊天记录 1"));
    expect(screen.queryByText("待办 · 1")).not.toBeInTheDocument();
    expect(screen.getByText("写完了吗").closest("a")?.getAttribute("href")).toContain("/me/chats?chat=5&at=9");
    expect(JSON.parse(window.localStorage.getItem("yawnbot.me.search.recent") ?? "[]")).toEqual(["作业"]);
  });

  it("lays out a month, opens a day and completes its todo", async () => {
    renderAt("/me/calendar");
    expect(await screen.findByText("2026 年 11 月")).toBeInTheDocument();
    expect(screen.getAllByRole("gridcell")).toHaveLength(30);
    const today = screen.getByRole("gridcell", { name: "2026-11-04，2 件事" });
    expect(today).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("签了 2 个群")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("完成：交作业"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/todos/7", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ done: true }) })));
    fireEvent.click(screen.getByRole("gridcell", { name: "2026-11-11，1 件事" }));
    expect(await screen.findByText("11 月 11 日 周三")).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("day=2026-11-11");
    fireEvent.click(screen.getByRole("button", { name: "下个月" }));
    expect(await screen.findByText("2026 年 12 月")).toBeInTheDocument();
    expect(state.api).toHaveBeenCalledWith("/me/calendar?month=2026-12");
  });

  it("hides a calendar layer without refetching", async () => {
    renderAt("/me/calendar");
    await screen.findByText("2026 年 11 月");
    const grid = screen.getByRole("grid");
    expect(within(grid).getByText("双十一")).toBeInTheDocument();
    const calls = state.api.mock.calls.length;
    fireEvent.click(screen.getByText("倒数日", { selector: ".ant-tag-checkable, .ant-tag-checkable *" }));
    expect(within(grid).queryByText("双十一")).not.toBeInTheDocument();
    expect(state.api.mock.calls.length).toBe(calls);
  });

  it("filters achievements and remembers which ones were seen", async () => {
    renderAt("/me/achievements");
    expect(await screen.findByText("已达成 1 个")).toBeInTheDocument();
    expect(screen.getAllByText("七日之约 4/7").length).toBeGreaterThan(0);
    expect(screen.getByText("新")).toBeInTheDocument();
    fireEvent.click(screen.getByText("还没达成 2"));
    expect(screen.queryByText("初来乍到")).not.toBeInTheDocument();
    expect(screen.getByText("行动派")).toBeInTheDocument();
  });

  it("switches the insight range through the URL and never names other members", async () => {
    renderAt("/me/insights");
    expect(await screen.findByText("群好感 第 2 名")).toBeInTheDocument();
    expect(screen.getByText("30 人 · 累计签到 20 天 · 连签 4")).toBeInTheDocument();
    expect(screen.getByText(/最长连续 6 天/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("最近 90 天"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/insights?days=90"));
    expect(screen.getByTestId("where")).toHaveTextContent("days=90");
  });

  it("opens quick capture with N, saves a note and tells other pages", async () => {
    const changed = vi.fn();
    window.addEventListener("yawnbot-me-changed", changed);
    renderAt("/me/somewhere", <MeHeaderTools onNavigate={() => undefined} />);
    fireEvent.keyDown(window, { key: "n" });
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("笔记"));
    fireEvent.change(within(dialog).getByLabelText("内容"), { target: { value: "wifi 密码在冰箱上" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/notes", expect.objectContaining({ method: "POST" })));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    window.removeEventListener("yawnbot-me-changed", changed);
  });

  it("does not steal keys while typing and jumps with G then a letter", async () => {
    const go = vi.fn();
    renderAt("/me/somewhere", <><MeHeaderTools onNavigate={go} /><input aria-label="别的输入框" /></>);
    await act(async () => { await Promise.resolve(); });
    fireEvent.keyDown(screen.getByLabelText("别的输入框"), { key: "n" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "g" });
    fireEvent.keyDown(window, { key: "c" });
    expect(go).toHaveBeenCalledWith("/me/calendar");
    fireEvent.keyDown(window, { key: "/" });
    expect(go).toHaveBeenCalledWith("/me/search");
  });

  it("lists what needs attention in the bell and marks them seen", async () => {
    const go = vi.fn();
    renderAt("/me/somewhere", <MeHeaderTools onNavigate={go} />);
    const bell = await screen.findByRole("button", { name: "提醒，1 条没看" });
    fireEvent.click(bell);
    expect(await screen.findByText("有 1 个任务奖励可以领")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /都知道了/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/ui-prefs", expect.objectContaining({ method: "PUT", body: JSON.stringify({ seenNotices: ["todo_overdue@2026-11-04", "quest_claim@2026-11-04"] }) })));
    fireEvent.click(screen.getByText("2 条待办逾期了"));
    expect(go).toHaveBeenCalledWith("/me/assistant");
  });
});
