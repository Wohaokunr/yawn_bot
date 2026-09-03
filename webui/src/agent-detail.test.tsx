import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({
  api: vi.fn(() => new Promise(() => undefined)),
  ApiError: class ApiError extends Error {
    status = 500;
    fields = {};
  },
}));
vi.mock("./agent-panels/AgentConfigPanel", () => ({
  AgentConfigPanel: () => <input aria-label="config draft" defaultValue="draft" />,
}));
vi.mock("./agent-panels/PersonaPanel", () => ({
  PersonaPanel: () => <div>persona mock</div>,
}));
vi.mock("./agent-panels/MemoriesPanel", () => ({
  MemoriesPanel: () => <div>memories mock</div>,
}));
vi.mock("./agent-panels/MemberProfilesPanel", () => ({
  MemberProfilesPanel: () => <div>profiles mock</div>,
}));
vi.mock("./agent-panels/RelationsPanel", () => ({
  RelationsPanel: () => <div>relations mock</div>,
}));
vi.mock("./agent-panels/AgentMessagesPanel", () => ({
  AgentMessagesPanel: () => <div>messages mock</div>,
}));
vi.mock("./agent-panels/PrivacyPanel", () => ({
  PrivacyPanel: () => <div>privacy mock</div>,
}));
vi.mock("./agent-panels/AgentAuditsPanel", () => ({
  AgentAuditsPanel: () => <div>audits mock</div>,
}));
vi.mock("./agent-debug/AgentDebugger", () => ({
  AgentDebugger: () => <div>debug mock</div>,
}));

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
