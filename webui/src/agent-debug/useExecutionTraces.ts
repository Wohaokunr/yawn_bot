import { useCallback, useEffect, useRef, useState } from "react";
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
  const [status, setStatus] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [selectedTrace, setSelectedTrace] = useState<AgentExecutionTrace | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const detailGeneration = useRef(0);
  const userSelectedTrace = useRef(false);

  const loadSummaries = useCallback(
    () => {
      const query = status ? `?status=${encodeURIComponent(status)}` : "";
      return api<AgentExecutionTraceSummary[]>(
        `/agent/groups/${groupId}/execution-traces${query}`,
      ).then((response) => response.data);
    },
    [groupId, status],
  );
  const listQuery = useApiQuery(loadSummaries);
  const summaries = listQuery.data ?? [];

  useEffect(() => {
    const firstId = summaries[0]?.traceId ?? "";
    if (firstId && (!selectedTraceId || !userSelectedTrace.current)) {
      if (selectedTraceId !== firstId) setSelectedTraceId(firstId);
    }
  }, [selectedTraceId, summaries]);

  useEffect(() => {
    userSelectedTrace.current = false;
    setSelectedTraceId("");
    setSelectedTrace(null);
    setDetailError("");
  }, [groupId]);

  const selectTrace = useCallback((traceId: string) => {
    userSelectedTrace.current = true;
    setSelectedTraceId(traceId);
  }, []);

  const loadDetail = useCallback(
    async (traceId: string) => {
      const ticket = ++detailGeneration.current;
      if (!traceId) {
        setSelectedTrace(null);
        setDetailError("");
        setDetailLoading(false);
        return;
      }
      setSelectedTrace(null);
      setDetailLoading(true);
      setDetailError("");
      try {
        const response = await api<AgentExecutionTrace>(
          `/agent/groups/${groupId}/execution-traces/${encodeURIComponent(traceId)}`,
        );
        if (ticket === detailGeneration.current) setSelectedTrace(response.data);
      } catch (error) {
        if (ticket === detailGeneration.current) {
          setDetailError(error instanceof Error ? error.message : "Trace 详情加载失败");
        }
      } finally {
        if (ticket === detailGeneration.current) setDetailLoading(false);
      }
    },
    [groupId],
  );

  useEffect(() => {
    void loadDetail(selectedTraceId);
  }, [loadDetail, selectedTraceId]);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(listQuery.reload, TRACE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [autoRefresh, listQuery.reload]);

  const reloadSelected = useCallback(() => {
    listQuery.reload();
    if (selectedTraceId) void loadDetail(selectedTraceId);
  }, [listQuery.reload, loadDetail, selectedTraceId]);

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
    setSelectedTraceId: selectTrace,
    selectedTrace,
    selectedTraceUnavailable,
    listLoading: listQuery.loading,
    listRefreshing: listQuery.refreshing,
    listError: listQuery.error,
    detailLoading,
    detailError,
    reload: listQuery.reload,
    reloadSelected,
  };
}
