import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  getCsrfToken: () => "csrf",
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { WebSearchPage } from "./WebSearchPage";
import type { WebSearchSettings, WebSearchStatus } from "./types";

const settings: WebSearchSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  adminOnly: false,
  memberDailyCalls: 30,
  groupDailyCalls: 200,
  globalDailyCalls: 1000,
  memberCooldownSeconds: 3,
  maxCallsPerTurn: 6,
  defaultCount: 5,
  maxCount: 10,
  allowFullContent: false,
  fetchEnabled: true,
  maxFetchChars: 6000,
  cacheTtlMinutes: 30,
  blockedDomains: [],
};

function status(overrides: Partial<WebSearchStatus> = {}): WebSearchStatus {
  return {
    state: "ready",
    configured: true,
    keySource: "AGENT_WEB_SEARCH_API_KEY",
    keyMasked: "sk-…abcd",
    baseUrl: "https://api.deepseek.com",
    settings,
    defaults: settings,
    usage: {
      day: "2026-10-26",
      totals: { search: 3, fetch: 1, billed: 3, empty: 1, failed: 0, cached: 2 },
      billed: 3,
      costYuan: 0.015,
      pricePerCallYuan: 0.005,
      globalLimit: 1000,
      globalLeft: 997,
      groups: [{ groupId: "123456", calls: 3, groupName: "测试群" }],
      members: [],
      groupLimit: 200,
      memberLimit: 30,
    },
    cache: { search: { size: 2, hits: 2, misses: 3 }, fetch: { size: 1, hits: 0, misses: 1 } },
    upstream: { calls: 4, failures: 0, avgMs: 820, lastError: "", lastErrorAt: null, lastOkAt: null, byStatus: { "200": 4 } },
    ...overrides,
  };
}

function renderPage(path = "/web-search") {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <WebSearchPage />
      </MemoryRouter>
    </AntApp>,
  );
}

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

describe("WebSearchPage", () => {
  beforeAll(() => {
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
  });

  beforeEach(() => {
    state.api.mockReset();
  });

  it("shows status, masked key and today's cost", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path.startsWith("/web-search/status")) return { data: status() };
      throw new Error(`unexpected ${path}`);
    });
    renderPage();
    expect(await screen.findByText("网页搜索 · 可用")).toBeInTheDocument();
    expect(screen.getByText("密钥 sk-…abcd")).toBeInTheDocument();
    expect(screen.getByText("约 ¥0.015")).toBeInTheDocument();
    expect(screen.getByText("测试群（123456）")).toBeInTheDocument();
  });

  it("explains how to configure a missing key", async () => {
    state.api.mockResolvedValue({ data: status({ state: "no_key", configured: false, keySource: null, keyMasked: null }) });
    renderPage();
    expect(await screen.findByText("还没有配置 DeepSeek 密钥")).toBeInTheDocument();
    expect(screen.getByText("无密钥")).toBeInTheDocument();
  });

  it("runs a test search and renders ranked hits", async () => {
    state.api.mockImplementation(async (path: string, init?: { body?: string }) => {
      if (path.startsWith("/web-search/status")) return { data: status() };
      if (path === "/web-search/test") {
        expect(JSON.parse(init?.body ?? "{}")).toMatchObject({ query: "deepseek v4", count: 5 });
        return {
          data: {
            query: "deepseek v4",
            durationMs: 900,
            attempts: 1,
            billed: true,
            results: [{ title: "DeepSeek V4 发布", url: "https://example.com/a", site: "example.com", date: "2026-10-01", summary: "摘要", score: 2.5, authority: 3 }],
          },
        };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/web-search?web.tab=test");
    const input = await screen.findByPlaceholderText("关键词，例如：DeepSeek V4 发布");
    fireEvent.change(input, { target: { value: "deepseek v4" } });
    fireEvent.click(screen.getByRole("button", { name: /搜\s?索/ }));
    expect(await screen.findByText("DeepSeek V4 发布")).toBeInTheDocument();
    expect(screen.getByText("高")).toBeInTheDocument();
  });

  it("saves policy changes", async () => {
    const saved: WebSearchSettings[] = [];
    state.api.mockImplementation(async (path: string, init?: { method?: string; body?: string }) => {
      if (path.startsWith("/web-search/status")) return { data: status() };
      if (path === "/web-search/settings" && init?.method === "PUT") {
        const body = JSON.parse(init.body ?? "{}") as WebSearchSettings;
        saved.push(body);
        return { data: { settings: body } };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/web-search?web.tab=policy");
    const toggle = await screen.findByRole("switch", { name: "仅群主和管理员可用" });
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0].adminOnly).toBe(true);
  });
});
