import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useApiQuery } from "../shared";
import type { AgentExecutionTrace, AgentExecutionTraceSummary } from "../types";

const TRACE_POLL_INTERVAL_MS = 3_000;

export interface ExecutionTracesState {
  summaries: AgentExecutionTraceSummary[];
  status: string;
  setStatus: (value: string) => void;
  autoRefresh: boolean;
  setAutoRefresh: (value: boolean) => void;
  selectedTraceId: string;
  setSelectedTraceId: (value: string) => void;
  selectedTrace: AgentExecutionTrace | null;
  selectedTraceUnavailable: boolean;
  listLoading: boolean;
  listRefreshing: boolean;
  listError: string;
  detailLoading: boolean;
  detailError: string;
  reload: () => void;
  reloadSelected: () => void;
}

export function useExecutionTraces(groupId: string): ExecutionTracesState {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = searchParams.get("debug.status") ?? "";
  const selectedTraceId = searchParams.get("debug.trace") ?? "";
  const [autoRefresh, setAutoRefresh] = useState(true);
  const userSelectedTrace = useRef(Boolean(selectedTraceId));

  const updateParam = useCallback((key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value); else next.delete(key);
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const setStatus = useCallback((value: string) => {
    updateParam("debug.status", value);
  }, [updateParam]);

  const setSelectedTraceId = useCallback((traceId: string) => {
    userSelectedTrace.current = true;
    updateParam("debug.trace", traceId);
  }, [updateParam]);

  const listQuery = useApiQuery({
    queryKey: ["agent-execution-traces", groupId, status],
    fetcher: (signal) => {
      const query = status ? `?status=${encodeURIComponent(status)}` : "";
      return api<AgentExecutionTraceSummary[]>(
        `/agent/groups/${groupId}/execution-traces${query}`,
        { signal },
      ).then((response) => response.data);
    },
  });
  const summaries = listQuery.data ?? [];

  useEffect(() => {
    const firstId = summaries[0]?.traceId ?? "";
    if (firstId && (!selectedTraceId || !userSelectedTrace.current)) {
      if (selectedTraceId !== firstId) updateParam("debug.trace", firstId);
    }
  }, [selectedTraceId, summaries, updateParam]);

  const previousGroupId = useRef(groupId);
  useEffect(() => {
    if (previousGroupId.current === groupId) return;
    previousGroupId.current = groupId;
    userSelectedTrace.current = false;
    const next = new URLSearchParams(searchParams);
    next.delete("debug.trace");
    next.delete("debug.messageId");
    setSearchParams(next, { replace: true });
  }, [groupId, searchParams, setSearchParams]);

  const detailQuery = useApiQuery({
    queryKey: ["agent-execution-trace-detail", groupId, selectedTraceId],
    fetcher: (signal) => selectedTraceId
      ? api<AgentExecutionTrace>(
        `/agent/groups/${groupId}/execution-traces/${encodeURIComponent(selectedTraceId)}`,
        { signal },
      ).then((response) => response.data)
      : Promise.resolve(null),
  });

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(listQuery.reload, TRACE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [autoRefresh, listQuery.reload]);

  const reloadSelected = useCallback(() => {
    listQuery.reload();
    if (selectedTraceId) detailQuery.reload();
  }, [detailQuery.reload, listQuery.reload, selectedTraceId]);

  const selectedTraceUnavailable = Boolean(
    userSelectedTrace.current
      && selectedTraceId
      && !summaries.some((item) => item.traceId === selectedTraceId),
  );

  return {
    summaries,
    status,
    setStatus,
    autoRefresh,
    setAutoRefresh,
    selectedTraceId,
    setSelectedTraceId,
    selectedTrace: detailQuery.data,
    selectedTraceUnavailable,
    listLoading: listQuery.loading,
    listRefreshing: listQuery.refreshing,
    listError: listQuery.error,
    detailLoading: detailQuery.loading,
    detailError: detailQuery.error,
    reload: listQuery.reload,
    reloadSelected,
  };
}
