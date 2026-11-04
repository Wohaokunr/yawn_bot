import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  setCsrfToken: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

import { MyAffinityPage } from "./MyAffinityPage";
import { MyAssistantPage } from "./MyAssistantPage";
import { MyChatSettingsPage } from "./MyChatSettingsPage";
import { MyChatsPage } from "./MyChatsPage";
import { MyGamesPage } from "./MyGamesPage";
import { MyGroupsPage } from "./MyGroupsPage";
import { MyMemoryPage } from "./MyMemoryPage";
import { MyTasksPage } from "./MyTasksPage";

const ok = (data: unknown, meta: Record<string, unknown> = {}) => Promise.resolve({ data, meta });

const TODO = { id: 7, content: "交作业", priority: "high", priorityLabel: "重要", done: false, due: "2026-11-03", dueInDays: -1, overdue: true, createdAt: "2026-11-01 10:00", doneAt: null };
const TODOS = { items: [TODO, { ...TODO, id: 8, content: "买牛奶", priority: "normal", due: null, dueInDays: null, overdue: false, done: true, doneAt: "2026-11-04 08:00" }], pending: 1, done: 1, limit: 100 };
const CHAT = {
  prefs: { replyLength: "normal", emoji: "some", callMe: "", quietStart: "", quietEnd: "", morningGreeting: "", checkinNudge: "", birthday: "", proactive: true },
  inQuietHours: false,
  agent: { enabled: true, loaded: true, autoReply: true, friendsOnly: true, paused: false, turnsToday: 3, dailyTurns: 200, proactiveAllowed: true },
};
const OFFERS = {
  siteEnabled: true, siteOpen: true, defaultOn: true, on: true, ownChoice: false, everyHours: 3, defaultEveryHours: 3, minEveryHours: 1, pausedUntil: null, dailyMax: 3, siteQuiet: "22:30-09:00",
  kinds: [{ kind: "todo_due", label: "快到期的待办", description: "问要不要帮你拆一下", siteOn: true, mutedUntil: null }],
  history: [{ id: "o1", at: "2026-11-03 15:00", status: "sent", statusLabel: "问了你", about: ["交作业"], outcome: "accepted", outcomeLabel: "你答应了" }],
  stats: { sent: 1, accepted: 1, declined: 0, ignored: 0 },
};
const MEMORY = {
  available: true, enabled: true, shareEnabled: true, tombstoneDays: 90,
  labels: { hobby: "爱好", skill: "擅长" }, listKeys: ["hobby"],
  facts: [
    { key: "hobby", label: "爱好", content: "猫、羽毛球", source: "agent", sourceLabel: "你让我记的", protected: false, updatedAt: "2026-11-01T10:00:00", expiresAt: null },
    { key: "skill", label: "擅长", content: "写代码", source: "manual", sourceLabel: "管理员设置的", protected: true, updatedAt: null, expiresAt: null },
  ],
  groupProfile: [{ key: "hobby", label: "爱好", content: "打游戏", groups: ["猫猫群"], selfTold: false, asOf: "" }],
  settings: { groupsInPrivate: true, mutedGroups: [], shareKeys: [], shareGroups: [] },
  shareableKeys: [{ key: "hobby", label: "爱好" }],
  groups: [{ groupId: "900001", name: "猫猫群", agentReadable: true, inPrivate: true, seesShared: [] }],
  tombstones: [{ what: "爱好", keyword: "游泳", scope: "私聊", until: "2027-01-01 00:00", index: 0 }],
  ledger: [],
  recap: { summary: "聊了周末去哪玩", openThreads: ["电影票"], updatedAt: "2026-11-03 22:00" },
};

function renderAt(path: string): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="me/assistant" element={<MyAssistantPage />} />
          <Route path="me/chat" element={<MyChatSettingsPage />} />
          <Route path="me/memory" element={<MyMemoryPage />} />
          <Route path="me/chats" element={<MyChatsPage />} />
          <Route path="me/affinity" element={<MyAffinityPage />} />
          <Route path="me/tasks" element={<MyTasksPage />} />
          <Route path="me/groups" element={<MyGroupsPage />} />
          <Route path="me/games" element={<MyGamesPage />} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

describe("我的空间 2.0", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class { observe(): void {} unobserve(): void {} disconnect(): void {} });
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    state.api.mockReset();
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (path === "/me/assistant/todos" && method === "GET") return ok(TODOS);
      if (path === "/me/assistant/todos" && method === "POST") return ok({ ...TODOS, items: [{ ...TODO, id: 9, content: "洗衣服", overdue: false, dueInDays: null, due: null }, ...TODOS.items], pending: 2, added: 9 });
      if (path === "/me/assistant/todos/7" && method === "PATCH") return ok({ ...TODOS, items: [{ ...TODO, done: true }, TODOS.items[1]], pending: 0, done: 2 });
      if (path === "/me/assistant/notes") return ok({ items: [{ id: 3, content: "wifi 在冰箱上", tag: "家", pinned: true, at: "2026-11-01 10:00", editedAt: null }], tags: ["家"], limit: 100 });
      if (path === "/me/assistant/moods" && method === "GET") return ok({ items: [], week: { days: 7, count: 0, average: null, averageLabel: null, trend: null }, month: { days: 30, count: 0, average: null, averageLabel: null, trend: null }, scores: {}, limit: 120 });
      if (path === "/me/assistant/moods" && method === "POST") return ok({ items: [{ index: 0, at: "2026-11-04 09:00", score: 5, label: "很好", note: "" }], week: { days: 7, count: 1, average: 5, averageLabel: "很好", trend: "flat" }, month: { days: 30, count: 1, average: 5, averageLabel: "很好", trend: "flat" }, scores: {}, limit: 120 });
      if (path === "/me/chat") return ok(CHAT);
      if (path === "/me/chat/prefs") return ok({ ...CHAT, prefs: { ...CHAT.prefs, callMe: "小猫" } });
      if (path === "/me/chat/pause") return ok({ ...CHAT, agent: { ...CHAT.agent, paused: true } });
      if (path === "/me/offers" && method === "GET") return ok(OFFERS);
      if (path === "/me/offers" && method === "PATCH") return ok({ ...OFFERS, on: false, ownChoice: true });
      if (path === "/me/memory") return ok(MEMORY);
      if (path.startsWith("/me/memory/facts?")) return ok({ ...MEMORY, facts: [MEMORY.facts[1]], result: { removed: ["hobby"], trimmed: [], protected: [], changed: true, blocked: true } });
      if (path === "/me/chats") return ok([{ id: "5", title: null, where: "private", groupId: null, groupName: null, messages: 2, mine: 1, createdAt: null, updatedAt: null, firstAt: "2026-11-04T09:00:00", lastAt: "2026-11-04T09:01:00", preview: "早呀" }]);
      if (path.startsWith("/me/chats/5/messages")) return ok([{ id: "1", role: "user", content: "早", at: "2026-11-04T09:00:00" }, { id: "2", role: "assistant", content: "早呀", at: "2026-11-04T09:01:00" }], { hasMore: false, before: "1" });
      if (path.startsWith("/me/chats/search")) return ok([{ chatId: "5", messageId: "2", role: "assistant", at: "2026-11-04T09:01:00", snippet: "早呀", chatTitle: null, where: "private" }]);
      if (path === "/me/affinity") return ok({ userId: "1", points: 70, level: "熟人", levelIndex: 2, nextLevel: "朋友", toNext: 80, progress: 0.11, today: 2, available: true, levels: [{ floor: 0, name: "陌生" }, { floor: 20, name: "面熟" }, { floor: 60, name: "熟人" }, { floor: 150, name: "朋友" }], sources: { checkin: { label: "签到", dailyCap: 5 } }, breakdown30d: { checkin: 12 }, events: [{ id: 1, group_id: 900001, source: "checkin", source_label: "签到", delta: 2, balance_after: 70, note: "", day: "2026-11-04", created_at: "2026-11-04T08:00:00" }], groups: [{ groupId: "900001", groupName: "猫猫群", groupAffinity: 3, checkedToday: true, streakDays: 4, totalDays: 10, points: 100, exp: 50, lastCheckinDate: "2026-11-04" }], tones: { 熟人: "比较熟了，可以开开玩笑" } });
      if (path === "/me/tasks") return ok({ tasksEnabled: true, taskLimits: { perUser: 2, daily: 10, timeoutSeconds: 600 }, tasks: [{ id: "t3", title: "查天气", goal: "查本周天气", state: "running", status: "进行中", toolsets: [], createdAt: null, startedAt: null, finishedAt: null, progress: [{ at: "2026-11-04 09:00", note: "在查", step: 1, total: 2 }], summary: null, error: null, question: null, files: [], reported: false, held: false }], tasksToday: 1, computer: null, projects: [] });
      if (path === "/me/tasks/t3") return ok({ tasksEnabled: true, taskLimits: { perUser: 2, daily: 10, timeoutSeconds: 600 }, tasks: [], tasksToday: 1, computer: null, projects: [], cancelled: true });
      if (path === "/me/joined-groups") return ok([
        { groupId: "900001", groupName: "猫猫群", role: "admin", managed: true, memberCount: 3, lastActiveAt: null, firstSeenAt: null, agentEnabled: true, featuresOff: [], checkin: { checkedToday: true, streakDays: 4, streakBroken: false, totalDays: 10, points: 1, exp: 1, groupAffinity: 1, lastCheckinDate: null } },
        { groupId: "900002", groupName: "摸鱼群", role: "member", managed: false, memberCount: 9, lastActiveAt: null, firstSeenAt: null, agentEnabled: false, featuresOff: [{ key: "checkin", label: "签到" }], checkin: null },
      ]);
      if (path === "/me/games") return ok({ available: true, games: 3, finished: 2, aborted: 1, wins: 1, winRate: 0.5, survived: 1, sheriff: 1, currentStreak: 1, bestStreak: 1, byFaction: { wolf: { games: 1, wins: 1 }, good: { games: 1, wins: 0 } }, byRole: [{ role: "预言家", games: 1, wins: 0 }], deathCauses: { VOTED: 1 }, recent: [{ gameId: "1", groupId: "900001", groupName: "猫猫群", board: "预女猎白", players: 9, startedAt: "2026-11-01T10:00:00", endedAt: "2026-11-01T10:30:00", role: "狼人", faction: "wolf", seat: 2, sheriff: false, won: true, deathRound: null, deathCause: null, rounds: 3, winner: "wolf" }] });
      return Promise.reject(new Error(`unexpected API: ${method} ${path}`));
    });
  });

  it("adds, completes and filters todos", async () => {
    renderAt("/me/assistant");
    expect(await screen.findByText("交作业")).toBeInTheDocument();
    expect(screen.getByText("逾期 1 天")).toBeInTheDocument();
    expect(screen.queryByText("买牛奶")).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("要做什么？回车记下"), { target: { value: "洗衣服" } });
    fireEvent.click(screen.getByRole("button", { name: /添\s?加/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/todos", expect.objectContaining({ method: "POST", body: JSON.stringify({ content: "洗衣服", priority: "normal", due: null }) })));
    expect(await screen.findByText("洗衣服")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("完成：交作业"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/todos/7", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ done: true }) })));
  });

  it("opens notes and mood tabs from the URL and logs a mood", async () => {
    renderAt("/me/assistant?tab=notes");
    expect(await screen.findByText("wifi 在冰箱上")).toBeInTheDocument();
    expect(screen.getAllByText("#家").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("心情"));
    fireEvent.click(await screen.findByRole("radio", { name: /很好/ }));
    fireEvent.click(screen.getByRole("button", { name: /记\s?下/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/assistant/moods", expect.objectContaining({ method: "POST", body: JSON.stringify({ score: 5, note: null }) })));
  });

  it("saves only changed chat prefs and pauses auto reply", async () => {
    renderAt("/me/chat");
    const input = await screen.findByPlaceholderText("比如：小猫、老板");
    fireEvent.change(input, { target: { value: "小猫" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/chat/prefs", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ callMe: "小猫" }) })));
    expect((await screen.findAllByText("你答应了")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /暂停自动回复/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^暂\s?停$/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/chat/pause", expect.objectContaining({ body: JSON.stringify({ paused: true }) })));
  });

  it("turns offers off as the user's own choice", async () => {
    renderAt("/me/chat");
    await screen.findByText("快到期的待办");
    fireEvent.click(screen.getByText("关"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/offers", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ enabled: false }) })));
  });

  it("shows memory, protects manual facts and forgets with block", async () => {
    renderAt("/me/memory");
    expect(await screen.findByText("猫、羽毛球")).toBeInTheDocument();
    expect(screen.getByText("聊了周末去哪玩")).toBeInTheDocument();
    expect(screen.getByText("管理员设置")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^eye-invisible 忘\s?掉$/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^忘\s?掉$/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/memory/facts?key=hobby&keyword=&block=true", expect.objectContaining({ method: "DELETE" })));
    fireEvent.click(screen.getByText(/群里的我/));
    expect(await screen.findByText("打游戏")).toBeInTheDocument();
  });

  it("reads a chat with day separators and searches", async () => {
    renderAt("/me/chats");
    fireEvent.click(await screen.findByText("早呀"));
    expect(await screen.findByText("2026-11-04")).toBeInTheDocument();
    expect(screen.getByText("早")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /导\s?出/ })).toHaveAttribute("href", "/webui/api/v1/me/chats/5/export");
    const search = screen.getByPlaceholderText("搜索聊天内容");
    fireEvent.change(search, { target: { value: "早" } });
    fireEvent.keyDown(search, { key: "Enter", code: "Enter", keyCode: 13 });
    expect(await screen.findByText(/找到 1 条/)).toBeInTheDocument();
  });

  it("renders affinity level, sources and check-ins", async () => {
    renderAt("/me/affinity");
    expect((await screen.findAllByText("熟人")).length).toBeGreaterThan(0);
    expect(screen.getByText("比较熟了，可以开开玩笑")).toBeInTheDocument();
    expect(screen.getByText("再 80 点升到「朋友」 · 今天 +2")).toBeInTheDocument();
    expect(screen.getByText("连签 4")).toBeInTheDocument();
  });

  it("cancels a running helper task", async () => {
    renderAt("/me/tasks");
    expect(await screen.findByText("查天气")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^stop 取\s?消$/ }));
    fireEvent.click(await screen.findByRole("button", { name: "取消任务" }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/tasks/t3", expect.objectContaining({ method: "DELETE" })));
  });

  it("lists joined groups and links only managed ones", async () => {
    renderAt("/me/groups");
    expect(await screen.findByText("猫猫群")).toBeInTheDocument();
    expect(screen.getByText("猫猫群").closest("a")).toHaveAttribute("href", "/me/groups/900001");
    expect(screen.getByText("摸鱼群").closest("a")).toBeNull();
    expect(screen.getByText("1 项功能对你关着")).toBeInTheDocument();
    fireEvent.click(screen.getByText("我管理的 1"));
    expect(screen.queryByText("摸鱼群")).not.toBeInTheDocument();
  });

  it("renders werewolf stats", async () => {
    renderAt("/me/games");
    expect(await screen.findByText("50%")).toBeInTheDocument();
    expect(screen.getByText("狼人阵营")).toBeInTheDocument();
    expect(screen.getByText("被投出 1")).toBeInTheDocument();
  });
});
