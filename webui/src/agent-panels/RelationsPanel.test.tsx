import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

beforeAll(() => vi.stubGlobal("ResizeObserver", ResizeObserverMock));
afterAll(() => vi.unstubAllGlobals());

const apiMock = vi.hoisted(() => vi.fn());
const graphModuleLoads = vi.hoisted(() => ({ count: 0 }));

vi.mock("../api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status = 500;
    fields = {};
  },
}));
vi.mock("../relation-graph", () => {
  graphModuleLoads.count += 1;
  return { RelationGraphView: () => <div>relation graph mock</div> };
});

import { RelationsPanel } from "./RelationsPanel";

function envelope<T>(data: T, meta: Record<string, unknown> = {}) {
  return Promise.resolve({ data, meta });
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation((path: string) => {
    if (path.endsWith("/relations/summary")) {
      return envelope({ relationCount: 0, memberCount: 0, typeCounts: [], lastSeenAt: null });
    }
    if (path.endsWith("/relations/types")) return envelope([]);
    if (path.endsWith("/relations/graph")) {
      return envelope({
        nodes: [],
        edges: [],
        meta: { relationTruncated: false, memberTruncated: false },
      });
    }
    if (path.includes("/relations?")) return envelope([], { total: 0 });
    throw new Error(`unexpected api path: ${path}`);
  });
});

describe("RelationsPanel graph loading", () => {
  it("does not request or import the full graph until graph view is selected", async () => {
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/agent/1?tab=relations"]}>
          <RelationsPanel groupId="1" />
        </MemoryRouter>
      </AntApp>,
    );

    await waitFor(() => {
      expect(apiMock.mock.calls.some(([path]) => String(path).endsWith("/relations/summary"))).toBe(true);
      expect(apiMock.mock.calls.some(([path]) => String(path).includes("/relations?"))).toBe(true);
    });
    expect(apiMock.mock.calls.some(([path]) => String(path).endsWith("/relations/graph"))).toBe(false);
    expect(graphModuleLoads.count).toBe(0);

    fireEvent.click(screen.getByText("图谱视图"));

    await waitFor(() => {
      expect(apiMock.mock.calls.some(([path]) => String(path).endsWith("/relations/graph"))).toBe(true);
      expect(screen.getByText("relation graph mock")).toBeTruthy();
    });
    expect(graphModuleLoads.count).toBe(1);
  });
});
