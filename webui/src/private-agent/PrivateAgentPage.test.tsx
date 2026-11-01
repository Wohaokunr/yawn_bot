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
  if (path.startsWith("/private-agent/captures/12345")) return { data: { captureEnabled: true, paused: false, perUser: 8, maxUsers: 64, userId: "12345", prompts: [] } };
  if (path.startsWith("/private-agent/prompts/sections/") && init?.method === "PUT") return { data: { revision: 4 } };
  throw new Error(`unexpected ${path}`);
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
});
