import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Persona, PersonaCard, PersonaTemplate } from "../types";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("../api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status: number;
    fields: Record<string, string>;

    constructor(status: number, message: string, fields: Record<string, string> = {}) {
      super(message);
      this.status = status;
      this.fields = fields;
    }
  },
}));

import { PersonaPanel } from "./PersonaPanel";

const baseCard: PersonaCard = {
  presetId: "natural",
  name: "Yawn",
  selfName: "",
  userTitle: "",
  identity: "自然群友",
  personality: "",
  speakingStyle: "",
  catchphrases: [],
  likes: [],
  avoidTopics: [],
  examples: [],
  warmth: 2,
  humor: 1,
  verbosity: 1,
  expressiveness: 1,
  sociability: 2,
  reactionTendency: 2,
};

const catCard: PersonaCard = {
  ...baseCard,
  presetId: "tsundere_catgirl",
  name: "猫猫",
  selfName: "本喵",
  userTitle: "杂鱼",
  catchphrases: ["哼", "喵"],
  humor: 4,
};

const persona: Persona = {
  groupId: "100",
  enabled: false,
  schemaVersion: 3,
  card: baseCard,
  effectiveCard: baseCard,
  source: "builtin",
  sourceLabel: "内置默认",
  globalCard: baseCard,
  globalLabel: "内置默认",
  globalSummary: "Yawn · 自然群友",
  presets: [
    { id: "natural", label: "自然群友", description: "自然", card: baseCard },
    { id: "tsundere_catgirl", label: "傲娇猫娘", description: "嘴硬心软", card: catCard },
  ],
  summary: "Yawn · 自然群友 · 自然",
  behavior: {
    source: "builtin",
    sociability: 2,
    followupTendency: 2,
    reactionTendency: 2,
    warmupProbabilityScale: 0.75,
    activeProbabilityScale: 0.75,
    maxFollowupBotTurns: 3,
    allowSpontaneousReaction: true,
    reactionMode: "normal",
  },
  emotion: {
    schemaVersion: 1,
    label: "neutral",
    displayLabel: "平静",
    valence: 0,
    arousal: 0,
    intensity: 0,
    expressionIntensity: 0,
    expressionHint: "",
    source: "",
    reason: "",
    updatedAt: null,
    ageMinutesBucket: 0,
    eventCount: 0,
  },
  compiled: "{\"name\":\"Yawn\"}",
  promptTokens: 12,
  promptTokenLimit: 700,
  trimmed: false,
  version: "v1",
};

const template: PersonaTemplate = {
  id: "7",
  name: "猫娘",
  isGlobal: true,
  card: catCard,
  summary: "猫猫 · 自称本喵 · 傲娇猫娘",
  version: "1",
  updatedAt: null,
};

function mockApi(): void {
  apiMock.mockImplementation((path: string, init?: RequestInit) => {
    if (path === "/agent/persona-library") return Promise.resolve({ data: [template], meta: {} });
    if (path === "/agent/groups/100/persona" && init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { card: PersonaCard; enabled: boolean };
      return Promise.resolve({ data: { ...persona, enabled: body.enabled, card: body.card, version: "v2" }, meta: {} });
    }
    if (path === "/agent/groups/100/persona/apply-template") {
      return Promise.resolve({ data: { ...persona, enabled: true, card: catCard, source: "group", version: "v2" }, meta: {} });
    }
    return Promise.resolve({ data: persona, meta: {} });
  });
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

afterAll(() => vi.unstubAllGlobals());

beforeEach(() => apiMock.mockReset());

describe("PersonaPanel v3", () => {
  it("套用模板后保存的是 v3 card，并保留名字", async () => {
    mockApi();
    render(<AntApp><PersonaPanel groupId="100" /></AntApp>);

    fireEvent.click(await screen.findByRole("button", { name: /傲娇猫娘/ }));
    fireEvent.click(screen.getByText("当前群自定义", { selector: ".ant-segmented-item-label" }));
    fireEvent.click(screen.getByRole("button", { name: "保存人设" }));

    await waitFor(() => expect(apiMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const [, init] = apiMock.mock.calls.find(([, call]) => call?.method === "PUT")!;
    const body = JSON.parse(String(init.body)) as { enabled: boolean; card: PersonaCard; profile?: unknown };
    expect(body.enabled).toBe(true);
    expect(body.profile).toBeUndefined();
    expect(body.card.presetId).toBe("tsundere_catgirl");
    expect(body.card.selfName).toBe("本喵");
    expect(body.card.catchphrases).toEqual(["哼", "喵"]);
    expect(body.card.name).toBe("Yawn");
  });

  it("人设库列出全局人设，可以一键用于本群", async () => {
    mockApi();
    render(<AntApp><PersonaPanel groupId="100" /></AntApp>);

    expect(await screen.findByText("全局人设", { selector: ".ant-tag" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "用于本群" }));

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/agent/groups/100/persona/apply-template",
      expect.objectContaining({ method: "POST" }),
    ));
    const [, init] = apiMock.mock.calls.find(([path]) => path === "/agent/groups/100/persona/apply-template")!;
    expect(JSON.parse(String(init.body))).toEqual({ templateId: 7, version: "v1" });
  });

  it("全局人设变化时会重新读取本群人设", async () => {
    mockApi();
    render(<AntApp><PersonaPanel groupId="100" /></AntApp>);
    await screen.findByRole("button", { name: /傲娇猫娘/ });
    const reads = () => apiMock.mock.calls.filter(([path, init]) => path === "/agent/groups/100/persona" && !init?.method).length;
    const before = reads();

    window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
      detail: { resource: "agent_persona_library", scope: null, entityId: "global" },
    }));
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });
});
