import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
vi.mock("../relation-graph", () => ({
  RelationGraphView: ({ focusRequest }: { focusRequest?: { userId: string } | null }) => <div>graph focus:{focusRequest?.userId ?? "none"}</div>,
}));

import { RelationsPanel } from "../agent-panels/RelationsPanel";

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

const edges = [
  { id: "11", groupId: "100", subjectUserId: "1", objectUserId: "2", type: "好友", sourceKind: "manual", note: "常聊天", confidence: 0.9, evidenceCount: 3, lastSeenAt: "2026-10-22T10:00:00+08:00" },
  { id: "12", groupId: "100", subjectUserId: "2", objectUserId: "1", type: "好友", sourceKind: "auto", note: "", confidence: 0.7, evidenceCount: 1, lastSeenAt: "2026-10-20T10:00:00+08:00" },
  { id: "13", groupId: "100", subjectUserId: "3", objectUserId: "1", type: "对立", sourceKind: "mention", note: "吵过架", confidence: 0.3, evidenceCount: 1, lastSeenAt: "2026-06-01T10:00:00+08:00" },
];
const nodes = [
  { userId: "1", nickname: "阿一", groupNickname: null, role: "owner", linked: true, degree: 3 },
  { userId: "2", nickname: "小二", groupNickname: null, role: "member", linked: true, degree: 2 },
  { userId: "3", nickname: "张三", groupNickname: null, role: "member", linked: true, degree: 1 },
];
const names: Record<string, string> = { 1: "阿一", 2: "小二", 3: "张三" };

function envelope<T>(data: T, meta: Record<string, unknown> = {}) {
  return Promise.resolve({ data, meta });
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation((path: string, init?: RequestInit) => {
    if (path.includes("/relations?")) {
      return envelope(edges.map((edge) => ({ ...edge, subjectName: names[edge.subjectUserId], objectName: names[edge.objectUserId], mutual: edge.type === "好友" })), { total: edges.length });
    }
    if (path.endsWith("/relations/summary")) {
      return envelope({
        edgeCount: 3, linkedMemberCount: 3, typeCounts: [{ type: "好友", count: 2 }, { type: "对立", count: 1 }], lastSeenAt: "2026-10-22T10:00:00+08:00",
        avgConfidence: 0.63, mutualEdgeCount: 2, freshEdgeCount: 1, staleEdgeCount: 1, lowConfidenceCount: 1,
        sourceCounts: [{ source: "manual", count: 1 }, { source: "auto", count: 1 }, { source: "mention", count: 1 }],
      });
    }
    if (path.endsWith("/relations/types")) return envelope(["好友", "对立"]);
    if (path.endsWith("/relations/graph")) return envelope({ nodes, edges, meta: { relationTruncated: false, memberTruncated: false } });
    if (path.endsWith("/relations/batch-delete")) {
      const body = JSON.parse(String(init?.body)) as { ids: number[] };
      return envelope({ deleted: body.ids.length, missing: 0 });
    }
    throw new Error(`unexpected api path: ${path}`);
  });
});

function renderPanel(url = "/agent/100?tab=relations") {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={[url]}>
        <RelationsPanel groupId="100" />
      </MemoryRouter>
    </AntApp>,
  );
}

const listCalls = () => apiMock.mock.calls.map(([path]) => String(path)).filter((path) => path.includes("/relations?"));

describe("RelationsPanel 2.0", () => {
  it("指标条展示摘要并可点类型筛选列表", async () => {
    renderPanel();
    expect(await screen.findByText("常聊天")).toBeInTheDocument();
    expect(screen.getByText("67%")).toBeInTheDocument();
    expect(screen.getByText("待复核")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^对立\s*1$/ }));
    await waitFor(() => expect(listCalls().some((path) => path.includes("type=%E5%AF%B9%E7%AB%8B"))).toBe(true));
    expect(screen.getByText("清除全部")).toBeInTheDocument();
  });

  it("点击列头按服务端字段排序", async () => {
    renderPanel();
    await screen.findByText("常聊天");
    fireEvent.click(screen.getAllByText("最后见到")[0]);
    await waitFor(() => expect(listCalls().some((path) => path.includes("sort=lastSeen") && path.includes("order=asc"))).toBe(true));
  });

  it("勾选后批量删除走批量接口", async () => {
    renderPanel();
    await screen.findByText("常聊天");
    const table = document.querySelector(".rel-table") as HTMLElement;
    const selectAll = within(table).getAllByRole("checkbox")[0];
    fireEvent.click(selectAll);
    fireEvent.click(await screen.findByRole("button", { name: /批量删除/ }));
    const confirm = await screen.findByRole("button", { name: /^确\s?定$|^OK$/ });
    fireEvent.click(confirm);
    await waitFor(() => {
      const call = apiMock.mock.calls.find(([path]) => String(path).endsWith("/relations/batch-delete"));
      expect(call).toBeTruthy();
      expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ ids: [11, 12, 13] });
    });
  });

  it("成员视图按需拉取图谱并展示成员卡片", async () => {
    renderPanel("/agent/100?tab=relations&relations.view=members");
    expect(await screen.findByRole("button", { name: "阿一，3 条关系" })).toBeInTheDocument();
    expect(apiMock.mock.calls.some(([path]) => String(path).endsWith("/relations/graph"))).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "阿一，3 条关系" }));
    expect(await screen.findByText("阿一 · 关系档案")).toBeInTheDocument();
  });

  it("洞察视图给出健康分与待复核队列", async () => {
    renderPanel("/agent/100?tab=relations&relations.view=insights");
    expect(await screen.findByText("关系数据健康度")).toBeInTheDocument();
    expect(screen.getByText("长期未见 1")).toBeInTheDocument();
    expect(screen.getByText("低置信 1")).toBeInTheDocument();
    expect(screen.getByText("社交核心")).toBeInTheDocument();
  });

  it("URL 里的成员筛选在图谱里变成聚焦请求", async () => {
    renderPanel("/agent/100?tab=relations&relations.view=graph&relations.member=3");
    expect(await screen.findByText("graph focus:3")).toBeInTheDocument();
  });
});
