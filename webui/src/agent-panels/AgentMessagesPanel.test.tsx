import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentMessageItem } from "../types";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ api: apiMock }));

import { AgentMessagesPanel } from "./AgentMessagesPanel";

function row(id: number): AgentMessageItem {
  return {
    id: String(id),
    messageId: String(1000 + id),
    groupId: "100",
    userId: String(2000 + id),
    senderName: `成员${id}`,
    role: "member",
    title: null,
    text: `第 ${id} 页消息`,
    receivedAt: "2026-09-03T12:00:00Z",
    expiresAt: null,
  };
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

afterAll(() => vi.unstubAllGlobals());
beforeEach(() => apiMock.mockReset());

describe("AgentMessagesPanel query lifecycle", () => {
  it("keeps previous rows non-actionable while paging and aborts the obsolete slow page request", async () => {
    let page2Aborted = false;
    let page2Started = false;

    apiMock.mockImplementation((path?: string, init?: RequestInit) => {
      if (typeof path !== "string") return new Promise<never>(() => undefined);
      if (path.includes("page=1")) {
        return Promise.resolve({ data: [row(1)], meta: { total: 60 } });
      }
      if (path.includes("page=2")) {
        page2Started = true;
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            page2Aborted = true;
            reject(new DOMException("aborted", "AbortError"));
          });
        });
      }
      if (path.includes("page=3")) {
        return Promise.resolve({ data: [row(3)], meta: { total: 60 } });
      }
      return new Promise<never>(() => undefined);
    });

    render(<MemoryRouter><AgentMessagesPanel groupId="100" /></MemoryRouter>);
    await screen.findByText("第 1 页消息");
    expect(screen.getByRole("button", { name: "调试" })).not.toBeDisabled();

    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(page2Started).toBe(true));

    expect(screen.getByText("第 1 页消息")).toBeTruthy();
    expect(screen.getByRole("button", { name: "调试" })).toBeDisabled();

    fireEvent.click(screen.getByTitle("3"));
    await waitFor(() => expect(page2Aborted).toBe(true));
    await screen.findByText("第 3 页消息");

    expect(screen.queryByText("第 1 页消息")).toBeNull();
    expect(screen.getByRole("button", { name: "调试" })).not.toBeDisabled();
  });
});
