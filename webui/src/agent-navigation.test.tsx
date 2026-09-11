import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { afterEach, it, vi } from "vitest";
import { expect } from "vitest";
import { AgentDetailPage } from "./agent";

vi.mock("./agent-panels/AgentConfigPanel", async () => {
  const { useUnsavedChanges } = await import("./shared");
  return { AgentConfigPanel: () => { useUnsavedChanges(true); return <div>config draft</div>; } };
});
vi.mock("./agent-panels/MemoriesPanel", () => ({ MemoriesPanel: () => <div>memories panel</div> }));
vi.mock("./agent-panels/AgentMessagesPanel", () => ({ AgentMessagesPanel: () => <div>messages panel</div> }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("uses legacy tab links and preserves drafts and list filters across grouped tabs", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.spyOn(window, "confirm").mockReturnValue(false);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ data: { groupId: "123" }, meta: {} }) }));
  function Location() { return <div data-testid="location">{useLocation().search}</div>; }
  render(<MemoryRouter initialEntries={["/agent/123?tab=config&page=3&search=hello"]}><Location /><Routes><Route path="agent/:groupId" element={<AgentDetailPage />} /></Routes></MemoryRouter>);
  expect(await screen.findByText("config draft")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "知识" }));
  fireEvent.click(screen.getByRole("tab", { name: "消息" }));
  await waitFor(() => expect(screen.getByText("messages panel")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: "运行" }));
  expect(screen.getByText("config draft")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "知识" }));
  expect(screen.getByTestId("location")).toHaveTextContent("tab=messages&page=3&search=hello");
  expect(screen.getByRole("link", { name: "返回 Agent 列表" })).toHaveAttribute("href", "/agent?page=3&search=hello");
});
