import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  getCsrfToken: () => "csrf",
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import type { WwEvent, WwGame, WwMeta } from "./types";
import { REVEAL_KEY, WerewolfWorkspace } from "./WerewolfWorkspace";

const meta: WwMeta = {
  defaultBoard: "预女猎白",
  boards: [{ key: "预女猎白", silenceMode: null, isDefault: true, counts: [{ players: 9, wolves: 3, gods: 3, villagers: 3, roles: [{ role: "狼人", count: 3, faction: "wolf", isGod: false }, { role: "预言家", count: 1, faction: "good", isGod: true }] }] }],
  roles: [{ role: "狼人", faction: "wolf", isGod: false, skill: "每晚刀人", voteVariantSkill: null }],
  config: { ww_min_players: 9, ww_speech_timeout: 120, ww_ai_enabled: true },
  phaseTimeouts: { DAY_SPEECH: 120, NIGHT_WITCH: 60 },
  sheriffFinalSpeechSeconds: 60,
  commands: [{ name: "狼人杀", aliases: ["开狼"], description: "开设狼人杀房间", scope: "group", permission: null, level: "entry" }],
};

function game(extra: Partial<WwGame> = {}): WwGame {
  return {
    groupId: 42, groupName: "测试群", hostUserId: 1, board: "预女猎白", phase: "NIGHT_WITCH", phaseLabel: "夜晚", roundNo: 1, currentSpeaker: null,
    signupCount: 3, playerCount: 3, aiCount: 1, aliveCount: 3, queueDepth: 0, pendingCount: 0, workerAlive: true, signup: [],
    players: [
      { seat: 1, userId: 11, name: "阿梓", isAi: false, alive: true, isSheriff: false, role: "狼人", faction: "wolf", deathRound: null, deathCause: null },
      { seat: 2, userId: 12, name: "栗子", isAi: false, alive: true, isSheriff: false, role: "预言家", faction: "good", deathRound: null, deathCause: null },
      { seat: 3, userId: -3, name: "千夏", isAi: true, alive: true, isSheriff: false, role: "女巫", faction: "good", deathRound: null, deathCause: null },
    ],
    ...extra,
  };
}

const events: WwEvent[] = [
  { seq: 1, ts: "2026-11-01T10:00:00Z", type: "phase", roundNo: 1, phase: "NIGHT_WITCH", seat: null, userId: null, name: null, text: "NIGHT_WITCH", extra: {} },
  { seq: 2, ts: "2026-11-01T10:00:05Z", type: "ai_decision", roundNo: 1, phase: "NIGHT_WITCH", seat: 3, userId: -3, name: "千夏", text: "救", extra: { instruction: "女巫用药", action: { kind: "save", value: null } } },
  { seq: 3, ts: "2026-11-01T10:00:09Z", type: "speech", roundNo: 1, phase: "DAY_SPEECH", seat: 2, userId: 12, name: "栗子", text: "我是预言家", extra: { isAi: false } },
];

function route(path: string) {
  if (path.startsWith("/games/werewolf/meta")) return { data: meta, meta: {} };
  if (path.startsWith("/games/werewolf/42/events")) return { data: { game: game(), events: path.includes("after_seq") ? [] : events }, meta: {} };
  if (path.startsWith("/games/werewolf/history")) return { data: [], meta: { total: 0 } };
  if (path.startsWith("/games/werewolf/stats")) {
    return { data: { totals: { games: 0, finished: 0, unfinished: 0, wolfWins: 0, goodWins: 0, wolfWinRate: null, avgRounds: null, avgPlayers: null, aiSeatShare: null, humanPlayers: 0, groups: 0 }, boards: [], roles: [], deathCauses: [], deathRounds: [], rounds: [], aiVsHuman: { ai: { seats: 0, wins: 0, winRate: null }, human: { seats: 0, wins: 0, winRate: null } }, daily: [], hours: [], groups: [], leaderboard: [], truncated: false, window: { days: 30, board: null, groupId: null } }, meta: {} };
  }
  if (path.endsWith("/stop")) return { data: { stopping: true }, meta: {} };
  throw new Error(`unexpected ${path}`);
}

function renderAt(url: string, games: WwGame[] = [game()], onChanged = vi.fn()) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <AntApp>
        <WerewolfWorkspace available games={games} onChanged={onChanged} refreshing={false} />
      </AntApp>
    </MemoryRouter>,
  );
}

beforeAll(() => {
  globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
  });
});

beforeEach(() => {
  state.api.mockReset();
  state.api.mockImplementation(async (path: string) => route(path));
  window.localStorage.clear();
});

describe("WerewolfWorkspace", () => {
  it("实时卡片默认隐藏身份并把夜间子阶段折叠成“夜晚”", async () => {
    renderAt("/games");
    expect(await screen.findByText("测试群")).toBeInTheDocument();
    expect(screen.getByText("1 局进行中")).toBeInTheDocument();
    expect(screen.getAllByText("夜晚").length).toBeGreaterThan(0);
    expect(screen.queryByText("女巫用药")).not.toBeInTheDocument();
  });

  it("打开上帝视角后显示具体夜间环节并记在本机", async () => {
    renderAt("/games");
    fireEvent.click(await screen.findByRole("switch", { name: /上帝视角/ }));
    expect(await screen.findByText("女巫用药")).toBeInTheDocument();
    expect(window.localStorage.getItem(REVEAL_KEY)).toBe("1");
  });

  it("没有对局时给出开局指引", async () => {
    renderAt("/games", []);
    expect(await screen.findByText("现在没有进行中的狼人杀")).toBeInTheDocument();
    expect(screen.getByText("当前没有对局")).toBeInTheDocument();
  });

  it("引擎停止时卡片给出强停建议", async () => {
    renderAt("/games", [game({ workerAlive: false })]);
    expect(await screen.findByText(/引擎任务已停止/)).toBeInTheDocument();
    expect(screen.getByText("1 局引擎异常")).toBeInTheDocument();
  });

  it("观战室：未开上帝视角时隐藏 AI 思考", async () => {
    renderAt("/games?ww.game=42");
    expect(await screen.findByText("我是预言家")).toBeInTheDocument();
    expect(screen.queryByText("救")).not.toBeInTheDocument();
    expect(screen.getByText(/已隐藏 1 条 AI 思考记录/)).toBeInTheDocument();
  });

  it("观战室：上帝视角下显示 AI 思考，点座位可筛选", async () => {
    window.localStorage.setItem(REVEAL_KEY, "1");
    renderAt("/games?ww.game=42");
    expect(await screen.findByText("救")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^2号 栗子/ }));
    await waitFor(() => expect(screen.queryByText("救")).not.toBeInTheDocument());
    // 时间线 + 座位详情里的“最近发言”各一份
    expect(screen.getAllByText("我是预言家")).toHaveLength(2);
    expect(screen.getByText(/2 号 · 栗子/)).toBeInTheDocument();
  });

  it("观战室增量拉取带 after_seq", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderAt("/games?ww.game=42");
      await screen.findByText("我是预言家");
      await vi.advanceTimersByTimeAsync(2600);
      await waitFor(() => expect(state.api.mock.calls.some(([path]) => String(path).includes("after_seq=3"))).toBe(true));
    } finally {
      vi.useRealTimers();
    }
  });

  it("战绩筛选写进请求", async () => {
    renderAt("/games?ww.view=history&ww.board=%E9%A2%84%E5%A5%B3%E7%8C%8E%E7%99%BD&ww.winner=wolf&ww.group=42");
    await waitFor(() => expect(state.api).toHaveBeenCalledWith(expect.stringContaining("/games/werewolf/history?")));
    const call = state.api.mock.calls.map(([path]) => String(path)).find((path) => path.startsWith("/games/werewolf/history?"))!;
    expect(call).toContain("winner=wolf");
    expect(call).toContain("groupId=42");
    expect(decodeURIComponent(call)).toContain("board=预女猎白");
    expect(await screen.findByText("没有符合筛选条件的对局")).toBeInTheDocument();
  });

  it("数据洞察空数据给出提示", async () => {
    renderAt("/games?ww.view=insights");
    expect(await screen.findByText("这个时间范围内还没有对局")).toBeInTheDocument();
    expect(state.api).toHaveBeenCalledWith("/games/werewolf/stats?days=30");
  });

  it("规则页展示板子构成与命令", async () => {
    renderAt("/games?ww.view=rules");
    const card = (await screen.findByText("默认")).closest("article")!;
    expect(within(card).getByText(/狼人 × 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("命令 1"));
    expect(await screen.findByText("/狼人杀")).toBeInTheDocument();
  });
});
