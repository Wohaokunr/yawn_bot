import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("../api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));
vi.mock("../relation-graph", () => ({ RelationGraphView: () => <div>graph</div> }));

import { RelationsPanel } from "../agent-panels/RelationsPanel";
import { changedBondFields } from "./BondDetailDrawer";

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  });
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: false,
    media: query,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});
afterAll(() => vi.unstubAllGlobals());

const bond = {
  id: "5", userId: "7", botId: "9", name: "阿眠", stage: "friend", stageLabel: "朋友", familiarity: 0.62, rawFamiliarity: 0.62,
  affinity: 0.4, trust: 0.5, label: "爱逗我的老朋友", memberCallsBot: "喵喵", botCallsMember: "眠眠", tone: "可以互相吐槽",
  note: "", insideJokes: ["猫猫税"], boundaries: ["别提考试"], lockedFields: ["member_calls_bot"], sourceKind: "auto",
  memberToBotCount: 30, botToMemberCount: 10, exchangeCount: 8, positiveCount: 4, negativeCount: 0, activeDays: 6,
  knownDays: 40, idleDays: 1, evidenceCount: 3, momentCount: 1, firstInteractionAt: null,
  lastInteractionAt: "2026-10-24T10:00:00+08:00", lastModelUpdateAt: null, updatedAt: null,
};

function envelope<T>(data: T, meta: Record<string, unknown> = {}) {
  return Promise.resolve({ data, meta });
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation((path: string, init?: RequestInit) => {
    if (path.includes("/bonds?")) return envelope([bond], { total: 1, tableReady: true });
    if (path.endsWith("/bonds/summary")) {
      return envelope({
        tableReady: true, total: 1, avgFamiliarity: 0.62, avgAffinity: 0.4, activeWeekCount: 1,
        stageCounts: [{ stage: "friend", label: "朋友", count: 1 }], top: [],
      });
    }
    if (path.endsWith("/bonds/7") && (!init || !init.method)) {
      return envelope({ ...bond, moments: [{ id: "m1", content: "一起熬夜打完了活动", sourceKind: "auto", accessCount: 2, createdAt: null, updatedAt: null, expiresAt: null }], evidence: [], evidenceMissing: 0 });
    }
    if (path.endsWith("/bonds/7") && init?.method === "PATCH") return envelope(bond);
    if (path.includes("/relations")) {
      if (path.includes("/relations?")) return envelope([], { total: 0 });
      if (path.endsWith("/summary")) return envelope({ edgeCount: 0, linkedMemberCount: 0, typeCounts: [], lastSeenAt: null });
      if (path.endsWith("/types")) return envelope([]);
      if (path.endsWith("/graph")) return envelope({ nodes: [], edges: [], meta: {} });
    }
    throw new Error(`unexpected api path: ${path}`);
  });
});

function renderPanel(url = "/agent/100?tab=relations&relations.view=bot") {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={[url]}>
        <RelationsPanel groupId="100" />
      </MemoryRouter>
    </AntApp>,
  );
}

describe("Bot 视角关系视图", () => {
  it("展示 Bot 与成员的关系卡片，不拉成员关系图谱", async () => {
    renderPanel();
    expect(await screen.findByText("爱逗我的老朋友")).toBeInTheDocument();
    expect(screen.getByText("叫我「喵喵」")).toBeInTheDocument();
    expect(screen.getByText("梗：猫猫税")).toBeInTheDocument();
    const paths = apiMock.mock.calls.map(([path]) => String(path));
    expect(paths.some((path) => path.endsWith("/relations/graph"))).toBe(false);
  });

  it("点卡片打开详情，能看到共同经历和边界", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /阿眠，朋友/ }));
    expect(await screen.findByText("一起熬夜打完了活动")).toBeInTheDocument();
    expect(screen.getByText("别提考试")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "解锁他怎么叫 Bot" })).toBeInTheDocument();
  });

  it("切换阶段筛选会带上 stage 参数", async () => {
    renderPanel();
    await screen.findByText("爱逗我的老朋友");
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "按阶段筛选" }));
    const options = await screen.findAllByText("亲近");
    fireEvent.click(options[options.length - 1]);
    await waitFor(() => expect(apiMock.mock.calls.some(([path]) => String(path).includes("stage=close"))).toBe(true));
  });

  it("表未迁移时提示先执行迁移", async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.includes("/bonds?")) return envelope([], { total: 0, tableReady: false });
      if (path.endsWith("/bonds/summary")) return envelope({ tableReady: false, total: 0, stageCounts: [] });
      if (path.includes("/relations?")) return envelope([], { total: 0 });
      if (path.endsWith("/relations/summary")) return envelope({ edgeCount: 0, linkedMemberCount: 0, typeCounts: [], lastSeenAt: null });
      if (path.endsWith("/relations/types")) return envelope([]);
      throw new Error(`unexpected api path: ${path}`);
    });
    renderPanel();
    expect(await screen.findByText("Bot 关系表还没有建好")).toBeInTheDocument();
  });

  it("只提交改过的字段，避免把没动的字段也锁住", () => {
    const before = {
      label: "a", memberCallsBot: "", botCallsMember: "", tone: "", note: "",
      insideJokes: ["x"], boundaries: [], affinity: 0, trust: 0.3,
    };
    expect(changedBondFields(before, { ...before, label: "b", insideJokes: ["x"] })).toEqual({ label: "b" });
    expect(changedBondFields(before, { ...before, insideJokes: ["x", "y"] })).toEqual({ insideJokes: ["x", "y"] });
  });
});
