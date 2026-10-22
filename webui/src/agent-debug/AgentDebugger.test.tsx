import { App as AntApp } from "antd";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentDebugResponse, AgentExecutionTrace, AgentExecutionTraceStats, AgentExecutionTraceSummary } from "../types";

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

import { AgentDebugger } from "./AgentDebugger";

const summaries: AgentExecutionTraceSummary[] = [
  {
    traceId: "trace-new-0001",
    groupId: "1",
    mode: "dialogue",
    source: "runtime",
    triggerSource: "mention",
    actorUserId: "10001",
    messageId: "501",
    startedAt: new Date(Date.now() - 60_000).toISOString(),
    status: "failed",
    outcome: "error",
    durationMs: 9_800,
    eventCount: 3,
    hasFailure: true,
    hasDegradation: false,
    hasTool: true,
    hasMedia: false,
    hasOutboundProblem: false,
    preview: "帮我把刷屏的人禁言一下",
    toolNames: ["mute_member"],
    llmRounds: 1,
  },
  {
    traceId: "trace-old-0002",
    groupId: "1",
    mode: "warmup",
    source: "runtime",
    triggerSource: "proactive_warmup",
    actorUserId: null,
    messageId: null,
    startedAt: new Date(Date.now() - 3_600_000).toISOString(),
    status: "completed",
    outcome: "speak",
    durationMs: 1_200,
    eventCount: 2,
    hasFailure: false,
    hasDegradation: false,
    hasTool: false,
    hasMedia: false,
    hasOutboundProblem: false,
    preview: null,
  },
];

function detail(traceId: string): AgentExecutionTrace {
  const base = summaries.find((item) => item.traceId === traceId) ?? summaries[0];
  return {
    ...base,
    events: traceId === "trace-new-0001"
      ? [
        { id: "e1", phase: "intake", label: "消息归一化完成", status: "success", offsetMs: 3, durationMs: null, input: {}, output: { trigger_source: "mention", text_preview: "帮我把刷屏的人禁言一下" }, detail: null, round: null },
        { id: "e2", phase: "llm", label: "模型调用", status: "success", offsetMs: 9_000, durationMs: 8_900, input: {}, output: { model: "deepseek-chat" }, detail: null, round: 1 },
        { id: "e3", phase: "tool", label: "工具 mute_member", status: "failed", offsetMs: 9_700, durationMs: 40, input: { arguments: { user_id: 3 } }, output: { ok: false, error: "bot_not_admin" }, detail: null, round: 1 },
      ]
      : [
        { id: "e1", phase: "intake", label: "主动参与", status: "success", offsetMs: 1, durationMs: null, input: {}, output: {}, detail: null, round: null },
        { id: "e2", phase: "llm", label: "模型调用", status: "success", offsetMs: 1_100, durationMs: 1_000, input: {}, output: {}, detail: null, round: 1 },
      ],
  };
}

const stats: AgentExecutionTraceStats = {
  groupId: "1",
  capacity: 12,
  total: 2,
  failed: 1,
  degraded: 0,
  withTools: 1,
  withMedia: 0,
  outboundProblems: 0,
  running: 0,
  durationMs: { avg: 5_500, p50: 1_200, p95: 9_800, max: 9_800 },
  byMode: { dialogue: 1, warmup: 1 },
  byTrigger: { mention: 1, proactive_warmup: 1 },
  byOutcome: { error: 1, speak: 1 },
  phases: [{ phase: "llm", count: 2, avgMs: 4_950, maxMs: 8_900, totalMs: 9_900, failures: 0 }],
  tools: [{ name: "mute_member", count: 1 }],
  oldestAt: summaries[1].startedAt,
  newestAt: summaries[0].startedAt,
};

function debugResult(text: string, promptExtra = ""): AgentDebugResponse {
  return {
    promptVersion: "v9",
    mode: "dialogue",
    persona: {
      source: "persisted",
      persistedSummary: "傲娇猫娘",
      persistedProfile: {} as never,
      appliedProfile: {} as never,
      persistedBehavior: { chattiness: 2 } as never,
      appliedBehavior: { chattiness: 2 } as never,
      persistedEmotion: {} as never,
      appliedEmotion: {} as never,
    },
    currentTurn: { user_id: 10001, content: text },
    context: { messages: [], members: [], memories: [], relations: [] },
    contextSelection: [],
    contextBudget: [],
    promptMessages: [
      { role: "system", content: `你是一只猫娘${promptExtra}` },
      { role: "user", content: text },
    ],
    tools: [],
    toolPermissions: [],
    route: { task: "agent_dialogue", profile: "default", provider: "deepseek", model: "deepseek-chat", thinking: "off", multimodal: "off", configured: true },
    stats: {},
    warnings: ["当前没有在线 Bot，工具列表按离线能力收敛"],
    executionTrace: { ...detail("trace-old-0002"), source: "debug" },
    result: null,
  };
}

let runCount = 0;

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <output aria-label="location">{location.search}</output>;
}

function renderDebugger(initial = "/agent/1?tab=debug"): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/agent/:groupId" element={<><AgentDebugger groupId="1" /><LocationProbe /></>} />
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
  runCount = 0;
  window.localStorage.clear();
  apiMock.mockReset();
  apiMock.mockImplementation((path: string, init?: RequestInit) => {
    if (path === "/agent/groups/1/execution-traces/stats") return Promise.resolve({ data: stats, meta: {} });
    if (path.startsWith("/agent/groups/1/execution-traces/") && path.endsWith("/audits")) return Promise.resolve({ data: [], meta: {} });
    if (path.startsWith("/agent/groups/1/execution-traces/")) {
      return Promise.resolve({ data: detail(decodeURIComponent(path.split("/").at(-1) ?? "")), meta: {} });
    }
    if (path.startsWith("/agent/groups/1/execution-traces")) {
      if (init?.method === "DELETE") return Promise.resolve({ data: { cleared: 2 }, meta: {} });
      return Promise.resolve({ data: summaries, meta: {} });
    }
    if (path.startsWith("/agent/groups/1/messages")) return Promise.resolve({ data: [], meta: {} });
    if (path.startsWith("/groups/1/members")) {
      return Promise.resolve({ data: [{ userId: "10001", nickname: "小明", groupNickname: "群主小明", role: "owner", active: true }], meta: {} });
    }
    if (path === "/agent/groups/1/debug/run") {
      runCount += 1;
      const body = JSON.parse(String(init?.body ?? "{}")) as { text?: string };
      return Promise.resolve({ data: debugResult(body.text ?? "", runCount > 1 ? "\n新增一行规则" : ""), meta: {} });
    }
    return new Promise<never>(() => undefined);
  });
});

describe("AgentDebugger 2.0", () => {
  it("展示缓冲统计、带预览的 Trace 列表、KPI 与自动诊断", async () => {
    renderDebugger();

    expect(await screen.findByText("P95 耗时")).toBeInTheDocument();
    expect(screen.getByText("2 / 12")).toBeTruthy();
    const list = await screen.findByRole("listbox", { name: "Trace" });
    expect(within(list).getByText("帮我把刷屏的人禁言一下")).toBeInTheDocument();
    expect(within(list).getAllByRole("option")).toHaveLength(2);

    expect(await screen.findByText("模型响应偏慢")).toBeInTheDocument();
    expect(screen.getAllByText(/工具失败：工具 mute_member/).length).toBeGreaterThan(0);
    expect(screen.getByText("bot_not_admin", { selector: ".dbg-insight-copy span" })).toBeInTheDocument();
  });

  it("状态 chip 带数量并写入 URL，键盘 j/k 切换 Trace", async () => {
    renderDebugger();
    await screen.findByRole("listbox", { name: "Trace" });
    const failedChip = screen.getByRole("radio", { name: /失败/ });
    expect(failedChip.textContent).toContain("1");
    fireEvent.click(failedChip);
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.status=failed"));

    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.trace=trace-new-0001"));
    fireEvent.keyDown(window, { key: "j" });
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.trace=trace-old-0002"));
    fireEvent.keyDown(window, { key: "k" });
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.trace=trace-new-0001"));
  });

  it("收藏保存在本机，并可只看收藏", async () => {
    renderDebugger();
    const list = await screen.findByRole("listbox", { name: "Trace" });
    fireEvent.click(within(list).getAllByRole("button", { name: "收藏" })[1]);
    expect(JSON.parse(window.localStorage.getItem("yawnbot.debug.starred") ?? "{}")).toEqual({ 1: ["trace-old-0002"] });
    fireEvent.click(screen.getByRole("button", { name: "更多筛选" }));
    fireEvent.click(screen.getByText("只看收藏"));
    await waitFor(() => expect(within(screen.getByRole("listbox", { name: "Trace" })).getAllByRole("option")).toHaveLength(1));
  });

  it("点击诊断里的“定位事件”会写入事件深链", async () => {
    renderDebugger();
    await screen.findByText("模型响应偏慢");
    const insight = screen.getByText("模型响应偏慢").closest("li") as HTMLElement;
    fireEvent.click(within(insight).getByRole("button", { name: "定位事件" }));
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.event=e2"));
  });

  it("回放把真实 Trace 的消息带进模拟实验室", async () => {
    renderDebugger();
    await screen.findByText("模型响应偏慢");
    fireEvent.click(screen.getByRole("button", { name: /回放/ }));
    await waitFor(() => expect(screen.getByLabelText("location").textContent).toContain("debug.view=lab"));
    expect(screen.getByLabelText("location").textContent).toContain("debug.messageId=501");
  });

  it("实验室运行两次后可设基准并看到逐行 Prompt Diff", async () => {
    renderDebugger("/agent/1?tab=debug&debug.view=lab");
    fireEvent.click(await screen.findByText("打招呼"));
    const picker = screen.getByPlaceholderText("输入昵称或 QQ 号搜索群成员");
    fireEvent.change(picker, { target: { value: "10001" } });
    fireEvent.click(screen.getByRole("button", { name: /生成调试快照/ }));
    expect(await screen.findByText("运行历史")).toBeInTheDocument();
    expect(screen.getByText("当前没有在线 Bot，工具列表按离线能力收敛")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "设为基准 A" }));
    fireEvent.click(screen.getByRole("button", { name: /生成调试快照/ }));
    await waitFor(() => expect(runCount).toBe(2));
    await waitFor(() => expect(screen.getAllByRole("option", { name: /快照/ })).toHaveLength(2));

    fireEvent.click(screen.getByRole("tab", { name: "Diff" }));
    expect(await screen.findByText("新增一行规则")).toBeInTheDocument();
    expect(screen.getByText("+1")).toBeInTheDocument();
  });

  it("清空缓冲需要确认，并调用 DELETE", async () => {
    renderDebugger();
    await screen.findByRole("listbox", { name: "Trace" });
    fireEvent.click(screen.getByRole("button", { name: /清空缓冲/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^清\s?空$/ }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/agent/groups/1/execution-traces", { method: "DELETE" }));
  });

  it("? 打开快捷键帮助", async () => {
    renderDebugger();
    await screen.findByRole("listbox", { name: "Trace" });
    act(() => {
      fireEvent.keyDown(window, { key: "?" });
    });
    expect(await screen.findByText("调试台快捷键")).toBeInTheDocument();
  });
});
