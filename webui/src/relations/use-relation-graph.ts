import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useEntityRefresh } from "../shared";
import type { AgentRelationGraph } from "../types";

export interface RelationGraphState {
  data: AgentRelationGraph | null;
  loading: boolean;
  stale: boolean;
  error: string;
  reload: () => void;
}

// 全量图谱只在需要时拉取（图谱/成员/矩阵/洞察视图、成员抽屉、导出）；
// 列表视图保持轻量分页，不触发这里的请求。拉过一次后切回列表也保留数据，避免来回闪烁。
export function useRelationGraph(groupId: string, enabled: boolean): RelationGraphState {
  const [data, setData] = useState<AgentRelationGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const loadedForRef = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    if (data) setStale(true);
    api<AgentRelationGraph>(`/agent/groups/${groupId}/relations/graph`, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        loadedForRef.current = groupId;
        setData(response.data);
        setStale(false);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "加载失败");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // data 只用于判断是否标记 stale，不应触发重拉。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, enabled, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  // 未启用时的变更通知只把缓存标脏，下次启用自然重拉。
  useEntityRefresh(
    () => {
      if (enabled) reload();
      else if (loadedForRef.current) setStale(true);
    },
    { resources: ["agent_relation", "agent_member_data", "agent_group_data"], scope: { groupId } },
  );

  return { data: loadedForRef.current === groupId ? data : null, loading, stale, error, reload };
}
