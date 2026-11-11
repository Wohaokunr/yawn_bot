import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  setCsrfToken: vi.fn(),
  ApiError: class ApiError extends Error {},
}));

import { AffinityAdminPage } from "./AffinityAdminPage";
import { MyAffinityPlay } from "./AffinityPlayPanel";
// 夹具由后端 perks_payload / policy_meta 直接导出，形状和接口一致
import PERKS from "./fixtures/perks-state.json";
import POLICY from "./fixtures/policy.json";
import type { AffinityStats, PerksState, PolicyPayload } from "./types";

const ok = (data: unknown) => Promise.resolve({ data, meta: {} });
const perks = PERKS as unknown as PerksState;
const policy = POLICY as unknown as PolicyPayload;

const STATS: AffinityStats = {
  available: true, enabled: true, users: 12, withAffinity: 8,
  distribution: policy.meta.levels.map((row, i) => ({ level: row.name, count: 6 - i })),
  sources30d: [{ source: "checkin", label: "签到", total: 120 }, { source: "decay", label: "长期不来往", total: -4 }],
  daily: [{ day: "2026-11-08", gained: 30, users: 5 }, { day: "2026-11-09", gained: 12, users: 3 }],
  top: [{ userId: "10001", nickname: "小明", level: "熟人", points: 70 }],
  gifts: [{ key: "milk_tea", name: "奶茶", count: 3 }],
  items: [{ key: "shield", name: "好感护盾", count: 2 }],
  nicknames: 1, questsDoneToday: 4, activeShields: 1, favoriteToday: "蛋糕", moodToday: "元气满满",
  bond: {
    badges: [{ key: "week_streak", name: "一周之约", emoji: "🔥", count: 3 }],
    moments30d: { up: 5, down: 1 },
    streakTop: [{ userId: "10001", nickname: "小明", days: 12 }],
    fortunesToday: 2, lettersSent: 1, weeklyDoneThisWeek: 4,
  },
};

function renderAt(path: string, element: React.ReactNode): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="*" element={element} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

beforeEach(() => {
  state.api.mockReset();
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
});

describe("好感度管理页", () => {
  it("概览显示统计，可以切到等级权益并保存整份策略", async () => {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/affinity/stats") return ok(STATS);
      if (path === "/affinity/policy" && !init) return ok(policy);
      if (path === "/affinity/policy" && init?.method === "PUT") return ok({ ...policy, policy: JSON.parse(String(init.body)).policy });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderAt("/affinity", <AffinityAdminPage />);
    expect(await screen.findByText("有好感的人")).toBeInTheDocument();
    expect(screen.getByText("和 Bot 最熟的人")).toBeInTheDocument();
    expect(screen.getAllByText("小明").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("tab", { name: /等级权益/ }));
    expect((await screen.findAllByText("挚友")).length).toBeGreaterThan(0);
    const save = screen.getByRole("button", { name: /保\s*存/ });
    expect(save).toBeDisabled();
    // 改一处数字：保存按钮亮起
    const inputs = screen.getAllByRole("spinbutton");
    fireEvent.change(inputs[0], { target: { value: "7" } });
    fireEvent.blur(inputs[0]);
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/affinity/policy", expect.objectContaining({ method: "PUT" })));
    const body = JSON.parse(String(state.api.mock.calls.find((call) => call[1]?.method === "PUT")![1].body));
    expect(body.policy.levels).toHaveLength(6);
  });

  it("羁绊玩法标签能改里程碑，概览显示羁绊统计", async () => {
    state.api.mockImplementation((path: string) => (path === "/affinity/stats" ? ok(STATS) : ok(policy)));
    renderAt("/affinity", <AffinityAdminPage />);
    expect(await screen.findByText(/近 30 天心动瞬间/)).toBeInTheDocument();
    expect(screen.getByText(/一周之约 ×3/)).toBeInTheDocument();
    expect(screen.getByText("元气满满")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /羁绊玩法/ }));
    expect(await screen.findByText("连续来往")).toBeInTheDocument();
    expect(screen.getByText(/心动瞬间（Agent 工具/)).toBeInTheDocument();
    const save = screen.getByRole("button", { name: /保\s*存/ });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "今日缘分" }));
    await waitFor(() => expect(save).not.toBeDisabled());
  });

  it("从 URL 直接打开礼物与商店标签", async () => {
    state.api.mockImplementation((path: string) => (path === "/affinity/stats" ? ok(STATS) : ok(policy)));
    renderAt("/affinity?aff.tab=economy", <AffinityAdminPage />);
    expect(await screen.findByText("商店道具")).toBeInTheDocument();
    expect(screen.getAllByDisplayValue("奶茶").length).toBeGreaterThan(0);
  });
});

describe("我的空间 · 好感玩法", () => {
  it("权益页列出这一级的加成和下一级解锁", async () => {
    state.api.mockImplementation(() => ok(perks));
    renderAt("/me/affinity?tab=perks", <MyAffinityPlay view="perks" />);
    expect(await screen.findByText("各等级一览")).toBeInTheDocument();
    expect(screen.getAllByText(/专属称呼/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/朋友/).length).toBeGreaterThan(0);
  });

  it("玩法页可以兑换道具，结果刷新背包", async () => {
    const bought: PerksState = { ...perks, bag: { ...perks.bag, items: [...perks.bag.items, { key: "repair_card", name: "补签卡", count: 1, description: "" }] } };
    state.api.mockImplementation((path: string) => {
      if (path === "/me/affinity/perks") return ok(perks);
      if (path === "/me/affinity/buy") return ok({ result: { item: "补签卡" }, quests: { claimed: [], affinity: 0, bonus_item: null }, state: bought });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderAt("/me/affinity?tab=play", <MyAffinityPlay view="play" />);
    expect(await screen.findByText("好感商店")).toBeInTheDocument();
    expect(screen.getAllByText("今日任务").length).toBeGreaterThan(0);
    expect(screen.getByDisplayValue("阿明")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: /兑\s*换/ })[0]);
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/affinity/buy", expect.objectContaining({ method: "POST" })));
    await waitFor(() => expect(screen.getAllByRole("button", { name: /使\s*用/ }).length).toBe(perks.bag.items.length + 1));
  });

  it("羁绊页显示关系称号、连续来往、徽章，能抽今日缘分", async () => {
    const drawn: PerksState = { ...perks, bond: { ...perks.bond, fortune: { day: "2026-11-11", tier: "大吉", lucky_gift: "🍬糖果", yi: "早点睡", ji: "熬夜", advice: "今天适合说谢谢。" } } };
    const noFortune: PerksState = { ...perks, bond: { ...perks.bond, fortune: null } };
    state.api.mockImplementation((path: string) => {
      if (path === "/me/affinity/perks") return ok(noFortune);
      if (path === "/me/affinity/fortune") return ok({ result: { ...drawn.bond.fortune, first_today: true, affinity_up: true, message: "🔮" }, quests: { claimed: [], affinity: 0, bonus_item: null }, state: drawn });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderAt("/me/affinity?tab=bond", <MyAffinityPlay view="bond" />);
    expect(await screen.findByText(perks.bond.title)).toBeInTheDocument();
    expect(screen.getByText("羁绊徽章")).toBeInTheDocument();
    expect(screen.getByText("陪我聊到半夜")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /升到「熟人」那天/ }));
    expect(screen.getByText(/谢谢你常来找我/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /抽一签/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/affinity/fortune", expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("大吉")).toBeInTheDocument();
  });

  it("玩法页把没解锁的礼物锁住", async () => {
    state.api.mockImplementation(() => ok(perks));
    renderAt("/me/affinity?tab=play", <MyAffinityPlay view="play" />);
    expect(await screen.findByText("流星")).toBeInTheDocument();
    expect(screen.getByText("流星").closest("button")).toBeDisabled();
    expect(screen.getAllByText(/「好朋友」解锁/).length).toBeGreaterThan(0);
  });
});
