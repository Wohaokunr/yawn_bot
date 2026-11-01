import { describe, expect, it } from "vitest";
import type { WwEvent, WwGame, WwPlayer } from "./types";
import {
  aiSummary,
  configRows,
  deathLabel,
  deathOrder,
  eventFilterOf,
  filterCounts,
  filterEvents,
  formatDuration,
  gameDuration,
  groupByRound,
  liveAlerts,
  mergeEvents,
  outlook,
  parseView,
  phaseCountdown,
  phaseInfo,
  roleInfo,
  seatActivity,
  seatPosition,
  voteSummaries,
} from "./ww-meta";

function ev(seq: number, type: string, extra: Partial<WwEvent> = {}): WwEvent {
  return { seq, ts: "2026-11-01T10:00:00Z", type, roundNo: 1, phase: "DAY_SPEECH", seat: null, userId: null, name: null, text: null, extra: {}, ...extra };
}

function player(seat: number, role: string, faction: string, alive = true, extra: Partial<WwPlayer> = {}): WwPlayer {
  return { seat, userId: seat, name: `P${seat}`, isAi: false, alive, isSheriff: false, role, faction, deathRound: alive ? null : 1, deathCause: alive ? null : "VOTED", ...extra };
}

function game(players: WwPlayer[], extra: Partial<WwGame> = {}): WwGame {
  return {
    groupId: 1, hostUserId: 1, board: "预女猎白", phase: "DAY_SPEECH", phaseLabel: "白天发言", roundNo: 1, currentSpeaker: null,
    signupCount: players.length, playerCount: players.length, aiCount: 0, aliveCount: players.filter((p) => p.alive).length,
    queueDepth: 0, pendingCount: 0, workerAlive: true, players, signup: [], ...extra,
  };
}

describe("阶段与角色字典", () => {
  it("已知阶段给出中文名与所属环节，未知阶段兜底", () => {
    expect(phaseInfo("NIGHT_WITCH")).toMatchObject({ label: "女巫用药", stage: "night" });
    expect(phaseInfo("NIGHT_GUARD").stage).toBe("night");
    expect(phaseInfo("NIGHT_SOMETHING_NEW", "夜晚")).toMatchObject({ label: "夜晚", stage: "night" });
    expect(phaseInfo(null).label).toBe("未知阶段");
  });

  it("新角色与未知角色都有阵营", () => {
    expect(roleInfo("守卫").kind).toBe("god");
    expect(roleInfo("白狼王").faction).toBe("wolf");
    expect(roleInfo("恶灵骑士", "wolf").faction).toBe("wolf");
    expect(roleInfo("新角色").faction).toBe("good");
  });

  it("死因有中文，未知值原样显示", () => {
    expect(deathLabel("WITCH_POISON")).toBe("女巫毒杀");
    expect(deathLabel("WOLF_KING_SHOT")).toBe("狼王开枪");
    expect(deathLabel("SOMETHING")).toBe("SOMETHING");
    expect(deathLabel(null)).toBe("出局");
  });
});

describe("事件筛选", () => {
  const events = [
    ev(1, "phase", { text: "DAY_SPEECH" }),
    ev(2, "speech", { seat: 3, text: "我是预言家，昨晚验了 5 号是狼" }),
    ev(3, "ai_decision", { seat: 5, text: "投票3", extra: { attempt: 2 } }),
    ev(4, "vote_tally", { extra: { votes: [{ voterSeat: 1, targetSeat: 5, isSheriff: true }, { voterSeat: 2, targetSeat: 5, isSheriff: false }, { voterSeat: 3, targetSeat: null, isSheriff: false }], counts: { "5": 2.5 } } }),
    ev(5, "death", { seat: 5, roundNo: 2, extra: { role: "狼人", deathCause: "VOTED" } }),
  ];

  it("类型归类与计数", () => {
    expect(eventFilterOf("ai_speech")).toBe("ai");
    expect(eventFilterOf("announce")).toBe("flow");
    expect(filterCounts(events)).toMatchObject({ all: 5, talk: 1, ai: 1, vote: 1, death: 1, flow: 1 });
  });

  it("按座位筛选时包含计票里投出/被投的记录", () => {
    const seat5 = filterEvents(events, { filter: "all", seat: 5, round: null, text: "" }).map((event) => event.seq);
    expect(seat5).toEqual([3, 4, 5]);
    const seat1 = filterEvents(events, { filter: "all", seat: 1, round: null, text: "" }).map((event) => event.seq);
    expect(seat1).toEqual([4]);
  });

  it("全文搜索与天数筛选", () => {
    expect(filterEvents(events, { filter: "all", seat: null, round: null, text: "预言家" }).map((event) => event.seq)).toEqual([2]);
    expect(filterEvents(events, { filter: "all", seat: null, round: 2, text: "" }).map((event) => event.seq)).toEqual([5]);
  });

  it("按天分组保持顺序", () => {
    const groups = groupByRound(events);
    expect(groups.map((group) => group.round)).toEqual([1, 2]);
    expect(groups[0].events).toHaveLength(4);
  });

  it("计票汇总采用后端权威票数并标出最高票", () => {
    const [vote] = voteSummaries(events);
    expect(vote.buckets[0]).toMatchObject({ seat: 5, votes: 2.5 });
    expect(vote.leaders).toEqual([5]);
    expect(vote.abstain).toEqual([3]);
    expect(vote.total).toBe(3);
  });

  it("座位活跃度与 AI 汇总", () => {
    expect(seatActivity(events, 5)).toMatchObject({ votesReceived: 2, aiDecisions: 1, aiRetries: 1 });
    expect(seatActivity(events, 3)).toMatchObject({ speeches: 1, votesCast: 0 });
    const ai = aiSummary(events);
    expect(ai).toMatchObject({ decisions: 1, retries: 1, empty: 0 });
    expect(ai.bySeat[0].seat).toBe(5);
  });
});

describe("mergeEvents", () => {
  it("增量追加并去重", () => {
    const merged = mergeEvents([ev(1, "phase"), ev(2, "speech")], [ev(2, "speech"), ev(3, "death")]);
    expect(merged.map((event) => event.seq)).toEqual([1, 2, 3]);
  });

  it("对方 seq 回退时视为新的一局，直接替换", () => {
    const merged = mergeEvents([ev(10, "phase"), ev(11, "speech")], [ev(1, "phase"), ev(2, "speech")]);
    expect(merged.map((event) => event.seq)).toEqual([1, 2]);
  });

  it("超过上限只保留最新", () => {
    const many = Array.from({ length: 10 }, (_, index) => ev(index + 1, "announce"));
    expect(mergeEvents([], many, 4).map((event) => event.seq)).toEqual([7, 8, 9, 10]);
  });
});

describe("局势推算", () => {
  it("屠边：神职全灭判狼优", () => {
    const view = outlook([player(1, "狼人", "wolf"), player(2, "预言家", "good", false), player(3, "村民", "good"), player(4, "村民", "good")]);
    expect(view).toMatchObject({ wolves: 1, gods: 0, villagers: 2, tone: "wolf" });
  });

  it("狼人全灭判好人", () => {
    expect(outlook([player(1, "狼人", "wolf", false), player(2, "女巫", "good")]).tone).toBe("good");
  });

  it("没发牌时提示未发牌", () => {
    expect(outlook([]).verdict).toBe("尚未发牌");
  });
});

describe("运维告警与倒计时", () => {
  it("引擎停止、队列积压、私聊失败都会报警", () => {
    const alerts = liveAlerts(game([player(1, "村民", "good", true, { dmOk: false })], { workerAlive: false, queueDepth: 30 }), { now: Date.now() });
    expect(alerts.map((alert) => alert.level)).toEqual(["error", "warning", "warning"]);
  });

  it("阶段超时过久报警", () => {
    const start = Date.parse("2026-11-01T10:00:00Z");
    const alerts = liveAlerts(game([player(1, "村民", "good")], { phaseStartedAt: "2026-11-01T10:00:00Z" }), { now: start + 400_000, timeouts: { DAY_SPEECH: 120 } });
    expect(alerts.some((alert) => alert.text.includes("超过窗口"))).toBe(true);
  });

  it("优先使用引擎剩余时间，否则按开始时间估算", () => {
    const start = Date.parse("2026-11-01T10:00:00Z");
    expect(phaseCountdown(game([], { phaseRemaining: 12.4 }), { DAY_SPEECH: 120 }, start)).toMatchObject({ remaining: 12, estimated: false });
    expect(phaseCountdown(game([], { phaseStartedAt: "2026-11-01T10:00:00Z" }), { DAY_SPEECH: 120 }, start + 30_000)).toMatchObject({ remaining: 90, estimated: true, total: 120 });
    expect(phaseCountdown(game([]), {}, start)).toBeNull();
  });
});

describe("格式化与杂项", () => {
  it("时长", () => {
    expect(formatDuration(45)).toBe("45 秒");
    expect(formatDuration(125)).toBe("2 分 5 秒");
    expect(formatDuration(3720)).toBe("1 小时 2 分");
    expect(formatDuration(null)).toBe("—");
    expect(gameDuration("2026-11-01T10:00:00Z", "2026-11-01T10:30:00Z")).toBe(1800);
    expect(gameDuration("2026-11-01T10:00:00Z", null)).toBeNull();
  });

  it("出局顺序按天再按座位", () => {
    const order = deathOrder([{ seat: 3, deathRound: 2 }, { seat: 1, deathRound: null }, { seat: 4, deathRound: 1 }, { seat: 2, deathRound: 2 }]);
    expect(order.map((item) => item.seat)).toEqual([4, 2, 3]);
  });

  it("配置分组，未知键归入其他", () => {
    const groups = configRows({ ww_speech_timeout: 120, ww_ai_enabled: true, ww_new_thing: 3 });
    expect(groups.map((group) => group.group)).toEqual(["白天", "AI 玩家", "其他"]);
    expect(groups[2].rows[0].label).toBe("ww_new_thing");
  });

  it("视图参数非法时回到实时对局", () => {
    expect(parseView("insights")).toBe("insights");
    expect(parseView("nope")).toBe("live");
  });

  it("座位 1 号在正上方", () => {
    expect(seatPosition(1, 9).y).toBeCloseTo(8, 5);
  });
});
