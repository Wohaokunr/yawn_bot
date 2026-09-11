import type { PropsWithChildren } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import type { AgentExecutionTrace, AgentExecutionTraceSummary } from "../types";
import { useExecutionTraces } from "./useExecutionTraces";

vi.mock("../api", () => ({ api: vi.fn() }));

const apiMock = vi.mocked(api);

function wrapper({ children }: PropsWithChildren): React.JSX.Element {
  return <MemoryRouter initialEntries={["/agent/1?tab=debug"]}>{children}</MemoryRouter>;
}

function summary(traceId: string, status = "completed"): AgentExecutionTraceSummary {
  return {
    traceId,
    groupId: "1",
    mode: "dialogue",
    source: "real",
    triggerSource: "mention",
    actorUserId: "10",
    messageId: "20",
    startedAt: "2026-09-03T12:00:00+08:00",
    status,
    outcome: "success",
    durationMs: 120,
    eventCount: 3,
    hasFailure: status === "failed",
    hasDegradation: false,
    hasTool: false,
    hasMedia: false,
    hasOutboundProblem: false,
  };
}

function detail(traceId: string): AgentExecutionTrace {
  return {
    ...summary(traceId),
    events: [],
  };
}

describe("useExecutionTraces selection stability", () => {
  let summaries: AgentExecutionTraceSummary[];

  beforeEach(() => {
    summaries = [summary("new"), summary("old")];
    apiMock.mockReset();
    apiMock.mockImplementation(async (path: string) => {
      if (path.includes("/execution-traces/")) {
        const traceId = decodeURIComponent(path.split("/").at(-1) ?? "");
        return { data: detail(traceId) } as never;
      }
      return { data: summaries } as never;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("旧 Trace 被 12 条缓冲淘汰后保持用户选择和已加载详情", async () => {
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTraceId).toBe("new"));

    act(() => result.current.setSelectedTraceId("old"));
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("old"));

    summaries = [summary("new")];
    act(() => result.current.reload());

    await waitFor(() => expect(result.current.selectedTraceUnavailable).toBe(true));
    expect(result.current.selectedTraceId).toBe("old");
    expect(result.current.selectedTrace?.traceId).toBe("old");
  });

  it("状态筛选隐藏旧 Trace 时不改选中项", async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path.includes("/execution-traces/")) {
        const traceId = decodeURIComponent(path.split("/").at(-1) ?? "");
        return { data: detail(traceId) } as never;
      }
      return {
        data: path.includes("?status=failed") ? [summary("failed", "failed")] : summaries,
      } as never;
    });
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTraceId).toBe("new"));

    act(() => result.current.setSelectedTraceId("old"));
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("old"));
    act(() => result.current.setStatus("failed"));

    await waitFor(() => expect(result.current.summaries[0]?.traceId).toBe("failed"));
    expect(result.current.status).toBe("failed");
    expect(result.current.selectedTraceId).toBe("old");
    expect(result.current.selectedTrace?.traceId).toBe("old");
    expect(result.current.selectedTraceUnavailable).toBe(true);
  });

  it("3 秒自动轮询刷新摘要时不会把用户从旧 Trace 跳回最新", async () => {
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTraceId).toBe("new"));
    act(() => result.current.setAutoRefresh(false));
    act(() => result.current.setSelectedTraceId("old"));
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("old"));

    let intervalCallback: (() => void) | null = null;
    vi.spyOn(window, "setInterval").mockImplementation((handler: TimerHandler) => {
      intervalCallback = handler as () => void;
      return 1;
    });
    vi.spyOn(window, "clearInterval").mockImplementation(() => undefined);
    act(() => result.current.setAutoRefresh(true));

    summaries = [summary("newer"), summary("new")];
    expect(intervalCallback).not.toBeNull();
    await act(async () => {
      intervalCallback?.();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.summaries[0]?.traceId).toBe("newer");
    expect(result.current.selectedTraceId).toBe("old");
    expect(result.current.selectedTrace?.traceId).toBe("old");
    expect(result.current.selectedTraceUnavailable).toBe(true);
  });
  it("摘要变化刷新选中详情，但相同摘要不重复获取", async () => {
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("new"));
    const count = () => apiMock.mock.calls.filter(([path]) => path.endsWith("/new")).length;
    const initial = count();
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.listRefreshing).toBe(false));
    expect(count()).toBe(initial);
    summaries = [{ ...summary("new"), eventCount: 4 }, summary("old")];
    act(() => result.current.reload());
    await waitFor(() => expect(count()).toBe(initial + 1));
  });

  it("刷新失败保留已加载快照，并可恢复跟随最新", async () => {
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("new"));
    act(() => result.current.setSelectedTraceId("old"));
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("old"));
    apiMock.mockImplementation(async (path: string) => {
      if (path.endsWith("/old")) throw new Error("详情已过期");
      return { data: path.includes("/execution-traces/") ? detail("new") : summaries } as never;
    });
    act(() => result.current.reloadSelected());
    await waitFor(() => expect(result.current.detailError).toBe("详情已过期"));
    expect(result.current.selectedTrace?.traceId).toBe("old");
    act(() => result.current.selectLatest());
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("new"));
  });

  it("隐藏页签停止轮询", async () => {
    const interval = vi.spyOn(window, "setInterval");
    const clear = vi.spyOn(window, "clearInterval");
    const { rerender } = renderHook(({ active }) => useExecutionTraces("1", active), { wrapper, initialProps: { active: false } });
    expect(interval).not.toHaveBeenCalled();
    rerender({ active: true });
    expect(interval).toHaveBeenCalledWith(expect.any(Function), 3000);
    rerender({ active: false });
    expect(clear).toHaveBeenCalled();
  });

  it("合并同一详情重复刷新，旧请求不覆盖新选择", async () => {
    const { result } = renderHook(() => useExecutionTraces("1"), { wrapper });
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("new"));
    let resolveOld!: (value: unknown) => void;
    apiMock.mockImplementation((path: string) => {
      if (path.endsWith("/old")) return new Promise((resolve) => { resolveOld = resolve; }) as never;
      return Promise.resolve({ data: path.includes("/execution-traces/") ? detail("new") : summaries }) as never;
    });
    act(() => result.current.setSelectedTraceId("old"));
    await waitFor(() => expect(resolveOld).toBeDefined());
    act(() => { result.current.reloadSelected(); result.current.reloadSelected(); });
    expect(apiMock.mock.calls.filter(([path]) => path.endsWith("/old"))).toHaveLength(1);
    act(() => result.current.setSelectedTraceId("new"));
    await waitFor(() => expect(result.current.selectedTrace?.traceId).toBe("new"));
    await act(async () => resolveOld({ data: detail("old") }));
    expect(result.current.selectedTrace?.traceId).toBe("new");
  });

});
