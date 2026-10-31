import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { UserAffinity, UserPrivateAgent } from "./user-insights";
import {
  AffinityPanel,
  formatDelta,
  privateAgentState,
  PrivateAgentPanel,
  sourceRows,
  turnsText,
} from "./user-insights";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("./api", () => ({ api: state.api }));

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

function privateAgent(overrides: Partial<UserPrivateAgent> = {}): UserPrivateAgent {
  return {
    userId: "42",
    enabled: true,
    agentLoaded: true,
    autoReply: true,
    friendsOnly: true,
    dailyTurns: 120,
    autoPaused: false,
    turnsToday: 3,
    memoryAvailable: true,
    facts: [
      {
        key: "hobby",
        label: "爱好",
        content: "爬山",
        source: "agent",
        sourceLabel: "本人要求记的",
        updatedAt: null,
        expiresAt: null,
        expired: false,
      },
    ],
    ...overrides,
  };
}

function affinity(overrides: Partial<UserAffinity> = {}): UserAffinity {
  return {
    userId: "42",
    points: 70,
    level: "熟人",
    levelIndex: 2,
    nextLevel: "朋友",
    toNext: 80,
    progress: 0.11,
    today: { checkin: 3 },
    available: true,
    levels: [],
    sources: {
      checkin: { label: "签到", dailyCap: 8 },
      manual: { label: "管理员调整", dailyCap: null },
    },
    breakdown30d: { checkin: 20, manual: -5 },
    events: [
      {
        id: 1,
        group_id: 101,
        source: "checkin",
        source_label: "签到",
        delta: 3,
        balance_after: 70,
        note: "",
        day: "2026-10-31",
        created_at: "2026-10-31T08:00:00",
      },
    ],
    groups: [
      {
        groupId: "101",
        groupName: "测试群",
        groupAffinity: 5,
        checkedToday: true,
        streakDays: 7,
        totalDays: 20,
        points: 200,
        exp: 120,
        lastCheckinDate: "2026-10-31",
      },
    ],
    ...overrides,
  };
}

describe("纯函数", () => {
  it("formatDelta 给正数加号", () => {
    expect(formatDelta(3)).toBe("+3");
    expect(formatDelta(0)).toBe("0");
    expect(formatDelta(-2)).toBe("-2");
  });

  it("sourceRows 合并上限、今日与 30 天", () => {
    expect(sourceRows(affinity())).toEqual([
      { key: "checkin", label: "签到", today: 3, cap: 8, last30: 20 },
      { key: "manual", label: "管理员调整", today: 0, cap: null, last30: -5 },
    ]);
  });

  it("privateAgentState 按优先级判断", () => {
    expect(privateAgentState(privateAgent({ enabled: false, autoPaused: true }))).toBe("off");
    expect(privateAgentState(privateAgent({ agentLoaded: false }))).toBe("not_loaded");
    expect(privateAgentState(privateAgent({ autoReply: false, autoPaused: true }))).toBe("manual_only");
    expect(privateAgentState(privateAgent({ autoPaused: true }))).toBe("paused");
    expect(privateAgentState(privateAgent())).toBe("auto");
  });

  it("turnsText 区分不限", () => {
    expect(turnsText(3, 120)).toBe("3 / 120");
    expect(turnsText(3, 0)).toBe("3（不限）");
  });
});

describe("PrivateAgentPanel", () => {
  beforeEach(() => {
    state.api.mockReset();
  });

  it("显示状态与记忆，关掉开关时发 PATCH 暂停", async () => {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return Promise.resolve({ data: privateAgent({ autoPaused: true }), meta: {} });
      return Promise.resolve({ data: privateAgent(), meta: {} });
    });
    render(<AntApp><PrivateAgentPanel userId="42" /></AntApp>);
    expect(await screen.findByText("自动回复")).toBeInTheDocument();
    expect(screen.getByText("爬山")).toBeInTheDocument();
    expect(screen.getByText("3 / 120")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "自动回复这位用户" }));
    await waitFor(() => {
      expect(state.api).toHaveBeenCalledWith("/users/42/private-agent", {
        method: "PATCH",
        body: JSON.stringify({ autoPaused: true }),
      });
    });
  });

  it("自动接管关闭时开关不可用", async () => {
    state.api.mockResolvedValue({ data: privateAgent({ autoReply: false }), meta: {} });
    render(<AntApp><PrivateAgentPanel userId="42" /></AntApp>);
    expect(await screen.findByText("需 /对话")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "自动回复这位用户" })).toBeDisabled();
  });
});

describe("AffinityPanel", () => {
  beforeEach(() => {
    state.api.mockReset();
  });

  it("显示等级、群签到与流水", async () => {
    state.api.mockResolvedValue({ data: affinity(), meta: {} });
    render(<AntApp><AffinityPanel userId="42" /></AntApp>);
    expect(await screen.findByText("熟人")).toBeInTheDocument();
    expect(screen.getByText("距「朋友」还差 80")).toBeInTheDocument();
    expect(screen.getByText("测试群")).toBeInTheDocument();
    expect(screen.getByText("3 / 8")).toBeInTheDocument();
    expect(state.api).toHaveBeenCalledWith("/users/42/affinity", expect.objectContaining({ signal: expect.anything() }));
  });

  it("流水表未迁移时提示并禁用调整", async () => {
    state.api.mockResolvedValue({ data: affinity({ available: false, events: [] }), meta: {} });
    render(<AntApp><AffinityPanel userId="42" /></AntApp>);
    expect(await screen.findByText("好感流水表还没迁移")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /调\s*整/ })).toBeDisabled();
  });
});
