import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { afterEach, it, vi } from "vitest";
import { expect } from "vitest";
import { AgentDetailPage } from "./agent";

vi.mock("./agent-panels/AgentConfigPanel", async () => {
  const { useUnsavedChanges } = await import("./shared");
  return { AgentConfigPanel: () => { useUnsavedChanges(true); return <div>config draft</div>; } };
});
vi.mock("./agent-panels/AgentMessagesPanel", () => ({ AgentMessagesPanel: () => <div>messages panel</div> }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("uses legacy tab links, confirms dirty navigation and preserves list filters", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  function Location() { return <div data-testid="location">{useLocation().search}</div>; }
  render(<MemoryRouter initialEntries={["/agent/123?tab=config&page=3&search=hello"]}><Location /><Routes><Route path="agent/:groupId" element={<AgentDetailPage />} /></Routes></MemoryRouter>);
  expect(screen.getByText("config draft")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("menuitem", { name: "消息记录" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByText("config draft")).toBeInTheDocument();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole("menuitem", { name: "消息记录" }));
  await waitFor(() => expect(screen.getByText("messages panel")).toBeInTheDocument());
  expect(screen.getByTestId("location")).toHaveTextContent("tab=messages&page=3&search=hello");
  expect(screen.getByRole("link", { name: "返回 Agent 列表" })).toHaveAttribute("href", "/agent?page=3&search=hello");
});
