import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

beforeAll(() => vi.stubGlobal("ResizeObserver", ResizeObserverMock));
afterAll(() => vi.unstubAllGlobals());

const panelLoads = vi.hoisted(() => ({
  config: 0,
  persona: 0,
  memories: 0,
  profiles: 0,
  relations: 0,
  messages: 0,
  debug: 0,
  privacy: 0,
  audits: 0,
}));
const apiMock = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status = 500;
    fields = {};
  },
}));
vi.mock("./agent-panels/AgentConfigPanel", () => {
  panelLoads.config += 1;
  return { AgentConfigPanel: () => <input aria-label="config draft" defaultValue="draft" /> };
});
vi.mock("./agent-panels/PersonaPanel", () => {
  panelLoads.persona += 1;
  return { PersonaPanel: () => <div>persona mock</div> };
});
vi.mock("./agent-panels/MemoriesPanel", () => {
  panelLoads.memories += 1;
  return { MemoriesPanel: () => <div>memories mock</div> };
});
vi.mock("./agent-panels/MemberProfilesPanel", () => {
  panelLoads.profiles += 1;
  return { MemberProfilesPanel: () => <div>profiles mock</div> };
});
vi.mock("./agent-panels/RelationsPanel", () => {
  panelLoads.relations += 1;
  return { RelationsPanel: () => <div>relations mock</div> };
});
vi.mock("./agent-panels/AgentMessagesPanel", () => {
  panelLoads.messages += 1;
  return { AgentMessagesPanel: () => <div>messages mock</div> };
});
vi.mock("./agent-panels/PrivacyPanel", () => {
  panelLoads.privacy += 1;
  return { PrivacyPanel: () => <div>privacy mock</div> };
});
vi.mock("./agent-panels/AgentAuditsPanel", () => {
  panelLoads.audits += 1;
  return { AgentAuditsPanel: () => <div>audits mock</div> };
});
vi.mock("./agent-debug/AgentDebugger", () => {
  panelLoads.debug += 1;
  return { AgentDebugger: () => <div>debug mock</div> };
});

import { AgentDetailPage } from "./agent";

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <output aria-label="location">{location.pathname}{location.search}</output>;
}

function renderPage(entry: string): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/agent/:groupId" element={<><AgentDetailPage /><LocationProbe /></>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation((path: string) => {
    if (path === "/groups/1") {
      return Promise.resolve({
        data: { groupId: "1", groupName: "测试群", memberCount: 12 },
        meta: {},
      });
    }
    return new Promise<never>(() => undefined);
  });
});

describe("AgentDetailPage information architecture", () => {
  it("shows four top-level groups, the group name and number, and keeps Studio panels lazy", async () => {
    renderPage("/agent/1");
    await screen.findByText("Agent · 测试群（1）");
    expect(screen.getByText("运行")).toBeTruthy();
    expect(screen.getByText("知识")).toBeTruthy();
    expect(screen.getByText("调试")).toBeTruthy();
    expect(screen.getByText("治理")).toBeTruthy();
    expect(screen.getByText("诊断")).toBeTruthy();
    expect(panelLoads).toEqual({
      config: 0,
      persona: 0,
      memories: 0,
      profiles: 0,
      relations: 0,
      messages: 0,
      debug: 0,
      privacy: 0,
      audits: 0,
    });

    fireEvent.click(screen.getByText("配置"));
    await screen.findByLabelText("config draft");
    expect(panelLoads.config).toBe(1);
    expect(panelLoads.persona).toBe(0);
    expect(panelLoads.debug).toBe(0);
  });

  it("keeps the last leaf in each group mounted and preserves namespaced URL state", async () => {
    renderPage("/agent/1?tab=config&profiles.userId=42&debug.trace=trace-a");
    const input = await screen.findByLabelText("config draft");
    fireEvent.change(input, { target: { value: "unsaved local draft" } });

    fireEvent.click(screen.getByText("知识"));
    await screen.findByText("memories mock");
    expect(screen.getByLabelText("location").textContent).toContain("tab=memories");
    expect(screen.getByLabelText("location").textContent).toContain("profiles.userId=42");
    expect(screen.getByLabelText("location").textContent).toContain("debug.trace=trace-a");

    fireEvent.click(screen.getByText("运行"));
    await screen.findByLabelText("config draft");
    expect(screen.getByLabelText("location").textContent).toContain("tab=config");
    expect((screen.getByLabelText("config draft") as HTMLInputElement).value).toBe("unsaved local draft");

    fireEvent.click(screen.getByText("人设"));
    await screen.findByText("persona mock");
    fireEvent.click(screen.getByText("知识"));
    await screen.findByText("memories mock");
    fireEvent.click(screen.getByText("运行"));
    await screen.findByText("persona mock");
  });

  it("maps legacy leaf deep links into the correct top-level group", async () => {
    renderPage("/agent/1?tab=relations&relations.view=graph");
    await screen.findByText("relations mock");
    expect(screen.getByText("知识").closest(".ant-tabs-tab")?.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("location").textContent).toContain("tab=relations");
    expect(screen.getByLabelText("location").textContent).toContain("relations.view=graph");
  });

  it("normalizes an invalid tab to overview without deleting panel-specific params", async () => {
    renderPage("/agent/1?tab=not-a-tab&debug.trace=trace-a&profiles.userId=42");
    await waitFor(() => {
      const location = screen.getByLabelText("location").textContent ?? "";
      expect(location).not.toContain("tab=not-a-tab");
      expect(location).toContain("debug.trace=trace-a");
      expect(location).toContain("profiles.userId=42");
    });
  });
});