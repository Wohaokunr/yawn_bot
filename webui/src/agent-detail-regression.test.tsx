import { App as AntApp } from "antd";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig } from "./types";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({
  api: apiMock,
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

vi.mock("./agent-panels/PersonaPanel", () => ({ PersonaPanel: () => <div>persona mock</div> }));
vi.mock("./agent-panels/MemoriesPanel", () => ({ MemoriesPanel: () => <div>memories mock</div> }));
vi.mock("./agent-panels/MemberProfilesPanel", () => ({ MemberProfilesPanel: () => <div>profiles mock</div> }));
vi.mock("./agent-panels/RelationsPanel", () => ({ RelationsPanel: () => <div>relations mock</div> }));
vi.mock("./agent-panels/AgentMessagesPanel", () => ({ AgentMessagesPanel: () => <div>messages mock</div> }));
vi.mock("./agent-panels/PrivacyPanel", () => ({ PrivacyPanel: () => <div>privacy mock</div> }));
vi.mock("./agent-panels/AgentAuditsPanel", () => ({ AgentAuditsPanel: () => <div>audit mock</div> }));

vi.mock("./agent-debug/useExecutionTraces", () => ({
  TRACE_POLL_OPTIONS: [3000],
  useExecutionTraces: () => ({
    summaries: [],
    visibleSummaries: [],
    stats: null,
    statsError: "",
    selectedTrace: null,
    detailLoading: false,
    detailError: "",
    starred: [],
    autoRefresh: true,
    setAutoRefresh: vi.fn(),
    reloadSelected: vi.fn(),
    selectRelative: vi.fn(),
    clearBuffer: vi.fn(),
  }),
}));
vi.mock("./agent-debug/TraceNavigator", () => ({ TraceNavigator: () => <div>trace navigator</div> }));
vi.mock("./agent-debug/TraceInspector", () => ({ TraceInspector: () => <div>trace inspector</div> }));
vi.mock("./agent-debug/SimulationLab", () => ({
  normalizeMode: (value: string) => value,
  SimulationLab: ({ runs }: {
    runs: {
      add: (run: { input: unknown; result: { promptVersion: string }; elapsedMs: number }) => { id: string };
      active: { result: { promptVersion: string } } | null;
      baseline: { result: { promptVersion: string } } | null;
      setBaseline: (id: string) => void;
      activeId: string;
    };
  }) => (
    <div>
      <button onClick={() => runs.add({ input: {}, result: { promptVersion: "debug-v1" }, elapsedMs: 1 })}>生成测试结果</button>
      <output aria-label="debug-result">{runs.active?.result.promptVersion ?? "none"}</output>
      <output aria-label="debug-baseline">{runs.baseline?.result.promptVersion ?? "none"}</output>
      {runs.active && <button onClick={() => runs.setBaseline(runs.activeId)}>设为基准 A</button>}
    </div>
  ),
}));

import { AgentDetailPage } from "./agent";

const config: AgentConfig = {
  groupId: "1",
  enabled: true,
  replyTriggerEnabled: true,
  explicitWakeupEnabled: true,
  proactiveEnabled: true,
  proactiveProbability: 0.35,
  proactiveActiveEnabled: true,
  shortConversationEnabled: true,
  proactiveActiveProbability: 0.25,
  proactiveActiveWindowMinutes: 10,
  idleThresholdMinutes: 30,
  cooldownMinutes: 15,
  dailyLimit: 5,
  rawRetentionDays: 30,
  crossGroupVisibility: "isolated",
  mediaCacheEnabled: true,
  adminToolDailyLimit: 10,
  criticalToolDailyLimit: 2,
  toolAllowlist: [],
  proactiveToday: 1,
  adminToolsToday: 0,
  criticalToolsToday: 0,
  version: "v1",
};

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

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation((path: string) => {
    if (path === "/groups/1") {
      return Promise.resolve({ data: { groupId: "1", groupName: "生命周期群", memberCount: 12 }, meta: {} });
    }
    if (path === "/agent/groups/1/config") {
      return Promise.resolve({ data: config, meta: {} });
    }
    return new Promise<never>(() => undefined);
  });
});

function renderPage(): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={["/agent/1?tab=config"]}>
        <Routes>
          <Route path="/agent/:groupId" element={<AgentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

describe("AgentDetailPage state retention", () => {
  it("preserves both the real config draft and the lab run history across top-level tab round trips", async () => {
    renderPage();

    const dailyLimit = await screen.findByLabelText("每日参与上限");
    fireEvent.change(dailyLimit, { target: { value: "9" } });
    await screen.findByText("有未保存的修改");

    fireEvent.click(screen.getByRole("tab", { name: "调试台" }));
    // 调试面板首次切换时才懒加载，冷启动 CI 下可能超过 findBy 默认的 1s。
    fireEvent.click(await screen.findByText("模拟实验室", undefined, { timeout: 5_000 }));
    fireEvent.click(await screen.findByRole("button", { name: "生成测试结果" }));
    expect(screen.getByLabelText("debug-result").textContent).toBe("debug-v1");
    fireEvent.click(screen.getByRole("button", { name: "设为基准 A" }));
    expect(screen.getByLabelText("debug-baseline").textContent).toBe("debug-v1");

    fireEvent.click(screen.getByRole("tab", { name: "运行配置" }));
    const restoredLimit = await screen.findByLabelText("每日参与上限");
    expect((restoredLimit as HTMLInputElement).value).toBe("9");
    expect(screen.getByText("有未保存的修改")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "调试台" }));
    expect((await screen.findByLabelText("debug-baseline")).textContent).toBe("debug-v1");
  });
});
