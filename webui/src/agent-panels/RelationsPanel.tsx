// 人物关系面板外壳：指标条 + 工具栏（视图/搜索/筛选/导出/新增）+ 五个视图 + 共享抽屉。
// 视图与筛选写进 URL（relations.*），刷新或分享链接能回到同一现场；写操作统一在这里完成后刷新各数据源。
import {
  AppstoreOutlined, BulbOutlined, CloseOutlined, DeploymentUnitOutlined, DownloadOutlined, FilterOutlined,
  PlusOutlined, ReloadOutlined, TeamOutlined, UnorderedListOutlined,
} from "@ant-design/icons";
import {
  App as AntApp, Badge, Button, Dropdown, Input, Popover, Segmented, Select, Slider, Space, Spin, Tag, Tooltip, Typography,
} from "antd";
import type { InputRef } from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { RELATION_SOURCE_META, RELATION_TYPE_PRESETS } from "../agent-meta";
import { nodeDisplayName, relationTypeColor } from "../relation-meta";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentRelationGraph, AgentRelationItem, AgentRelationListItem, AgentRelationSummary } from "../types";
import { EdgeDetailDrawer, MemberDrawer } from "../relations/RelationDetailDrawers";
import { RelationEditorDrawer } from "../relations/RelationEditorDrawer";
import { RelationMetrics } from "../relations/RelationMetrics";
import { RelationTableView, type RelationTableState } from "../relations/RelationTableView";
import { downloadText, filterEdges, relationsToCsv } from "../relations/relation-analysis";
import {
  activeFilterCount, EMPTY_FILTERS, RELATION_VIEWS, type RelationCreatePrefill, type RelationFilters,
  type RelationsContext, type RelationsView,
} from "../relations/relations-context";
import { useRelationGraph } from "../relations/use-relation-graph";

const { Text } = Typography;

// 重视图按需加载：列表视图首屏不拉 d3 与分析代码。
const LazyRelationGraphView = lazy(() =>
  import("../relation-graph").then(({ RelationGraphView }) => ({ default: RelationGraphView })),
);
const LazyMembersView = lazy(() => import("../relations/RelationMembersView").then((mod) => ({ default: mod.RelationMembersView })));
const LazyMatrixView = lazy(() => import("../relations/RelationMatrixView").then((mod) => ({ default: mod.RelationMatrixView })));
const LazyInsightsView = lazy(() => import("../relations/RelationInsightsView").then((mod) => ({ default: mod.RelationInsightsView })));

const VIEW_ICONS: Record<RelationsView, React.ReactNode> = {
  table: <UnorderedListOutlined />,
  graph: <DeploymentUnitOutlined />,
  members: <TeamOutlined />,
  matrix: <AppstoreOutlined />,
  insights: <BulbOutlined />,
};
const VIEW_KEYS = new Set(RELATION_VIEWS.map((view) => view.key));

function readFilters(params: URLSearchParams): RelationFilters {
  const number = (key: string) => {
    const raw = params.get(key);
    if (raw === null || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? Math.min(Math.max(value, 0), 1) : null;
  };
  return {
    type: params.get("relations.type") ?? "",
    source: params.get("relations.source") ?? "",
    member: params.get("relations.member") ?? "",
    search: params.get("relations.q") ?? "",
    minConfidence: number("relations.min"),
    maxConfidence: number("relations.max"),
  };
}

const FILTER_PARAM: Record<keyof RelationFilters, string> = {
  type: "relations.type",
  source: "relations.source",
  member: "relations.member",
  search: "relations.q",
  minConfidence: "relations.min",
  maxConfidence: "relations.max",
};

function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return !!element && (element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName));
}

export function RelationsPanel({
  groupId,
  readOnly = false,
}: {
  groupId: string;
  readOnly?: boolean;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get("relations.view") ?? "table";
  const view: RelationsView = VIEW_KEYS.has(requestedView as RelationsView) ? requestedView as RelationsView : "table";
  const filters = useMemo(() => readFilters(searchParams), [searchParams]);
  const [table, setTable] = useState<RelationTableState>({ page: 1, pageSize: 20, sort: "confidence", order: "desc" });
  const [searchDraft, setSearchDraft] = useState(filters.search);
  const [editorMode, setEditorMode] = useState<"create" | "edit" | null>(null);
  const [prefill, setPrefill] = useState<RelationCreatePrefill | undefined>();
  const [editing, setEditing] = useState<AgentRelationItem | null>(null);
  const [detailEdge, setDetailEdge] = useState<AgentRelationListItem | null>(null);
  const [memberId, setMemberId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [focusRequest, setFocusRequest] = useState<{ userId: string; nonce: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<InputRef>(null);
  const nameCache = useRef(new Map<string, string>());

  const writeParams = useCallback((mutate: (next: URLSearchParams) => void) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      mutate(next);
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const setView = useCallback((value: RelationsView) => {
    writeParams((next) => {
      if (value === "table") next.delete("relations.view");
      else next.set("relations.view", value);
    });
  }, [writeParams]);

  const patchFilters = useCallback((patch: Partial<RelationFilters>) => {
    writeParams((next) => {
      for (const [key, value] of Object.entries(patch) as Array<[keyof RelationFilters, RelationFilters[keyof RelationFilters]]>) {
        if (value === null || value === "" || value === undefined) next.delete(FILTER_PARAM[key]);
        else next.set(FILTER_PARAM[key], String(value));
      }
    });
    setTable((current) => ({ ...current, page: 1 }));
    if (patch.search !== undefined) setSearchDraft(patch.search);
  }, [writeParams]);

  const resetFilters = useCallback(() => {
    writeParams((next) => Object.values(FILTER_PARAM).forEach((key) => next.delete(key)));
    setSearchDraft("");
    setTable((current) => ({ ...current, page: 1 }));
  }, [writeParams]);

  // 搜索框防抖写回 URL，避免每敲一个字就请求一次。
  useEffect(() => {
    if (searchDraft === filters.search) return undefined;
    const timer = window.setTimeout(() => patchFilters({ search: searchDraft.trim() }), 320);
    return () => window.clearTimeout(timer);
  }, [searchDraft, filters.search, patchFilters]);

  const graphNeeded = view !== "table" || memberId !== null;
  const graph = useRelationGraph(groupId, graphNeeded);

  const listQuery = useApiQuery({
    queryKey: ["agent-relations", groupId, table, filters],
    fetcher: (signal) => {
      const params = new URLSearchParams({
        page: String(table.page),
        pageSize: String(table.pageSize),
        search: filters.search,
        sort: table.sort,
        order: table.order,
      });
      if (filters.type) params.set("type", filters.type);
      if (filters.source && !readOnly) params.set("source", filters.source);
      if (filters.member) params.set("member", filters.member);
      if (filters.minConfidence !== null) params.set("minConfidence", String(filters.minConfidence));
      if (filters.maxConfidence !== null) params.set("maxConfidence", String(filters.maxConfidence));
      return api<AgentRelationListItem[]>(`/agent/groups/${groupId}/relations?${params.toString()}`, { signal })
        .then((response) => ({ rows: response.data, total: response.meta.total ?? 0 }));
    },
    invalidation: { resources: ["agent_relation", "agent_member_data", "agent_group_data"], scope: { groupId } },
  });
  const summaryQuery = useApiQuery({
    queryKey: ["agent-relation-summary", groupId],
    fetcher: (signal) => api<AgentRelationSummary>(`/agent/groups/${groupId}/relations/summary`, { signal }).then((response) => response.data),
    invalidation: { resources: ["agent_relation", "agent_member_data", "agent_group_data"], scope: { groupId } },
  });
  const typesQuery = useApiQuery({
    queryKey: ["agent-relation-types", groupId],
    fetcher: (signal) => api<string[]>(`/agent/groups/${groupId}/relations/types`, { signal }).then((response) => response.data),
    invalidation: { resources: ["agent_relation"], scope: { groupId } },
  });

  // 名字缓存：图谱节点 + 列表行里的昵称都记下来，任何视图都能把 QQ 显示成人名。
  for (const node of graph.data?.nodes ?? []) nameCache.current.set(node.userId, nodeDisplayName(node, node.userId));
  for (const row of listQuery.data?.rows ?? []) {
    if (row.subjectName && !nameCache.current.has(row.subjectUserId)) nameCache.current.set(row.subjectUserId, row.subjectName);
    if (row.objectName && !nameCache.current.has(row.objectUserId)) nameCache.current.set(row.objectUserId, row.objectName);
  }
  const nameOf = useCallback((userId: string) => nameCache.current.get(userId) ?? userId, []);

  const locked = readOnly || listQuery.stale;
  const refreshAll = useCallback(() => {
    listQuery.reload();
    summaryQuery.reload();
    typesQuery.reload();
    graph.reload();
  }, [listQuery, summaryQuery, typesQuery, graph]);

  const removeEdges = useCallback(async (ids: string[]): Promise<boolean> => {
    if (locked || ids.length === 0) return false;
    try {
      if (ids.length === 1) {
        await api(`/agent/groups/${groupId}/relations/${ids[0]}`, { method: "DELETE" });
        message.success("关系边已删除");
      } else {
        const result = await api<{ deleted: number; missing: number }>(`/agent/groups/${groupId}/relations/batch-delete`, {
          method: "POST",
          body: JSON.stringify({ ids: ids.map(Number) }),
        });
        message.success(`已删除 ${result.data.deleted} 条关系边${result.data.missing ? `，${result.data.missing} 条已不存在` : ""}`);
      }
      setDetailEdge((current) => (current && ids.includes(current.id) ? null : current));
      refreshAll();
      return true;
    } catch (error) {
      message.error((error as Error).message);
      return false;
    }
  }, [locked, groupId, message, refreshAll]);

  const createReverse = useCallback(async (edges: AgentRelationItem[]) => {
    if (locked || edges.length === 0) return;
    let created = 0;
    let existed = 0;
    let failed = 0;
    for (const edge of edges) {
      try {
        await api(`/agent/groups/${groupId}/relations`, {
          method: "POST",
          body: JSON.stringify({
            subjectUserId: Number(edge.objectUserId),
            objectUserId: Number(edge.subjectUserId),
            type: edge.type,
            note: edge.note,
            confidence: edge.confidence,
          }),
        });
        created += 1;
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) existed += 1;
        else failed += 1;
      }
    }
    const parts = [`补全 ${created} 条反向关系`];
    if (existed) parts.push(`${existed} 条原本就有`);
    if (failed) parts.push(`${failed} 条失败`);
    (failed ? message.warning : message.success)(parts.join("，"));
    refreshAll();
  }, [locked, groupId, message, refreshAll]);

  const focusInGraph = useCallback((userId: string) => {
    setMemberId(null);
    setDetailEdge(null);
    setView("graph");
    setFocusRequest({ userId, nonce: Date.now() });
  }, [setView]);

  const openCreate = useCallback((value?: RelationCreatePrefill) => {
    if (locked) return;
    setPrefill(value);
    setEditorMode("create");
  }, [locked]);
  const openEdit = useCallback((edge: AgentRelationItem) => {
    if (locked) return;
    setDetailEdge(null);
    setEditing(edge);
    setEditorMode("edit");
  }, [locked]);

  const ctx: RelationsContext = {
    groupId,
    readOnly,
    locked,
    graph,
    filters,
    nameOf,
    patchFilters,
    openEdge: (edge) => { setMemberId(null); setDetailEdge(edge); },
    openMember: (userId) => { setDetailEdge(null); setMemberId(userId); },
    openCreate,
    openEdit,
    removeEdges,
    createReverse,
    focusInGraph,
    setView,
  };

  const exportData = async (format: "csv" | "json") => {
    setExporting(true);
    try {
      // 导出总是取一次最新全量，不依赖当前视图是否已加载图谱。
      const response = await api<AgentRelationGraph>(`/agent/groups/${groupId}/relations/graph`);
      const names = new Map(response.data.nodes.map((node) => [node.userId, nodeDisplayName(node, node.userId)]));
      const edges = filterEdges(response.data.edges, {
        types: filters.type ? new Set([filters.type]) : undefined,
        source: filters.source || undefined,
        member: filters.member || undefined,
        minConfidence: filters.minConfidence ?? undefined,
        maxConfidence: filters.maxConfidence ?? undefined,
        search: filters.search || undefined,
      }, (userId) => names.get(userId) ?? userId);
      const stamp = new Date().toISOString().slice(0, 10);
      if (format === "csv") {
        downloadText(`relations-${groupId}-${stamp}.csv`, relationsToCsv(edges, (userId) => names.get(userId) ?? userId, !readOnly), "text/csv;charset=utf-8");
      } else {
        const ids = new Set(edges.flatMap((edge) => [edge.subjectUserId, edge.objectUserId]));
        const nodes = response.data.nodes.filter((node) => ids.has(node.userId));
        downloadText(`relations-${groupId}-${stamp}.json`, JSON.stringify({ groupId, exportedAt: new Date().toISOString(), filters, nodes, edges }, null, 2), "application/json");
      }
      message.success(`已导出 ${edges.length} 条关系`);
    } catch (error) {
      message.error(`导出失败：${(error as Error).message}`);
    } finally {
      setExporting(false);
    }
  };

  // 面板快捷键：/ 聚焦搜索，N 新增，1–5 切视图；只在面板可见且未在输入时生效。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) return;
      const root = rootRef.current;
      if (!root || root.offsetParent === null || document.querySelector(".ant-drawer-open, .ant-modal-wrap:not([style*='none'])")) return;
      if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      } else if ((event.key === "n" || event.key === "N") && !readOnly) {
        event.preventDefault();
        openCreate();
      } else if (/^[1-5]$/.test(event.key)) {
        const target = RELATION_VIEWS[Number(event.key) - 1];
        if (target) setView(target.key);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openCreate, readOnly, setView]);

  const typeOptions = useMemo(() => {
    const counts = new Map((summaryQuery.data?.typeCounts ?? []).map((item) => [item.type, item.count]));
    return Array.from(new Set([...(typesQuery.data ?? []), ...RELATION_TYPE_PRESETS])).map((value) => ({
      value,
      label: <span className="rel-select-type"><span className="rel-type-dot" style={{ background: relationTypeColor(value) }} />{value}{counts.has(value) ? <span className="rel-legend-count">{counts.get(value)}</span> : null}</span>,
    }));
  }, [typesQuery.data, summaryQuery.data]);

  const filterCount = activeFilterCount(filters);
  const confidenceActive = filters.minConfidence !== null || filters.maxConfidence !== null;
  const graphFiltered = useMemo(() => {
    if (!graph.data) return null;
    const edges = filterEdges(graph.data.edges, {
      source: filters.source || undefined,
      minConfidence: filters.minConfidence ?? undefined,
      maxConfidence: filters.maxConfidence ?? undefined,
    });
    return edges.length === graph.data.edges.length ? graph.data : { ...graph.data, edges };
  }, [graph.data, filters.source, filters.minConfidence, filters.maxConfidence]);

  // 成员筛选在图谱里表现为“聚焦该成员”，而不是把其它人藏掉。
  useEffect(() => {
    if (view === "graph" && filters.member) setFocusRequest({ userId: filters.member, nonce: Date.now() });
  }, [view, filters.member]);

  const renderGraphBody = (render: () => React.ReactNode) => {
    if (graph.error && !graph.data) return <QueryErrorAlert error={graph.error} onRetry={graph.reload} />;
    if (!graph.data) return <div className="rg-loading-wrap"><Spin /></div>;
    return <Suspense fallback={<div className="rg-loading-wrap"><Spin /></div>}>{render()}</Suspense>;
  };

  const viewBody = () => {
    switch (view) {
      case "graph":
        return renderGraphBody(() => graphFiltered && (
          <LazyRelationGraphView
            graph={graphFiltered}
            typeFilter={filters.type}
            readOnly={locked || graph.stale}
            focusRequest={focusRequest}
            onOpenEdge={ctx.openEdge}
            onOpenMember={ctx.openMember}
            onCreateBetween={readOnly ? undefined : (subjectUserId, objectUserId) => openCreate({ subjectUserId, objectUserId })}
          />
        ));
      case "members":
        return renderGraphBody(() => graph.data && <LazyMembersView ctx={ctx} graph={graph.data} />);
      case "matrix":
        return renderGraphBody(() => graph.data && <LazyMatrixView ctx={ctx} graph={graph.data} />);
      case "insights":
        return renderGraphBody(() => graph.data && <LazyInsightsView ctx={ctx} graph={graph.data} />);
      default:
        return (
          <RelationTableView
            ctx={ctx}
            rows={listQuery.data?.rows ?? []}
            total={listQuery.data?.total ?? 0}
            loading={listQuery.loading}
            error={listQuery.error}
            reload={listQuery.reload}
            state={table}
            onStateChange={(patch) => setTable((current) => ({ ...current, ...patch }))}
            hasFilters={filterCount > 0}
            onResetFilters={resetFilters}
          />
        );
    }
  };

  const confidencePopover = (
    <div className="rel-conf-popover">
      <Text strong>置信度范围</Text>
      <Slider
        range
        min={0}
        max={1}
        step={0.05}
        value={[filters.minConfidence ?? 0, filters.maxConfidence ?? 1]}
        tooltip={{ formatter: (value) => `${Math.round(Number(value) * 100)}%` }}
        onChangeComplete={(value) => {
          const [min, max] = value as number[];
          patchFilters({ minConfidence: min > 0 ? min : null, maxConfidence: max < 1 ? max : null });
        }}
      />
      <Space size={4} wrap>
        <Tag.CheckableTag checked={filters.maxConfidence === 0.39 && filters.minConfidence === null} onChange={() => patchFilters({ minConfidence: null, maxConfidence: 0.39 })}>存疑 &lt;40%</Tag.CheckableTag>
        <Tag.CheckableTag checked={filters.minConfidence === 0.4 && filters.maxConfidence === 0.74} onChange={() => patchFilters({ minConfidence: 0.4, maxConfidence: 0.74 })}>较可信</Tag.CheckableTag>
        <Tag.CheckableTag checked={filters.minConfidence === 0.75 && filters.maxConfidence === null} onChange={() => patchFilters({ minConfidence: 0.75, maxConfidence: null })}>确定 ≥75%</Tag.CheckableTag>
        {confidenceActive && <Button size="small" type="link" onClick={() => patchFilters({ minConfidence: null, maxConfidence: null })}>不限</Button>}
      </Space>
    </div>
  );

  const chips: Array<{ key: keyof RelationFilters | "confidence"; label: React.ReactNode; clear: () => void }> = [];
  if (filters.type) chips.push({ key: "type", label: <><span className="rel-type-dot" style={{ background: relationTypeColor(filters.type) }} />类型：{filters.type}</>, clear: () => patchFilters({ type: "" }) });
  if (filters.source && !readOnly) chips.push({ key: "source", label: `来源：${RELATION_SOURCE_META[filters.source]?.label ?? filters.source}`, clear: () => patchFilters({ source: "" }) });
  if (filters.member) chips.push({ key: "member", label: `成员：${nameOf(filters.member)}`, clear: () => patchFilters({ member: "" }) });
  if (filters.search) chips.push({ key: "search", label: `搜索：${filters.search}`, clear: () => patchFilters({ search: "" }) });
  if (confidenceActive) chips.push({ key: "confidence", label: `置信度：${Math.round((filters.minConfidence ?? 0) * 100)}%–${Math.round((filters.maxConfidence ?? 1) * 100)}%`, clear: () => patchFilters({ minConfidence: null, maxConfidence: null }) });

  return (
    <div className="rel" ref={rootRef}>
      <RelationMetrics ctx={ctx} summary={summaryQuery.data} error={summaryQuery.error} />
      <section className="rel-card">
        <div className="rel-toolbar">
          <div className="rel-toolbar-row">
            <Segmented
              className="rel-view-switch"
              value={view}
              onChange={(value) => setView(value as RelationsView)}
              options={RELATION_VIEWS.map((item, index) => ({
                value: item.key,
                label: <Tooltip title={`${item.hint}（${index + 1}）`}><span className="rel-view-option">{VIEW_ICONS[item.key]}<span className="rel-view-full">{item.label}视图</span><span className="rel-view-short">{item.label}</span></span></Tooltip>,
              }))}
            />
            <div className="rel-toolbar-actions">
              <Tooltip title="刷新"><Button icon={<ReloadOutlined spin={listQuery.refreshing || graph.loading} />} aria-label="刷新" onClick={refreshAll} /></Tooltip>
              <Dropdown
                trigger={["click"]}
                menu={{
                  items: [
                    { key: "csv", label: "导出 CSV（按当前筛选）" },
                    { key: "json", label: "导出 JSON（含成员节点）" },
                  ],
                  onClick: ({ key }) => { void exportData(key as "csv" | "json"); },
                }}
              >
                <Button icon={<DownloadOutlined />} loading={exporting}>导出</Button>
              </Dropdown>
              {!readOnly && (
                <Tooltip title="快捷键 N">
                  <Button type="primary" icon={<PlusOutlined />} disabled={locked} onClick={() => openCreate()}>新增关系边</Button>
                </Tooltip>
              )}
            </div>
          </div>
          <div className="rel-toolbar-filters">
            <Input
              ref={searchRef}
              className="rel-search"
              allowClear
              placeholder="搜索成员昵称或 QQ（/）"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              onPressEnter={() => patchFilters({ search: searchDraft.trim() })}
              aria-label="搜索成员"
            />
            <Select
              className="rel-type-select"
              value={filters.type}
              onChange={(value) => patchFilters({ type: value })}
              options={[{ value: "", label: "全部类型" }, ...typeOptions]}
              popupMatchSelectWidth={false}
              aria-label="按类型筛选"
            />
            {!readOnly && (
              <Select
                className="rel-source-select"
                value={filters.source}
                onChange={(value) => patchFilters({ source: value })}
                options={[{ value: "", label: "全部来源" }, ...Object.entries(RELATION_SOURCE_META).map(([value, meta]) => ({ value, label: meta.label }))]}
                aria-label="按来源筛选"
              />
            )}
            <Popover content={confidencePopover} trigger="click" placement="bottomLeft">
              <Button icon={<FilterOutlined />} type={confidenceActive ? "primary" : "default"} ghost={confidenceActive}>置信度</Button>
            </Popover>
            <span className="rel-grow" />
            <Text type="secondary" className="rel-small rel-kbd-hint">
              <kbd>/</kbd> 搜索{readOnly ? "" : <> · <kbd>N</kbd> 新增</>} · <kbd>1</kbd>–<kbd>5</kbd> 切视图
            </Text>
          </div>
        </div>
        {chips.length > 0 && (
          <div className="rel-chips" aria-label="已生效的筛选">
            <Badge count={filterCount} size="small" color="var(--brand)"><FilterOutlined className="rel-chips-icon" /></Badge>
            {chips.map((chip) => (
              <span key={chip.key} className="rel-chip">
                {chip.label}
                <button type="button" className="rel-chip-close" aria-label="移除这个筛选" onClick={chip.clear}><CloseOutlined /></button>
              </span>
            ))}
            <Button type="link" size="small" onClick={resetFilters}>清除全部</Button>
          </div>
        )}
        <div className={`rel-view-body is-${view}`}>{viewBody()}</div>
      </section>
      {!readOnly && (
        <RelationEditorDrawer
          groupId={groupId}
          mode={editorMode ?? "create"}
          open={editorMode !== null}
          prefill={prefill}
          editing={editing}
          typeOptions={typesQuery.data ?? []}
          nameOf={nameOf}
          onClose={() => { setEditorMode(null); setEditing(null); }}
          onSaved={refreshAll}
        />
      )}
      <EdgeDetailDrawer ctx={ctx} edge={detailEdge} onClose={() => setDetailEdge(null)} />
      <MemberDrawer ctx={ctx} userId={memberId} onClose={() => setMemberId(null)} />
    </div>
  );
}
