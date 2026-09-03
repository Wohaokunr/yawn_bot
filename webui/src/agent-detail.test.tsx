import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

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

vi.mock("./api", () => ({
  api: vi.fn(() => new Promise<never>(() => undefined)),
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

describe("AgentDetailPage tab lifecycle", () => {
  it("keeps Studio panel modules unloaded while the overview is active", async () => {
    renderPage("/agent/1");
    await screen.findByText("运行诊断");
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

    fireEvent.click(screen.getByText("运行配置"));
    await screen.findByLabelText("config draft");
    expect(panelLoads.config).toBe(1);
    expect(panelLoads.persona).toBe(0);
    expect(panelLoads.debug).toBe(0);
  });

  it("keeps a visited panel mounted and preserves namespaced URL state across tab switches", async () => {
    renderPage("/agent/1?tab=config&profiles.userId=42&debug.trace=trace-a");
    const input = await screen.findByLabelText("config draft");
    fireEvent.change(input, { target: { value: "unsaved local draft" } });

    fireEvent.click(screen.getByText("人设"));
    await screen.findByText("persona mock");
    expect(screen.getByLabelText("location").textContent).toContain("tab=persona");
    expect(screen.getByLabelText("location").textContent).toContain("profiles.userId=42");
    expect(screen.getByLabelText("location").textContent).toContain("debug.trace=trace-a");

    fireEvent.click(screen.getByText("运行配置"));
    expect((screen.getByLabelText("config draft") as HTMLInputElement).value).toBe("unsaved local draft");
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
