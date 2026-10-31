import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  getCsrfToken: () => "csrf",
  ApiError: class ApiError extends Error {},
}));

import { GroupDetailPage } from "./GroupDetailPage";
import { GroupsPage } from "./GroupsPage";
import { UsersPage } from "./UsersPage";

const NOW = new Date().toISOString();
const features = [
  { key: "checkin", name: "签到", override: null, effective: true, source: "default" },
  { key: "fanqie", name: "番茄小说", override: false, effective: false, source: "group" },
  { key: "group_agent", name: "群聊 Agent", override: false, effective: false, source: "agent_config" },
];
const group = {
  groupId: "100",
  groupName: "猫猫群",
  firstSeenAt: NOW,
  lastActiveAt: NOW,
  memberCount: 3,
  activeMembers7d: 2,
  agentEnabled: true,
  personaEnabled: true,
  lastAgentAt: NOW,
  overrideCount: 1,
  disabledFeatures: ["fanqie"],
  memberOverrideCount: 2,
  checkinsToday: 1,
};
const member = {
  userId: "11",
  nickname: "小明",
  groupNickname: "群主小明",
  role: "owner",
  title: "大佬",
  firstSeenAt: NOW,
  lastSeenAt: NOW,
  active: true,
  groupAffinity: 30,
  streakDays: 5,
  totalDays: 9,
  checkedToday: true,
  overrideCount: 1,
};
const profile = {
  userId: "11",
  nickname: "小明",
  firstInteractionAt: NOW,
  lastInteractionAt: NOW,
  affinity: 120,
  affinityLevel: "熟悉",
  level: { name: "熟悉", nextLevel: "亲近", toNext: 30, progress: 0.5 },
  groupCount: 1,
  checkedTodayCount: 1,
  privateOverrides: 0,
  memberOverrides: 1,
  memberships: [{ groupId: "100", groupName: "猫猫群", groupNickname: "群主小明", role: "owner", title: null, active: true, firstSeenAt: NOW, lastSeenAt: NOW, groupAffinity: 30, streakDays: 5, totalDays: 9, checkedToday: true, overrideCount: 1, agentEnabled: true }],
};

function envelope(data: unknown, total?: number) {
  return Promise.resolve({ data, meta: total === undefined ? {} : { total } });
}

function route(path: string, init?: RequestInit) {
  const method = init?.method ?? "GET";
  const url = new URL(path, "http://x");
  if (url.pathname === "/groups/summary") return envelope({ total: 1, agentEnabled: 1, active24h: 1, active7d: 1, withOverrides: 1, checkinsToday: 1, members: 3 });
  if (url.pathname === "/groups" && method === "GET") return envelope([group], 1);
  if (url.pathname === "/groups/batch/features") return envelope({ updated: 1, feature: "rpg" });
  if (url.pathname === "/groups/100" && method === "GET") return envelope({ ...group, features });
  if (url.pathname === "/groups/100/overview") {
    return envelope({
      groupId: "100",
      roles: { owner: 1, member: 2 },
      activeMembers: { "24h": 1, "7d": 2, "30d": 3 },
      checkins: { today: 2, yesterday: 1, streakTop: [{ userId: "11", name: "群主小明", streakDays: 5, totalDays: 9, checkedToday: true }] },
      affinityTop: [],
      recentMembers: [],
      agent: { configured: true, enabled: true, personaEnabled: true, proactiveEnabled: true, dailyLimit: 30, lastAgentAt: NOW },
      overrides: { group: 1, member: 2, membersWithOverrides: 1 },
    });
  }
  if (url.pathname === "/groups/100/overrides") {
    return envelope([
      { userId: "11", name: "群主小明", role: "owner", feature: "rpg", featureName: "跑团", enabled: false, effective: false, groupEffective: true, blockedByGroup: false, redundant: false },
      { userId: "11", name: "群主小明", role: "owner", feature: "fanqie", featureName: "番茄小说", enabled: false, effective: false, groupEffective: false, blockedByGroup: false, redundant: true },
    ]);
  }
  if (url.pathname === "/groups/100/members") return envelope([member], 1);
  if (url.pathname.startsWith("/groups/100/members/11/features")) return envelope(method === "GET" ? features : features[0]);
  if (url.pathname.startsWith("/groups/100/features/")) return envelope(features[0]);
  if (url.pathname === "/users/summary") return envelope({ total: 1, active24h: 1, active7d: 1, withOverrides: 0, withMemberOverrides: 1, checkinsToday: 1, privateAgentPaused: 0 });
  if (url.pathname === "/users") return envelope([{ userId: "11", nickname: "小明", firstInteractionAt: NOW, lastInteractionAt: NOW, affinity: 120, affinityLevel: "熟悉", groupCount: 1, overrideCount: 0, memberOverrideCount: 1 }], 1);
  if (url.pathname === "/users/11") return envelope(profile);
  return envelope([]);
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}</div>;
}

function renderAt(path: string) {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/groups" element={<GroupsPage />} />
          <Route path="/groups/:groupId" element={<GroupDetailPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="*" element={<div>other</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </AntApp>,
  );
}

function calls(prefix: string, method = "GET") {
  return state.api.mock.calls.filter(([path, init]) => String(path).startsWith(prefix) && (init?.method ?? "GET") === method);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  state.api.mockReset();
  state.api.mockImplementation(route);
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: true,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

describe("群组列表", () => {
  it("显示状态条与统计，点状态条 chip 套用筛选并写进 URL", async () => {
    renderAt("/groups");
    expect(await screen.findByText("猫猫群")).toBeInTheDocument();
    expect(screen.getByText("1 个群 · 3 个成员身份")).toBeInTheDocument();
    expect(screen.getByText("群 1")).toBeInTheDocument();
    expect(screen.getByText("成员 2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Agent 关闭 0"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("groups.agent=off"));
    await waitFor(() => expect(calls("/groups?").some(([path]) => String(path).includes("agent=off"))).toBe(true));
  });

  it("搜索不用回车，停顿后自动查询", async () => {
    renderAt("/groups");
    await screen.findByText("猫猫群");
    fireEvent.change(screen.getByLabelText("搜群名或群号"), { target: { value: "猫" } });
    await waitFor(() => expect(calls("/groups?").some(([path]) => String(path).includes("search=%E7%8C%AB"))).toBe(true), { timeout: 2000 });
    expect(screen.getByTestId("location").textContent).toContain("groups.q=");
  });

  it("从 URL 恢复筛选", async () => {
    renderAt("/groups?groups.sort=members&groups.overrides=any");
    await screen.findByText("猫猫群");
    expect(calls("/groups?").some(([path]) => String(path).includes("sort=members") && String(path).includes("overrides=any"))).toBe(true);
    expect(screen.getByText("清除 1 个筛选")).toBeInTheDocument();
  });

  it("点整行进入群概览", async () => {
    renderAt("/groups");
    const name = await screen.findByText("1 个群 · 3 个成员身份");
    expect(name).toBeInTheDocument();
    const row = screen.getByLabelText("打开 猫猫群");
    fireEvent.click(row);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/groups/100"));
  });
});

describe("群详情", () => {
  it("概览显示签到对比与覆盖入口，标签进 URL", async () => {
    renderAt("/groups/100");
    expect(await screen.findByText("比昨天多 1（昨天 1）")).toBeInTheDocument();
    fireEvent.click(screen.getByText("群 1 · 1 位成员 2 条"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("group.tab=overrides"));
    expect(await screen.findByText("清理多余设置（1）")).toBeInTheDocument();
  });

  it("群功能：来源翻译，继承选项写出继承后的值，改设置调 PATCH", async () => {
    renderAt("/groups/100?group.tab=features");
    expect(await screen.findByText("Agent 总开关")).toBeInTheDocument();
    expect(screen.getAllByText("继承·开").length).toBeGreaterThan(0);
    const row = screen.getByText("签到").closest("tr")!;
    fireEvent.click(within(row).getByText("关"));
    await waitFor(() => expect(calls("/groups/100/features/checkin", "PATCH")).toHaveLength(1));
    expect(JSON.parse(String(calls("/groups/100/features/checkin", "PATCH")[0][1].body))).toEqual({ override: false });
  });

  it("成员：角色中文、签到列，点行打开档案并默认本群标签", async () => {
    renderAt("/groups/100?group.tab=members");
    expect(await screen.findByText("连签 5 天")).toBeInTheDocument();
    expect(screen.getAllByText("群主").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByLabelText("打开 群主小明 的档案"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("user=11"));
    expect(await screen.findByText("全部跟随群设置")).toBeInTheDocument();
    expect(calls("/groups/100/members/11/features").length).toBeGreaterThan(0);
  });
});

describe("用户页", () => {
  it("标题改为用户并说明私聊开关只管私聊；URL 打开档案", async () => {
    renderAt("/users?user=11&user.tab=groups");
    expect(await screen.findByRole("heading", { name: "用户" })).toBeInTheDocument();
    expect(screen.getByText(/私聊功能开关只管私聊/)).toBeInTheDocument();
    expect(await screen.findByText("今日签到 1 个群")).toBeInTheDocument();
    expect(screen.getByText("群名片 群主小明 ·", { exact: false })).toBeInTheDocument();
  });
});
