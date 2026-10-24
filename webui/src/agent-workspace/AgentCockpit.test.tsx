import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentActivity, AgentDiagnostics, AgentExecutionTraceSummary } from "../types";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("../api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status = 500;
    fields = {};
  },
}));

import { AgentDetailPage } from "../agent";
import { runtimeState } from "./AgentStatusBar";
import { formatDuration, relativeTime } from "./polling";

function diagnostics(overrides: Partial<AgentDiagnostics["effective"]> = {}, blockers: AgentDiagnostics["blockers"] = []): AgentDiagnostics {
  return {
    groupId: "1",
    tools: { adminToday: 3, adminLimit: 10, criticalToday: 0, criticalLimit: 2, allowlist: ["mute_member"] },
    pipeline: {
      callContinuationSeconds: 6,
      coalesceWindowSeconds: 20,
      maxCoalescedMessages: 4,
      conversationDirectQuietSeconds: 8,
      conversationQuietSeconds: 20,
      conversationMaxSeconds: 720,
      conversationMaxBotTurns: 4,
    },
    effective: {
      enabled: true,
      replyTriggerEnabled: true,
      explicitWakeupEnabled: true,
      proactiveEnabled: true,
      proactiveActiveEnabled: true,
      proactiveToday: 2,
      dailyLimit: 30,
      dailyRemaining: 28,
      cooldownMinutes: 8,
      cooldownRemainingMinutes: 0,
      lastAgentAt: null,
      lastProactiveAt: null,
      activeTopic: "周末去哪玩",
      mediaCacheEnabled: false,
      idleThresholdMinutes: 15,
      proactiveProbability: 0.35,
      proactiveActiveProbability: 0.25,
      proactiveActiveWindowMinutes: 12,
      shortConversation: {
        enabled: true,
        active: true,
        sessionId: 7,
        topic: "猫粮",
        botTurns: 2,
        evaluations: 1,
        consecutiveWaits: 0,
      },
      ...overrides,
    },
    memory: {
      groupId: "1",
      runtimeEnabled: true,
      pendingMessages: 5,
      lastCompactedMessageId: null,
      lastCompactedAt: null,
      countsByType: {},
      total: 0,
      oldestUpdatedAt: null,
      newestUpdatedAt: null,
      rebuildRequired: false,
      lastAttemptAt: null,
      lastSuccessAt: null,
      lastError: null,
      consecutiveFailures: 0,
      inFlight: false,
    },
    llm: { routes: [], unconfiguredProviders: [] },
    blockers,
    generatedAt: "2026-10-22T12:00:00+08:00",
  };
}

const activity: AgentActivity = {
  groupId: "1",
  hours: 24,
  buckets: Array.from({ length: 24 }, (_, index) => ({
    hour: new Date(Date.UTC(2026, 9, 22, index)).toISOString(),
    members: index % 3,
    bot: index % 5 === 0 ? 1 : 0,
  })),
  totals: { members: 23, bot: 5, activeMembers: 4 },
  topMembers: [{ userId: "42", name: "阿喵", count: 9 }],
  generatedAt: "2026-10-22T12:00:00+08:00",
};

const traces: AgentExecutionTraceSummary[] = [
  {
    traceId: "t-1", groupId: "1", mode: "dialogue", source: "runtime", triggerSource: "mention",
    actorUserId: "42", messageId: "1", startedAt: new Date().toISOString(), status: "completed",
    outcome: "speak", durationMs: 1800, eventCount: 6, hasFailure: false, hasDegradation: false,
    hasTool: true, hasMedia: false, hasOutboundProblem: false,
  },
  {
    traceId: "t-2", groupId: "1", mode: "dialogue", source: "runtime", triggerSource: "wake_word",
    actorUserId: "43", messageId: "2", startedAt: new Date().toISOString(), status: "failed",
    outcome: "error", durationMs: 4200, eventCount: 3, hasFailure: true, hasDegradation: false,
    hasTool: false, hasMedia: false, hasOutboundProblem: false,
  },
];

let currentDiagnostics = diagnostics();

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <output aria-label="location">{location.pathname}{location.search}</output>;
}

function renderPage(entry = "/agent/1"): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/agent/:groupId" element={<><AgentDetailPage /><LocationProbe /></>} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
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

beforeEach(() => {
  currentDiagnostics = diagnostics();
  apiMock.mockReset();
  apiMock.mockImplementation((path: string) => {
    if (path === "/groups/1") return Promise.resolve({ data: { groupId: "1", groupName: "猫窝", memberCount: 9 }, meta: {} });
    if (path === "/agent/groups/1/diagnostics") return Promise.resolve({ data: currentDiagnostics, meta: {} });
    if (path.startsWith("/agent/groups/1/activity")) return Promise.resolve({ data: activity, meta: {} });
    if (path === "/agent/groups/1/execution-traces") return Promise.resolve({ data: traces, meta: {} });
    if (path.startsWith("/agent/groups/1/tool-usage")) {
      return Promise.resolve({
        data: {
          groupId: "1", days: 7, total: 3, failed: 1,
          tools: [{ toolName: "mute_member", success: 2, failed: 1, total: 3 }],
          daily: Array.from({ length: 7 }, (_, index) => ({ day: `2026-10-${16 + index}`, success: index === 6 ? 2 : 0, failed: index === 5 ? 1 : 0 })),
        },
        meta: {},
      });
    }
    if (path.startsWith("/agent/audits")) {
      return Promise.resolve({ data: [{ id: "9", groupId: "1", actorUserId: "42", traceId: "t-1", toolName: "mute_member", arguments: {}, result: "success", createdAt: new Date().toISOString() }], meta: { total: 1 } });
    }
    if (path === "/agent/groups/1/conversation/close") return Promise.resolve({ data: { groupId: "1", closed: 1 }, meta: {} });
    return new Promise<never>(() => undefined);
  });
});

describe("Agent workspace status bar", () => {
  it("shows runtime state, quota meters and the live short conversation", async () => {
    renderPage();
    expect(await screen.findByText("运行中")).toBeTruthy();
    expect(screen.getByLabelText("特权工具 3/10")).toBeTruthy();
    expect(screen.getByLabelText("主动发言 2/30")).toBeTruthy();
    expect(screen.getByText("短会话进行中")).toBeTruthy();
    expect(screen.getByRole("switch", { name: "Agent 总开关" }).getAttribute("aria-checked")).toBe("true");
  });

  it("ends the short conversation through the new endpoint", async () => {
    renderPage();
    const bar = (await screen.findByText("短会话进行中")).closest(".agent-status-bar") as HTMLElement;
    fireEvent.click(within(bar).getByRole("button", { name: /^结\s*束$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s*定|OK/ }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/agent/groups/1/conversation/close", { method: "POST" }));
  });

  it("derives a paused or blocked state", () => {
    expect(runtimeState(diagnostics({ enabled: false })).label).toBe("已暂停");
    const blocked = runtimeState(diagnostics({}, [{ code: "bot_offline", severity: "error", title: "Bot 当前离线", detail: "" }]));
    expect(blocked).toMatchObject({ tone: "error", label: "存在阻塞", detail: "Bot 当前离线" });
  });
});

describe("Agent cockpit", () => {
  it("renders activity, execution pulse and dialogue rhythm", async () => {
    renderPage();
    expect(await screen.findByText("群聊活跃度")).toBeTruthy();
    expect(await screen.findByRole("img", { name: /成员消息 23 条，Bot 回复 5 条/ })).toBeTruthy();
    expect(await screen.findByText("50%")).toBeTruthy();
    expect(screen.getByText("只 @ 不说话时等 6s 补正文")).toBeTruthy();
    expect(screen.getByText("冷场 15 分钟后以 35% 概率暖场")).toBeTruthy();
  });

  it("explains why idle warmup is paused", async () => {
    currentDiagnostics = diagnostics({
      warmup: {
        lastHumanAt: "2026-10-22T10:00:00",
        lastBotAt: "2026-10-22T10:45:00",
        unanswered: 2,
        skips: 0,
        lastSkipAt: null,
        closed: false,
        warmupsToday: 2,
        recentTopics: ["流星雨"],
        dailyCap: 4,
        paused: true,
        gate: {
          allowed: false,
          code: "unanswered_cap",
          reason: "已连续主动说了 2 条没人接，等真人开口后再说",
          idleMinutes: 90,
          requiredIdleMinutes: 0,
          maxUnanswered: 2,
          nextEligibleAt: null,
        },
      },
    });
    currentDiagnostics.pipeline = { ...currentDiagnostics.pipeline!, warmupMaxUnanswered: 2, warmupQuietHours: [1, 8] };
    renderPage();
    expect(await screen.findByText("连续 2 条没人接就停到有人说话 · 1–8 点不暖场")).toBeTruthy();
    expect(screen.getByText("本段冷场已说 2 条没人接 · 今日暖场 2/4")).toBeTruthy();
    expect(screen.getByText("暖场门槛：已连续主动说了 2 条没人接，等真人开口后再说")).toBeTruthy();
  });

  it("jumps from a trace to the debugger and from a member to their profile", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /阿喵/ }));
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("profiles.userId=42"));
    expect(screen.getByLabelText("location").textContent).toContain("tab=profiles");
  });

  it("flags the overview nav item when a blocker is an error", async () => {
    currentDiagnostics = diagnostics({}, [{ code: "bot_offline", severity: "error", title: "Bot 当前离线", detail: "" }]);
    renderPage();
    expect(await screen.findByLabelText("有阻塞")).toBeTruthy();
  });
});

describe("Agent cockpit power tools", () => {
  it("filters the execution pulse down to failures", async () => {
    renderPage();
    await screen.findByText("执行脉搏");
    const pulse = screen.getByText("执行脉搏").closest(".ant-card") as HTMLElement;
    await within(pulse).findByText("@ 明确呼叫");
    const filter = pulse.querySelector(".agent-pulse-filter") as HTMLElement;
    fireEvent.click(within(filter).getByText("失败"));
    await waitFor(() => expect(within(pulse).queryByText("@ 明确呼叫")).toBeNull());
    expect(within(pulse).getByText("叫名 / 唤醒词")).toBeTruthy();
  });

  it("shows tool usage, the health score and copies a report", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPage();
    expect(await screen.findByLabelText("健康分 100")).toBeTruthy();
    expect(await screen.findByLabelText("最近 7 天共调用 3 次，失败 1 次")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /复制诊断报告/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(String((writeText.mock.calls[0] as unknown[])[0])).toContain("猫窝（1）");
  });

  it("switches panels with Alt+digit shortcuts", async () => {
    renderPage();
    await screen.findByText("运行中");
    fireEvent.keyDown(window, { key: "4", code: "Digit4", altKey: true });
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("tab=memories"));
    fireEvent.keyDown(window, { key: "0", code: "Digit0", altKey: true });
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("tab=audit"));
  });

  it("can turn auto refresh off", async () => {
    renderPage();
    const toggle = await screen.findByRole("switch", { name: "自动刷新" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
  });
});

describe("workspace helpers", () => {
  it("formats durations and relative times", () => {
    expect(formatDuration(420)).toBe("420ms");
    expect(formatDuration(1800)).toBe("1.8s");
    expect(formatDuration(null)).toBe("—");
    const now = Date.parse("2026-10-22T12:00:00Z");
    expect(relativeTime("2026-10-22T11:59:50Z", now)).toBe("刚刚");
    expect(relativeTime("2026-10-22T11:30:00Z", now)).toBe("30 分钟前");
    expect(relativeTime("2026-10-22T09:00:00Z", now)).toBe("3 小时前");
    expect(relativeTime(null, now)).toBe("—");
  });
});
