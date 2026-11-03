import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

import { PrivateAgentPage } from "./PrivateAgentPage";

const card = {
  presetId: "natural", name: "小橘", selfName: "本喵", userTitle: "", identity: "", personality: "靠谱", speakingStyle: "",
  catchphrases: [], likes: [], avoidTopics: [], examples: [], warmth: 2, humor: 1, verbosity: 1, expressiveness: 1, sociability: 2, reactionTendency: 2,
};

const section = (id: string, patch: Record<string, unknown> = {}) => ({
  id, title: id === "private.safety" ? "私聊安全" : "私聊规则", group: "rules", groupLabel: "规则与安全", category: "private", layer: "private_system", layerLabel: "私聊 system",
  kind: "static", description: "", when: "每轮", safety: id === "private.safety", protocol: false, editable: true, modes: ["replace", "append", "prepend", "disable", "builtin"],
  defaultText: "默认", defaultTokens: 1, sample: null, globalOverride: null, userOverride: null, userOverrideCount: 0, effectiveScope: null,
  effectiveMode: "default", effectiveText: "默认", effectiveTokens: 1, pipelines: [{ pipeline: "turn", label: "主回合", where: "system#1", condition: "" }], ...patch,
});

const catalog = (userId: string | null) => ({
  revision: 3, updatedAt: null, loadError: null, userId,
  groups: [{ key: "rules", label: "规则与安全", hint: "" }],
  pipelines: [{ key: "turn", label: "主回合", description: "一轮", trigger: "发消息", tools: "main", toolSections: [], tokens: { always: 120, conditional: 30, tools: 0 },
    steps: [{ sectionId: "private.rules", title: "私聊规则", where: "system#1", condition: "", mode: "default", scope: null, safety: false, tokens: 10 }] }],
  sections: [section("private.rules", userId ? { userOverride: { mode: "append", text: "叫他小明", note: "", updatedAt: "", updatedBy: "" }, effectiveScope: "user", effectiveMode: "append" } : {}), section("private.safety")],
  issues: [{ level: "danger", code: "safety_changed", sectionId: "private.safety", message: "安全段被删除", scope: "global" }],
  summary: { total: 2, globalOverrides: 1, userOverrides: userId ? 1 : 0, usersWithOverrides: 1, disabled: 0, safetyChanged: 1 },
});

function respond(path: string, init?: RequestInit) {
  if (path.startsWith("/private-agent/prompts/catalog")) return { data: catalog(path.includes("userId=") ? "12345" : null) };
  if (path === "/private-agent/prompts/users") return { data: [{ userId: "12345", count: 1, sections: ["private.rules"], safety: 0, updatedAt: null }] };
  if (path.startsWith("/private-agent/persona/users?") || path === "/private-agent/persona/users") {
    return { data: {
      users: [{ userId: "12345", mode: "custom", source: "user", sourceLabel: "本人专属", name: "小橘", summary: "小橘 · 本喵", templateName: null, missingTemplate: false, locked: false, revision: 2, updatedAt: null, updatedBy: "self", selfEditsToday: 1 }],
      stats: { custom: 1, library: 0, follow: 0, locked: 0, missingTemplate: 0, selfEditsToday: 1 },
      templateUsage: {},
      policy: { enabled: true, selfEdit: true, selfEditDaily: 10, selfFields: ["name"], allowPresets: true, allowLibrary: true, defaultTemplateId: null, revision: 0, updatedAt: null, updatedBy: null },
      options: { presets: [], library: [], fields: { name: "名字" }, promptTokenLimit: 700 },
    } };
  }
  if (path === "/private-agent/persona/users/12345") {
    return { data: { userId: "12345", mode: "custom", card, effectiveCard: card, source: "user", sourceLabel: "本人专属", personal: true, templateId: null, templateName: null, missingTemplate: false, suspended: false,
      summary: "小橘 · 本喵", compiled: "角色：小橘", promptTokens: 20, promptTokenLimit: 700, trimmed: false, locked: false, revision: 2, updatedAt: null, updatedBy: "self",
      selfEditsToday: 1, selfEditDaily: 10, callThem: "阿明", callThemSource: "chat_prefs", history: [{ at: "2026-11-01T10:00:00", by: "self", actor: null, action: "patch", changed: ["自称"], before: { mode: "follow", templateName: null, summary: "Yawn" } }] } };
  }
  if (path === "/private-agent/persona/preview") return { data: { compiled: "角色：小橘", promptTokens: 20, promptTokenLimit: 700, trimmed: false, summary: "", unsafe: null } };
  if (path === "/private-agent/captures") return { data: { captureEnabled: true, paused: false, perUser: 8, maxUsers: 64, users: [{ userId: "12345", count: 2, lastAt: null, lastPipeline: "turn" }] } };
  if (path === "/private-agent/cache") return { data: { cachedTokens: 900, missTokens: 100, hitRate: 0.9, anchoredUsers: 1, pipelines: { turn: { calls: 3, reported: 3, promptTokens: 1000, cachedTokens: 900, missTokens: 100, hitRate: 0.9, recent: [{ at: 1, round: 0, changed: "profile" }] } } } };
  if (path.startsWith("/private-agent/rhythm")) return { data: { settings: { interrupt: true, keepMedia: true, mixHint: true, maxParts: 4 }, stats: { deliveries: 10, interrupted: 2, skippedItems: 3, byParts: { "1": 5, "2": 3, "3": 1, "4": 1, "5+": 0 }, partsShare: { "1": 0.5, "2": 0.3, "3": 0.1, "4": 0.1, "5+": 0 }, byRhythm: { "整段": 5, "分条": 4, "连发": 1 }, withMedia: 1, mixHints: 1, cutNotes: 1 }, trackedUsers: 1, recent: [{ parts: 3, rhythm: "分条", media: 0, interrupted: true, at: 1 }], pattern: "", pendingCut: false } };
  if (path.startsWith("/private-agent/captures/12345")) return { data: { captureEnabled: true, paused: false, perUser: 8, maxUsers: 64, userId: "12345", prompts: [] } };
  if (path === "/private-agent/bridge") return { data: {
    enabled: true, today: { handoffs: 1, relays: 1, open_asks: 1 },
    handoffs: [{ id: "h1a2b", user_id: "12345", group_id: "100", group_name: "猫猫群", topic: "把副本攻略整理好", asked: "私聊发我", reason: "files", created: "2026-11-03 10:00:00", status: "opened" }],
    relays: [{ id: "r3c4d", user_id: "12345", group_id: "100", group_name: "猫猫群", kind: "ask", text: "周六谁有空？", created: "2026-11-03 10:05:00", status: "open", until: "2026-11-03 16:05:00", reply_count: 2 }],
    limits: { handoff_daily: 10, relay_daily: 10, relay_group_hourly: 6, ask_hours: 6, reply_limit: 20, confirm: true },
  } };
  if (path === "/private-agent/bridge/users/12345") return { data: {
    handoffs: [], waiting_confirm: [{ confirm_id: "b1", summary: "在「猫猫群」替你转告：「晚点到」", asked_seconds_ago: 5, he_replied_since: false }],
    relays: [{ id: "r3c4d", group_id: 100, group_name: "猫猫群", kind: "ask", text: "周六谁有空？", created: "2026-11-03 10:05:00", status: "open", replies: [{ from_id: 2, from_name: "小红", text: "我可以", at: "2026-11-03 10:06:00", delivered: true }] }],
  } };
  if (path === "/private-agent/memory-link") return { data: {
    enabled: true, share_enabled: true, group_self_facts: true, tombstone_days: 180, group_fact_limit: 4,
    totals: { sharing: 1, muting: 1, forgetting: 1 },
    users: [{ user_id: "12345", shares: ["preferred_address"], share_groups: 0, muted_groups: 1, groups_in_private: true, tombstones: 1 }],
    events: [{ at: "2026-11-03 10:00", what: "允许群里也知道", keys: ["preferred_address"], user_id: "12345" }],
  } };
  if (path === "/private-agent/memory-link/users/12345/unshare" && init?.method === "POST") return { data: memoryUser(false) };
  if (path === "/private-agent/memory-link/users/12345") return { data: memoryUser(true) };
  if (path.startsWith("/private-agent/prompts/sections/") && init?.method === "PUT") return { data: { revision: 4 } };
  throw new Error(`unexpected ${path}`);
}

function memoryUser(shared: boolean) {
  return {
    user_id: "12345",
    settings: { groups_in_private: true, muted_groups: ["101"], share_keys: shared ? ["preferred_address"] : [], share_groups: [] },
    tombstones: [{ index: 0, what: "爱好", keyword: "香菜", scope: "私聊和各群", until: "2027-05-01 10:00" }],
    ledger: [{ at: "2026-11-03 10:00", what: "允许群里也知道", keys: ["preferred_address"] }],
    private_facts: [{ key: "preferred_address", label: "希望的称呼", content: "阿明", source: "你让我记的" }],
    group_profile: [{ key: "hobby", content: "爬山", groups: ["猫猫群"], self_told: true, as_of: "2026-11-01" }],
    groups: [
      { group_id: "100", name: "猫猫群", agent_readable: true, in_private: true, group_sees_shared: shared ? [{ label: "希望的称呼", content: "阿明" }] : [] },
      { group_id: "101", name: "摸鱼群", agent_readable: true, in_private: false, group_sees_shared: [] },
    ],
  };
}

function renderAt(url: string) {
  return render(<MemoryRouter initialEntries={[url]}><AntApp><PrivateAgentPage /></AntApp></MemoryRouter>);
}

describe("PrivateAgentPage", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    state.api.mockReset();
    state.api.mockImplementation((path: string, init?: RequestInit) => Promise.resolve(respond(path, init)));
  });

  it("shows the private prompt registry with pipelines and health issues", async () => {
    renderAt("/private-agent");
    expect(await screen.findByText("私聊提示词注册表")).toBeTruthy();
    expect(screen.getByText(/体检发现 1 个问题/)).toBeTruthy();
    expect(screen.getAllByText("私聊规则").length).toBeGreaterThan(0);
    expect(screen.getByText("单独改过提示词的用户")).toBeTruthy();
  });

  it("loads the user scope from the URL and saves a per-user override", async () => {
    renderAt("/private-agent?pa.user=12345&pa.section=private.rules");
    expect(await screen.findByText(/正在编辑/)).toBeTruthy();
    expect(screen.getAllByText(/用户 12345 的私聊/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith(
      "/private-agent/prompts/sections/private.rules",
      expect.objectContaining({ method: "PUT", body: expect.stringContaining("\"userId\":12345") }),
    ));
  });

  it("opens a user's persona drawer from the persona tab", async () => {
    renderAt("/private-agent?pa.tab=persona&pa.user=12345");
    expect(await screen.findByText("私聊人设策略")).toBeTruthy();
    expect(await screen.findByText("私聊人设 · 12345")).toBeTruthy();
    expect(await screen.findByText(/他在聊天偏好里设的/)).toBeTruthy();
    expect(screen.getByText(/本人在私聊里改 · 按字段改/)).toBeTruthy();
  });

  it("lists captured private requests per user", async () => {
    renderAt("/private-agent?pa.tab=captures");
    expect(await screen.findByText("私聊最近请求")).toBeTruthy();
    expect(screen.getByRole("button", { name: "12345" })).toBeTruthy();
  });

  it("shows prefix cache hit rate per pipeline", async () => {
    renderAt("/private-agent?pa.tab=captures");
    expect(await screen.findByText("前缀缓存命中")).toBeTruthy();
    expect(screen.getByText("总命中率 90%")).toBeTruthy();
    expect(screen.getByText(/最近一次没命中是因为长期资料变了/)).toBeTruthy();
  });

  it("shows how many parts replies were sent in and interruptions", async () => {
    renderAt("/private-agent?pa.tab=captures&pa.user=12345");
    expect(await screen.findByText("分条发送")).toBeTruthy();
    expect(screen.getByText("可打断")).toBeTruthy();
    expect(screen.getByText(/1 条 · 5 次（50%）/)).toBeTruthy();
    expect(screen.getByText(/被打断 2 次，没发出去 3 条/)).toBeTruthy();
    expect(await screen.findByText(/3 条 · 分条 · 被打断/)).toBeTruthy();
  });

  it("shows group ↔ private hand-offs and relays with limits", async () => {
    renderAt("/private-agent?pa.tab=bridge");
    expect(await screen.findByText("群聊 ↔ 私聊协作")).toBeTruthy();
    expect(screen.getByText("把副本攻略整理好")).toBeTruthy();
    expect(screen.getByText("提问：周六谁有空？")).toBeTruthy();
    expect(screen.getByText(/收到 2 条回复/)).toBeTruthy();
    expect(screen.getByText(/没有私聊对话内容/)).toBeTruthy();
  });

  it("shows one user's relay replies and waiting confirmations", async () => {
    renderAt("/private-agent?pa.tab=bridge&pa.user=12345");
    expect(await screen.findByText("用户 12345 的协作记录")).toBeTruthy();
    expect(await screen.findByText("小红：我可以")).toBeTruthy();
    expect(screen.getByText(/在「猫猫群」替你转告/)).toBeTruthy();
  });

  it("summarizes who shares, mutes and asked to forget across group and private memory", async () => {
    renderAt("/private-agent?pa.tab=memory");
    expect(await screen.findByText(/群聊记忆 ↔ 私聊记忆/)).toBeTruthy();
    expect(screen.getByText("开了共享 1 人")).toBeTruthy();
    expect(screen.getByText(/私聊里聊的内容永远不进群/)).toBeTruthy();
    expect(screen.getByText(/群里也知道：希望的称呼/)).toBeTruthy();
  });

  it("shows one user's flows per group and lets an admin revoke sharing", async () => {
    renderAt("/private-agent?pa.tab=memory&pa.user=12345");
    expect(await screen.findByText(/群里能看到：希望的称呼「阿明」/)).toBeTruthy();
    expect(screen.getByText("他让私聊里别提这个群")).toBeTruthy();
    expect(screen.getByText(/“香菜”/)).toBeTruthy();
    expect(screen.getByText(/他亲口说的/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /撤掉共享/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^(OK|确定|确 定)$/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/private-agent/memory-link/users/12345/unshare", expect.objectContaining({ method: "POST" })));
  });
});
