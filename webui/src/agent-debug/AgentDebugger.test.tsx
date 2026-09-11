import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import type { AgentDebugResponse, AgentExecutionTrace } from "../types";
import { AgentDebugger } from "./AgentDebugger";
import { TracePipeline } from "./TracePipeline";
import { TraceSidebar } from "./TraceSidebar";
import type { ExecutionTracesState } from "./useExecutionTraces";

vi.mock("./useExecutionTraces", () => ({ useExecutionTraces: () => ({ summaries: [], selectedTrace: null }) }));
vi.mock("./SimulationWorkbench", () => ({ SimulationWorkbench: ({ onResult }: { onResult: (result: AgentDebugResponse) => void }) => {
  const [text, setText] = useState("");
  return <><input aria-label="模拟输入" value={text} onChange={(event) => setText(event.target.value)} />
    <button onClick={() => onResult({ promptVersion: text } as AgentDebugResponse)}>生成结果</button></>;
} }));
vi.mock("./TraceWorkspace", () => ({ TraceWorkspace: ({ result, baseline, onPinBaseline, onClearBaseline }: {
  result: AgentDebugResponse; baseline: AgentDebugResponse | null; onPinBaseline: () => void; onClearBaseline: () => void;
}) => <><span>结果：{result.promptVersion}</span><span>基准：{baseline?.promptVersion ?? "空"}</span>
  <button onClick={onPinBaseline}>固定基准</button><button onClick={onClearBaseline}>清除基准</button></> }));

afterAll(() => vi.unstubAllGlobals());

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Object.defineProperty(window, "matchMedia", { writable: true, value: vi.fn().mockImplementation(() => ({
    matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })) });
});

const trace: AgentExecutionTrace = {
  traceId: "trace-1", groupId: "1", mode: "dialogue", source: "runtime", triggerSource: "mention",
  actorUserId: "10", messageId: "20", startedAt: "2026-09-11T00:00:00Z", status: "completed", outcome: "success", durationMs: 100,
  events: ["success", "failed", "degraded", "unknown", "planned", "skipped"].map((status, index) => ({
    id: String(index), phase: "test", label: `event-${status}`, status, offsetMs: index, durationMs: 1,
    input: { note: "长文本".repeat(100) }, output: {}, detail: null, round: null,
  })),
};

describe("Agent debugger workflow", () => {
  it("默认真实执行，切换页签保留模拟输入、结果和可替换基准", async () => {
    render(<MemoryRouter><AgentDebugger groupId="1" /></MemoryRouter>);
    expect(screen.getByRole("tab", { name: "真实执行" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("tab", { name: "模拟调试" }));
    fireEvent.change(screen.getByLabelText("模拟输入"), { target: { value: "第一次" } });
    fireEvent.click(screen.getByText("生成结果"));
    fireEvent.click(screen.getByText("固定基准"));
    fireEvent.click(screen.getByRole("tab", { name: "真实执行" }));
    fireEvent.click(screen.getByRole("tab", { name: "模拟调试" }));
    expect(screen.getByLabelText("模拟输入")).toHaveValue("第一次");
    expect(screen.getByText("基准：第一次")).toBeVisible();
    fireEvent.change(screen.getByLabelText("模拟输入"), { target: { value: "第二次" } });
    fireEvent.click(screen.getByText("生成结果"));
    expect(screen.getByText("基准：第一次")).toBeVisible();
    fireEvent.click(screen.getByText("固定基准"));
    expect(screen.getByText("基准：第二次")).toBeVisible();
    fireEvent.click(screen.getByText("清除基准"));
    expect(screen.getByText("基准：空")).toBeVisible();
  });

  it("历史消息链接直接打开模拟调试", () => {
    render(<MemoryRouter initialEntries={["/?messageId=20"]}><AgentDebugger groupId="1" /></MemoryRouter>);
    expect(screen.getByRole("tab", { name: "模拟调试" })).toHaveAttribute("aria-selected", "true");
  });

  it("异常过滤不包括 planned/skipped，详情默认折叠并在刷新时保持展开", async () => {
    const { container, rerender } = render(<TracePipeline trace={trace} />);
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    fireEvent.click(details.querySelector("summary")!);
    expect(details.open).toBe(true);
    rerender(<TracePipeline trace={{ ...trace, durationMs: 200, events: [...trace.events] }} />);
    expect(container.querySelector("details")!.open).toBe(true);
    fireEvent.click(screen.getByText("异常事件", { selector: ".ant-segmented-item-label" }));
    await waitFor(() => expect(screen.queryByText("event-success")).not.toBeInTheDocument());
    for (const status of ["failed", "degraded", "unknown"]) expect(screen.getByText(`event-${status}`)).toBeVisible();
    expect(screen.queryByText("event-planned")).not.toBeInTheDocument();
    expect(screen.queryByText("event-skipped")).not.toBeInTheDocument();
  });

  it("列表支持键盘选择并显示摘要字段", () => {
    const select = vi.fn();
    render(<TraceSidebar traces={{ summaries: [{ ...trace, eventCount: 6 }], selectedTraceId: "", setSelectedTraceId: select } as unknown as ExecutionTracesState} />);
    const item = screen.getByRole("button", { name: /发言人 10/ });
    fireEvent.keyDown(item, { key: "Enter" });
    fireEvent.keyDown(item, { key: " " });
    expect(select).toHaveBeenNthCalledWith(1, "trace-1");
    expect(select).toHaveBeenNthCalledWith(2, "trace-1");
    expect(screen.getByText("100.0 ms")).toBeVisible();
  });
});
