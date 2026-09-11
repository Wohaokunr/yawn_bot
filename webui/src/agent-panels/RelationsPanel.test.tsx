import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

vi.mock("../relation-graph", () => ({
  RelationGraphView: () => <div>relation graph</div>,
}));

import { RelationsPanel } from "./RelationsPanel";

describe("RelationsPanel drawer focus", () => {
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
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0));
    vi.stubGlobal("cancelAnimationFrame", (id: number) => window.clearTimeout(id));
    state.api.mockReset();
    state.api.mockImplementation((path: string) => {
      if (path.startsWith("/agent/groups/100/relations?")) {
        return Promise.resolve({
          data: [{
            id: "r1",
            groupId: "100",
            subjectUserId: "1",
            objectUserId: "2",
            subjectName: "甲",
            objectName: "乙",
            type: "好友",
            sourceKind: "manual",
            note: "常聊天",
            confidence: 0.9,
            evidenceCount: 1,
            lastSeenAt: null,
          }],
          meta: { total: 1 },
        });
      }
      if (path === "/agent/groups/100/relations/summary") {
        return Promise.resolve({ data: { edgeCount: 1, linkedMemberCount: 2, typeCounts: [{ type: "好友", count: 1 }], lastSeenAt: null }, meta: {} });
      }
      if (path === "/agent/groups/100/relations/types") {
        return Promise.resolve({ data: ["好友"], meta: {} });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
  });

  it("moves focus into the create drawer after opening", async () => {
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/agent/100?tab=relations"]}>
          <RelationsPanel groupId="100" />
        </MemoryRouter>
      </AntApp>,
    );

    expect(await screen.findByText("常聊天")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "新增关系边" }));

    await waitFor(() => {
      const firstInput = document.querySelector<HTMLElement>(".relations-create-drawer input");
      expect(firstInput).not.toBeNull();
      expect(document.activeElement).toBe(firstInput);
    });
  });
});
