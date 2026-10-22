import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useVisiblePolling } from "../agent-workspace/polling";
import { api } from "../api";
import { useApiQuery } from "../shared";
import type { AgentExecutionTrace, AgentExecutionTraceStats, AgentExecutionTraceSummary } from "../types";
import { useLocalState } from "./debug-storage";
import { sortSummaries, summarySearchText, type TraceSort } from "./trace-analysis";

export const TRACE_POLL_OPTIONS = [2_000, 3_000, 5_000, 10_000] as const;
const DEFAULT_POLL_INTERVAL_MS = 3_000;

export interface TraceServerFilters {
  mode: string;
  triggerSource: string;
  actorUserId: string;
}

export interface ExecutionTracesState {
  summaries: AgentExecutionTraceSummary[];
  visibleSummaries: AgentExecutionTraceSummary[];
  stats: AgentExecutionTraceStats | null;
  statsError: string;
  status: string;
  setStatus: (value: string) => void;
  filters: TraceServerFilters;
  setFilter: (key: keyof TraceServerFilters, value: string) => void;
  clearFilters: () => void;
  activeFilterCount: number;
  search: string;
  setSearch: (value: string) => void;
  sort: TraceSort;
  setSort: (value: TraceSort) => void;
  starred: string[];
  toggleStar: (traceId: string) => void;
  starredOnly: boolean;
  setStarredOnly: (value: boolean) => void;
  autoRefresh: boolean;
  setAutoRefresh: (value: boolean) => void;
  pollInterval: number;
  setPollInterval: (value: number) => void;
  pageHidden: boolean;
  newCount: number;
  acknowledgeNew: () => void;
  selectedTraceId: string;
  setSelectedTraceId: (value: string) => void;
  selectRelative: (delta: number) => void;
  selectedTrace: AgentExecutionTrace | null;
  selectedTraceUnavailable: boolean;
  listLoading: boolean;
  listRefreshing: boolean;
  listError: string;
  detailLoading: boolean;
  detailError: string;
  lastUpdatedAt: number | null;
  reload: () => void;
  reloadSelected: () => void;
  clearBuffer: () => Promise<number>;
}

const FILTER_PARAMS: Record<keyof TraceServerFilters, string> = {
  mode: "debug.mode",
  triggerSource: "debug.trigger",
  actorUserId: "debug.actor",
};

function usePageHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.visibilityState === "hidden");
  useEffect(() => {
    const update = (): void => setHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return hidden;
}

export function useExecutionTraces(groupId: string): ExecutionTracesState {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = searchParams.get("debug.status") ?? "";
  const selectedTraceId = searchParams.get("debug.trace") ?? "";
  const sort = (searchParams.get("debug.sort") ?? "newest") as TraceSort;
  const filters: TraceServerFilters = {
    mode: searchParams.get(FILTER_PARAMS.mode) ?? "",
    triggerSource: searchParams.get(FILTER_PARAMS.triggerSource) ?? "",
    actorUserId: searchParams.get(FILTER_PARAMS.actorUserId) ?? "",
  };
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [pollInterval, setPollInterval] = useLocalState<number>("pollInterval", DEFAULT_POLL_INTERVAL_MS);
  const [starredByGroup, setStarredByGroup] = useLocalState<Record<string, string[]>>("starred", {});
  const [starredOnly, setStarredOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [acknowledgedTopId, setAcknowledgedTopId] = useState("");
  const pageHidden = usePageHidden();
  const userSelectedTrace = useRef(Boolean(selectedTraceId));

  const updateParams = useCallback((changes: Record<string, string>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value); else next.delete(key);
    }
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const setStatus = useCallback((value: string) => updateParams({ "debug.status": value }), [updateParams]);
  const setSort = useCallback((value: TraceSort) => updateParams({ "debug.sort": value === "newest" ? "" : value }), [updateParams]);
  const setFilter = useCallback((key: keyof TraceServerFilters, value: string) => {
    updateParams({ [FILTER_PARAMS[key]]: value.trim() });
  }, [updateParams]);
  const clearFilters = useCallback(() => {
    updateParams({ "debug.status": "", [FILTER_PARAMS.mode]: "", [FILTER_PARAMS.triggerSource]: "", [FILTER_PARAMS.actorUserId]: "" });
    setSearch("");
    setStarredOnly(false);
  }, [updateParams]);

  const setSelectedTraceId = useCallback((traceId: string) => {
    userSelectedTrace.current = true;
    updateParams({ "debug.trace": traceId, "debug.event": "" });
  }, [updateParams]);

  const queryString = useMemo(() => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (filters.mode) params.set("mode", filters.mode);
    if (filters.triggerSource) params.set("triggerSource", filters.triggerSource);
    if (/^\d+$/.test(filters.actorUserId)) params.set("actorUserId", filters.actorUserId);
    const text = params.toString();
    return text ? `?${text}` : "";
  }, [filters.actorUserId, filters.mode, filters.triggerSource, status]);

  const listQuery = useApiQuery({
    queryKey: ["agent-execution-traces", groupId, queryString],
    fetcher: (signal) => api<AgentExecutionTraceSummary[]>(
      `/agent/groups/${groupId}/execution-traces${queryString}`,
      { signal },
    ).then((response) => response.data),
  });
  const summaries = useMemo(() => listQuery.data ?? [], [listQuery.data]);

  const statsQuery = useApiQuery({
    queryKey: ["agent-execution-trace-stats", groupId],
    fetcher: (signal) => api<AgentExecutionTraceStats>(
      `/agent/groups/${groupId}/execution-traces/stats`,
      { signal },
    ).then((response) => response.data),
  });

  const starred = useMemo(() => starredByGroup[groupId] ?? [], [groupId, starredByGroup]);
  const toggleStar = useCallback((traceId: string) => {
    setStarredByGroup((current) => {
      const list = current[groupId] ?? [];
      const nextList = list.includes(traceId) ? list.filter((item) => item !== traceId) : [traceId, ...list].slice(0, 50);
      return { ...current, [groupId]: nextList };
    });
  }, [groupId, setStarredByGroup]);

  const visibleSummaries = useMemo(() => {
    const query = search.trim().toLowerCase();
    const filtered = summaries.filter((trace) => (
      (!query || summarySearchText(trace).includes(query))
      && (!starredOnly || starred.includes(trace.traceId))
    ));
    return sortSummaries(filtered, sort);
  }, [search, sort, starred, starredOnly, summaries]);

  // 用户没有手动选过时跟随最新 Trace；手动选择后绝不自动跳走。
  useEffect(() => {
    const firstId = summaries[0]?.traceId ?? "";
    if (firstId && (!selectedTraceId || !userSelectedTrace.current)) {
      if (selectedTraceId !== firstId) updateParams({ "debug.trace": firstId });
    }
  }, [selectedTraceId, summaries, updateParams]);

  useEffect(() => {
    const firstId = summaries[0]?.traceId ?? "";
    if (!firstId) return;
    if (!acknowledgedTopId || !userSelectedTrace.current) setAcknowledgedTopId(firstId);
  }, [acknowledgedTopId, summaries]);

  const newCount = useMemo(() => {
    if (!acknowledgedTopId) return 0;
    const index = summaries.findIndex((trace) => trace.traceId === acknowledgedTopId);
    return index < 0 ? (summaries.length > 0 && userSelectedTrace.current ? summaries.length : 0) : index;
  }, [acknowledgedTopId, summaries]);

  const acknowledgeNew = useCallback(() => {
    const firstId = summaries[0]?.traceId ?? "";
    setAcknowledgedTopId(firstId);
    if (firstId) setSelectedTraceId(firstId);
  }, [setSelectedTraceId, summaries]);

  const previousGroupId = useRef(groupId);
  useEffect(() => {
    if (previousGroupId.current === groupId) return;
    previousGroupId.current = groupId;
    userSelectedTrace.current = false;
    setAcknowledgedTopId("");
    const next = new URLSearchParams(searchParams);
    next.delete("debug.trace");
    next.delete("debug.event");
    next.delete("debug.messageId");
    next.delete("debug.compare");
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

  const reload = useCallback(() => {
    listQuery.reload();
    statsQuery.reload();
  }, [listQuery.reload, statsQuery.reload]);

  // 标签页隐藏时暂停轮询，回到前台立即补一次（与驾驶舱共用 useVisiblePolling）。
  useVisiblePolling(reload, autoRefresh ? pollInterval : null);

  const reloadSelected = useCallback(() => {
    reload();
    if (selectedTraceId) detailQuery.reload();
  }, [detailQuery.reload, reload, selectedTraceId]);

  const selectRelative = useCallback((delta: number) => {
    if (visibleSummaries.length === 0) return;
    const index = visibleSummaries.findIndex((trace) => trace.traceId === selectedTraceId);
    const nextIndex = index < 0 ? 0 : Math.min(visibleSummaries.length - 1, Math.max(0, index + delta));
    const next = visibleSummaries[nextIndex];
    if (next && next.traceId !== selectedTraceId) setSelectedTraceId(next.traceId);
  }, [selectedTraceId, setSelectedTraceId, visibleSummaries]);

  const clearBuffer = useCallback(async () => {
    const response = await api<{ cleared: number }>(`/agent/groups/${groupId}/execution-traces`, { method: "DELETE" });
    userSelectedTrace.current = false;
    setAcknowledgedTopId("");
    updateParams({ "debug.trace": "", "debug.event": "", "debug.compare": "" });
    reload();
    return response.data.cleared;
  }, [groupId, reload, updateParams]);

  const selectedTraceUnavailable = Boolean(
    userSelectedTrace.current
      && selectedTraceId
      && !summaries.some((item) => item.traceId === selectedTraceId),
  );

  const activeFilterCount = [status, filters.mode, filters.triggerSource, filters.actorUserId, search.trim(), starredOnly ? "1" : ""]
    .filter(Boolean).length;

  return {
    summaries,
    visibleSummaries,
    stats: statsQuery.data,
    statsError: statsQuery.error,
    status,
    setStatus,
    filters,
    setFilter,
    clearFilters,
    activeFilterCount,
    search,
    setSearch,
    sort,
    setSort,
    starred,
    toggleStar,
    starredOnly,
    setStarredOnly,
    autoRefresh,
    setAutoRefresh,
    pollInterval,
    setPollInterval,
    pageHidden,
    newCount,
    acknowledgeNew,
    selectedTraceId,
    setSelectedTraceId,
    selectRelative,
    selectedTrace: detailQuery.data,
    selectedTraceUnavailable,
    listLoading: listQuery.loading,
    listRefreshing: listQuery.refreshing,
    listError: listQuery.error,
    detailLoading: detailQuery.loading,
    detailError: detailQuery.error,
    lastUpdatedAt: listQuery.updatedAt,
    reload,
    reloadSelected,
    clearBuffer,
  };
}
