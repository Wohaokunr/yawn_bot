import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  getCsrfToken: () => "csrf",
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

import { SandboxPage } from "./SandboxPage";
import type { SandboxSettings, SandboxStatus } from "./types";

const settings: SandboxSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  adminOnly: false,
  memberDailyRuns: 20,
  groupDailyRuns: 120,
  memberCooldownSeconds: 5,
  timeoutSeconds: 30,
  memoryMb: 1024,
  diskQuotaMb: 200,
  maxOutputChars: 3000,
  maxShareMb: 20,
  allowUnisolated: false,
};

function status(overrides: Partial<SandboxStatus> = {}): SandboxStatus {
  return {
    state: "ready",
    online: true,
    configured: true,
    transport: "unix",
    error: null,
    health: {
      ok: true,
      version: "1.0.0",
      api: 1,
      python: "3.12.8",
      packages: [{ name: "numpy", version: "2.1.3" }, { name: "pandas", version: "2.2.3" }],
      packageCount: 58,
      isolation: {
        isolated: true,
        checks: { networkDisabled: true, privilegeDrop: true, readonlyRoot: true, noNewPrivileges: true },
        networkInterfaces: [],
      },
      acceptingRuns: true,
      ceilings: { timeoutSeconds: 90 },
      concurrency: 2,
      queueLimit: 8,
      running: 0,
      waiting: 0,
      retentionDays: 14,
      maxUploadMb: 20,
    },
    settings,
    usage: { day: "2026-10-24", totalRuns: 7, groups: [{ groupId: "123456", runs: 7 }], members: [], groupLimit: 120, memberLimit: 20 },
    ...overrides,
  };
}

const stats = {
  resources: {
    memory: { current: 200 * 1024 * 1024, max: 1536 * 1024 * 1024, peak: 400 * 1024 * 1024 },
    pids: { current: 3, max: 256 },
    cpu: { usageUsec: 1, quotaCores: 1.5, count: 4, load: [0.1, 0.2, 0.3] },
    disk: { total: 10, used: 5, free: 5 },
    uptimeSeconds: 7200,
  },
  workspaces: { count: 2, bytes: 4096, files: 3 },
  runs: {
    windowSeconds: 86400,
    total: 4,
    byStatus: { ok: 3, error: 1 },
    byWorkspace: { g123456: 4 },
    durationP50Ms: 900,
    durationP95Ms: 2400,
    hourly: Array.from({ length: 24 }, (_, index) => (index === 23 ? 4 : 0)),
    hourlyErrors: Array.from({ length: 24 }, (_, index) => (index === 23 ? 1 : 0)),
  },
  active: [],
  lastPrune: null,
  topWorkspaces: [{ workspace: "g123456", runs: 4, groupId: "123456", groupName: "摸鱼群" }],
};

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

function renderPage(search = ""): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[`/sandbox${search}`]}>
        <LocationProbe />
        <Routes>
          <Route path="sandbox" element={<SandboxPage />} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

describe("SandboxPage", () => {
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
  });

  it("在线时显示状态条、隔离标签和概览统计", async () => {
    state.api.mockImplementation((path: string) => {
      if (path.startsWith("/sandbox/status")) return Promise.resolve({ data: status(), meta: {} });
      if (path === "/sandbox/stats") return Promise.resolve({ data: stats, meta: {} });
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    renderPage();
    expect(await screen.findByText("代码沙箱 · 运行中")).toBeTruthy();
    expect(screen.getAllByText("无网络").length).toBeGreaterThan(0);
    expect(screen.getByText("独立用户")).toBeTruthy();
    expect(await screen.findByText("75%")).toBeTruthy();
    expect(screen.getByText("摸鱼群（123456）")).toBeTruthy();
  });

  it("离线时降级展示而不是报错，并能重新检测", async () => {
    state.api.mockImplementation((path: string) => {
      if (path.startsWith("/sandbox/status")) {
        return Promise.resolve({
          data: status({ state: "offline", online: false, health: null, error: { code: "offline", message: "代码沙箱未运行或无法连接" } }),
          meta: {},
        });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    renderPage();
    expect(await screen.findByText("代码沙箱 · 离线")).toBeTruthy();
    expect(screen.getByText("代码沙箱未运行或无法连接")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /重新检测/ }));
    await waitFor(() => expect(state.api.mock.calls.some(([path]) => path === "/sandbox/status?refresh=true")).toBe(true));
    expect(state.api.mock.calls.some(([path]) => path === "/sandbox/stats")).toBe(false);
  });

  it("控制台运行代码并展示输出与产物", async () => {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path.startsWith("/sandbox/status")) return Promise.resolve({ data: status(), meta: {} });
      if (path === "/sandbox/workspaces") return Promise.resolve({ data: { items: [] }, meta: {} });
      if (path === "/sandbox/console/run") {
        const body = JSON.parse(String(init?.body)) as { code: string; workspace: string };
        expect(body.workspace).toBe("admin");
        expect(body.code).toContain("print");
        return Promise.resolve({
          data: {
            runId: "r_abcdef12",
            workspace: "admin",
            status: "ok",
            exitCode: 0,
            signal: null,
            stdout: "hello sandbox\n",
            stderr: "",
            stdoutTruncated: false,
            stderrTruncated: false,
            startedAt: 1,
            durationMs: 321,
            queueMs: 0,
            cpuMs: 200,
            maxRssKb: 30_000,
            files: [{ path: "figure-1.png", size: 2048, kind: "image", created: true }],
            filesTruncated: false,
            message: "",
            limits: {},
            usage: { bytes: 2048, files: 1 },
          },
          meta: {},
        });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    renderPage("?sandbox.tab=console");
    const editor = await screen.findByLabelText("Python 代码");
    fireEvent.change(editor, { target: { value: "print('hello sandbox')" } });
    fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
    expect(await screen.findByText("hello sandbox")).toBeTruthy();
    expect(screen.getByAltText("figure-1.png").getAttribute("src")).toContain("/sandbox/workspaces/admin/file?path=figure-1.png");
  });

  it("策略页校验后才允许保存，保存提交 camelCase 设置", async () => {
    let saved: SandboxSettings | null = null;
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path.startsWith("/sandbox/status")) return Promise.resolve({ data: status(), meta: {} });
      if (path === "/sandbox/settings" && init?.method === "PUT") {
        saved = JSON.parse(String(init.body)) as SandboxSettings;
        return Promise.resolve({ data: { settings: saved }, meta: {} });
      }
      if (path === "/sandbox/settings") return Promise.resolve({ data: { settings, defaults: settings }, meta: {} });
      if (path === "/sandbox/usage") return Promise.resolve({ data: status().usage, meta: {} });
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    renderPage("?sandbox.tab=policy");
    const saveButton = await screen.findByRole("button", { name: /保\s*存/ });
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByText("只开放白名单群"));
    expect(await screen.findByText(/白名单模式下至少填一个群号/)).toBeTruthy();
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    const allowField = screen.getByText("白名单群号").closest("label") as HTMLElement;
    fireEvent.change(within(allowField).getByRole("textbox"), { target: { value: "123456\n654321" } });
    await waitFor(() => expect((saveButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(saveButton);
    await waitFor(() => expect(saved).not.toBeNull());
    expect(saved!.groupMode).toBe("allowlist");
    expect(saved!.allowedGroups).toEqual(["123456", "654321"]);
  });

  it("切换标签写入 sandbox.tab 参数", async () => {
    state.api.mockImplementation((path: string) => {
      if (path.startsWith("/sandbox/status")) return Promise.resolve({ data: status(), meta: {} });
      if (path === "/sandbox/stats") return Promise.resolve({ data: stats, meta: {} });
      if (path.startsWith("/sandbox/runs")) return Promise.resolve({ data: { items: [], active: [], nextBefore: null }, meta: {} });
      if (path === "/sandbox/workspaces") return Promise.resolve({ data: { items: [] }, meta: {} });
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    renderPage();
    fireEvent.click(await screen.findByText("运行历史"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("sandbox.tab=runs"));
    expect(await screen.findByText("还没有人运行过代码")).toBeTruthy();
  });
});
