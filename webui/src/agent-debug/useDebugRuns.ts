import { useCallback, useMemo, useState } from "react";
import type { AgentDebugMode, AgentDebugResponse } from "../types";

export const DEBUG_RUN_HISTORY_LIMIT = 12;

export interface DebugRunInput {
  mode: AgentDebugMode;
  source: "history" | "simulation";
  messageId?: string;
  actorUserId?: string;
  text?: string;
  runModel: boolean;
}

export interface DebugRun {
  id: string;
  createdAt: number;
  elapsedMs: number;
  input: DebugRunInput;
  result: AgentDebugResponse;
  /** 同一次“四场景矩阵”运行产生的结果共享 batchId。 */
  batchId?: string;
}

export interface DebugRunsState {
  runs: DebugRun[];
  active: DebugRun | null;
  baseline: DebugRun | null;
  activeId: string;
  baselineId: string;
  add: (run: Omit<DebugRun, "id" | "createdAt"> & { createdAt?: number }) => DebugRun;
  setActive: (id: string) => void;
  setBaseline: (id: string) => void;
  remove: (id: string) => void;
  clear: () => void;
}

let sequence = 0;

/**
 * 模拟实验室的运行历史。放在 AgentDebugger 里而不是实验室组件内部，
 * 这样切换 Agent 工作台的页签、或在调试台的视图之间来回切都不会丢。
 */
export function useDebugRuns(): DebugRunsState {
  const [runs, setRuns] = useState<DebugRun[]>([]);
  const [activeId, setActiveId] = useState("");
  const [baselineId, setBaselineId] = useState("");

  const add = useCallback((input: Omit<DebugRun, "id" | "createdAt"> & { createdAt?: number }) => {
    sequence += 1;
    const run: DebugRun = { ...input, id: `run-${Date.now().toString(36)}-${sequence}`, createdAt: input.createdAt ?? Date.now() };
    setRuns((current) => [run, ...current].slice(0, DEBUG_RUN_HISTORY_LIMIT));
    setActiveId(run.id);
    return run;
  }, []);

  const remove = useCallback((id: string) => {
    setRuns((current) => current.filter((run) => run.id !== id));
    setActiveId((current) => (current === id ? "" : current));
    setBaselineId((current) => (current === id ? "" : current));
  }, []);

  const clear = useCallback(() => {
    setRuns([]);
    setActiveId("");
    setBaselineId("");
  }, []);

  const active = useMemo(() => runs.find((run) => run.id === activeId) ?? runs[0] ?? null, [activeId, runs]);
  const baseline = useMemo(() => runs.find((run) => run.id === baselineId) ?? null, [baselineId, runs]);

  return {
    runs,
    active,
    baseline,
    activeId: active?.id ?? "",
    baselineId: baseline?.id ?? "",
    add,
    setActive: setActiveId,
    setBaseline: setBaselineId,
    remove,
    clear,
  };
}
