import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("./api", () => ({
  api: state.api,
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

vi.mock("./relation-graph", () => ({
  RelationGraphView: () => <div>relation graph</div>,
}));

import { GuestGroupPage, isGuestTabAllowed } from "./guest-home";

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}</div>;
}

describe("guest group route", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
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
    state.api.mockImplementation((path: string) => {
      if (path === "/groups/100") {
        return Promise.resolve({
          data: { groupId: "100", groupName: "测试群", memberCount: 42 },
          meta: {},
        });
      }
      if (path.startsWith("/agent/groups/100/memories?")) {
        return Promise.resolve({ data: [], meta: { total: 0 } });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
  });

  it("normalizes an admin tab query to memories without mounting admin panels", async () => {
    render(
      <MemoryRouter initialEntries={["/guest/100?tab=config"]}>
        <LocationProbe />
        <Routes>
          <Route path="guest/:groupId" element={<GuestGroupPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("测试群")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/guest/100?tab=memories");
    });
    expect(screen.queryByText("运行配置")).not.toBeInTheDocument();
    expect(screen.queryByText("人设")).not.toBeInTheDocument();
    expect(screen.queryByText("对话调试")).not.toBeInTheDocument();
    expect(screen.queryByText("新增记忆")).not.toBeInTheDocument();
    expect(screen.queryByText("立即整理")).not.toBeInTheDocument();
    expect(screen.queryByText("重建派生记忆")).not.toBeInTheDocument();
    expect(screen.queryByText("导出 JSON")).not.toBeInTheDocument();
    expect(screen.queryByText("清理全群 Agent 数据")).not.toBeInTheDocument();

    const paths = state.api.mock.calls.map(([path]) => String(path));
    expect(paths).toContain("/groups/100");
    expect(paths.some((path) => path.startsWith("/agent/groups/100/memories?"))).toBe(true);
    expect(paths.some((path) => path.endsWith("/memories/status"))).toBe(false);
    expect(paths.some((path) => path.includes("diagnostics"))).toBe(false);
    expect(paths.some((path) => path.includes("/config"))).toBe(false);
    expect(paths.some((path) => path.includes("/debug"))).toBe(false);
  });

  it("keeps relations read-only and only loads graph after switching views", async () => {
    state.api.mockImplementation((path: string) => {
      if (path === "/groups/100") {
        return Promise.resolve({ data: { groupId: "100", groupName: "测试群", memberCount: 42 }, meta: {} });
      }
      if (path.startsWith("/agent/groups/100/relations?")) {
        return Promise.resolve({
          data: [{ id: "r1", subjectUserId: "1", subjectName: "甲", objectUserId: "2", objectName: "乙", type: "好友", note: "常聊天", confidence: 0.9, lastSeenAt: null }],
          meta: { total: 1 },
        });
      }
      if (path === "/agent/groups/100/relations/summary") {
        return Promise.resolve({
          data: { edgeCount: 1, linkedMemberCount: 2, typeCounts: [{ type: "好友", count: 1 }], lastSeenAt: null },
          meta: {},
        });
      }
      if (path === "/agent/groups/100/relations/graph") {
        return Promise.resolve({
          data: {
            nodes: [
              { userId: "1", nickname: "甲", groupNickname: null, role: "member", linked: true, degree: 1 },
              { userId: "2", nickname: "乙", groupNickname: null, role: "member", linked: true, degree: 1 },
            ],
            edges: [{ id: "r1", subjectUserId: "1", objectUserId: "2", type: "好友", note: "常聊天", confidence: 0.9, lastSeenAt: null }],
            meta: { relationTruncated: false },
          },
          meta: {},
        });
      }
      if (path === "/agent/groups/100/relations/types") {
        return Promise.resolve({ data: ["好友"], meta: {} });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });

    render(
      <MemoryRouter initialEntries={["/guest/100?tab=relations"]}>
        <Routes>
          <Route path="guest/:groupId" element={<GuestGroupPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("常聊天")).toBeInTheDocument();
    expect(screen.getByText("甲")).toBeInTheDocument();
    expect(screen.getByText("乙")).toBeInTheDocument();
    expect(screen.queryByText("新增关系边")).not.toBeInTheDocument();
    expect(screen.queryByText("来源")).not.toBeInTheDocument();
    expect(screen.queryByText("证据数")).not.toBeInTheDocument();
    expect(screen.queryByText("编辑")).not.toBeInTheDocument();
    expect(screen.queryByText("删除")).not.toBeInTheDocument();

    let paths = state.api.mock.calls.map(([path]) => String(path));
    expect(paths).toContain("/agent/groups/100/relations/summary");
    expect(paths).not.toContain("/agent/groups/100/relations/graph");

    fireEvent.click(screen.getByText("图谱视图"));
    expect(await screen.findByText("relation graph")).toBeInTheDocument();
    paths = state.api.mock.calls.map(([path]) => String(path));
    expect(paths).toContain("/agent/groups/100/relations/graph");
  });

  it("only recognizes the three guest tabs", () => {
    expect(isGuestTabAllowed(null)).toBe(true);
    expect(isGuestTabAllowed("memories")).toBe(true);
    expect(isGuestTabAllowed("profiles")).toBe(true);
    expect(isGuestTabAllowed("relations")).toBe(true);
    expect(isGuestTabAllowed("config")).toBe(false);
    expect(isGuestTabAllowed("debug")).toBe(false);
  });
});
