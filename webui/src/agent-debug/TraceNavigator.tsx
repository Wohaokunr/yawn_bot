import {
  ApiOutlined,
  CloseCircleFilled,
  FilterOutlined,
  PauseCircleOutlined,
  PictureOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SendOutlined,
  StarFilled,
  StarOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Alert, Badge, Button, Input, type InputRef, Select, Skeleton, Switch, Tag, Tooltip, Typography } from "antd";
import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import { AdminEmpty, formatTime, QueryErrorAlert } from "../shared";
import type { AgentExecutionTraceSummary } from "../types";
import { AGENT_DEBUG_MODES, agentDebugModeLabel, TRACE_STATUS_META, traceOutcomeLabel, TRACE_TRIGGER_SOURCE_LABELS, triggerSourceLabel } from "./debug-utils";
import { formatDuration, relativeTime, statusFilterCounts, TRACE_SORT_OPTIONS, TRACE_STATUS_FILTERS, type TraceSort } from "./trace-analysis";
import { TRACE_POLL_OPTIONS, type ExecutionTracesState } from "./useExecutionTraces";

const { Text } = Typography;

function traceVisualStatus(trace: AgentExecutionTraceSummary): { label: string; color: string; tone: string } {
  if (trace.hasFailure) return { label: "失败", color: "red", tone: "error" };
  if (trace.hasDegradation || trace.hasOutboundProblem) return { label: trace.hasOutboundProblem ? "发送异常" : "降级", color: "orange", tone: "warning" };
  if (trace.status === "running") return { label: "执行中", color: "processing", tone: "running" };
  const meta = TRACE_STATUS_META[trace.status] ?? { label: trace.status, color: "default" };
  return { ...meta, tone: "ok" };
}

export const TraceNavigator = forwardRef<InputRef, {
  traces: ExecutionTracesState;
  onOpenLab: () => void;
}>(function TraceNavigator({ traces, onOpenLab }, searchRef) {
  const [showFilters, setShowFilters] = useState(() => Boolean(traces.filters.mode || traces.filters.triggerSource || traces.filters.actorUserId));
  const counts = useMemo(() => statusFilterCounts(traces.summaries), [traces.summaries]);
  const maxDuration = useMemo(
    () => traces.summaries.reduce((max, trace) => Math.max(max, trace.durationMs ?? 0), 0),
    [traces.summaries],
  );
  const listRef = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  // 键盘切换或深链进入时，让选中项滚进可视区。
  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(".dbg-nav-item.is-selected");
    node?.scrollIntoView?.({ block: "nearest" });
  }, [traces.selectedTraceId]);

  const triggerOptions = useMemo(() => [
    { value: "", label: "全部触发来源" },
    ...Object.keys(TRACE_TRIGGER_SOURCE_LABELS).map((key) => ({ value: key, label: TRACE_TRIGGER_SOURCE_LABELS[key] })),
  ], []);
  const live = traces.autoRefresh && !traces.pageHidden;

  return <section className="dbg-panel dbg-nav" aria-label="Trace 列表">
    <header className="dbg-panel-head">
      <div className="dbg-panel-title">
        <span>Trace 列表</span>
        <Tag className="dbg-count-tag">{traces.visibleSummaries.length}{traces.visibleSummaries.length !== traces.summaries.length ? ` / ${traces.summaries.length}` : ""}</Tag>
      </div>
      <div className="dbg-panel-actions">
        <Tooltip title={live ? `每 ${traces.pollInterval / 1000} 秒自动刷新（p 暂停）` : traces.pageHidden ? "标签页在后台，已暂停" : "自动刷新已暂停（p 继续）"}>
          <Button
            size="small"
            type="text"
            aria-label={traces.autoRefresh ? "暂停自动刷新" : "开启自动刷新"}
            className={live ? "dbg-live is-on" : "dbg-live"}
            icon={traces.autoRefresh ? <PauseCircleOutlined /> : <PlayCircleOutlined />}
            onClick={() => traces.setAutoRefresh(!traces.autoRefresh)}
          />
        </Tooltip>
        <Tooltip title="刷新（r）">
          <Button size="small" type="text" aria-label="刷新 Trace" icon={<ReloadOutlined spin={traces.listRefreshing} />} onClick={traces.reloadSelected} />
        </Tooltip>
      </div>
    </header>

    <div className="dbg-nav-controls">
      <Input
        ref={searchRef}
        allowClear
        size="middle"
        value={traces.search}
        onChange={(event) => traces.setSearch(event.target.value)}
        placeholder="搜索消息预览、工具、Trace ID…（/）"
        aria-label="搜索 Trace"
        suffix={<Tooltip title="更多筛选"><Badge dot={Boolean(traces.filters.mode || traces.filters.triggerSource || traces.filters.actorUserId || traces.starredOnly)} offset={[-2, 2]}>
          <Button size="small" type="text" aria-label="更多筛选" aria-expanded={showFilters} icon={<FilterOutlined />} onClick={() => setShowFilters((value) => !value)} />
        </Badge></Tooltip>}
      />
      <div className="dbg-chip-row" role="radiogroup" aria-label="按状态筛选">
        {TRACE_STATUS_FILTERS.map((filter) => {
          const active = traces.status === filter.value;
          const count = counts[filter.value] ?? 0;
          if (filter.value && count === 0 && !active) return null;
          return <button
            key={filter.value || "all"}
            type="button"
            role="radio"
            aria-checked={active}
            className={`dbg-chip${active ? " is-active" : ""}${filter.value === "failed" && count > 0 ? " is-danger" : ""}`}
            onClick={() => traces.setStatus(active && filter.value ? "" : filter.value)}
          >
            {filter.label}<span className="dbg-chip-count">{active || !traces.status ? count : "·"}</span>
          </button>;
        })}
      </div>
      {showFilters && <div className="dbg-nav-filters">
        <Select size="small" value={traces.filters.mode} onChange={(value) => traces.setFilter("mode", value)} options={[{ value: "", label: "全部场景" }, ...AGENT_DEBUG_MODES]} aria-label="按场景筛选" />
        <Select size="small" value={traces.filters.triggerSource} onChange={(value) => traces.setFilter("triggerSource", value)} options={triggerOptions} aria-label="按触发来源筛选" />
        <Input
          size="small"
          allowClear
          value={traces.filters.actorUserId}
          onChange={(event) => traces.setFilter("actorUserId", event.target.value.replace(/\D/g, ""))}
          placeholder="发言人 QQ"
          inputMode="numeric"
          aria-label="按发言人筛选"
        />
        <Select size="small" value={traces.sort} onChange={(value: TraceSort) => traces.setSort(value)} options={TRACE_SORT_OPTIONS} aria-label="排序" />
        <Select
          size="small"
          value={traces.pollInterval}
          onChange={traces.setPollInterval}
          options={TRACE_POLL_OPTIONS.map((value) => ({ value, label: `每 ${value / 1000} 秒刷新` }))}
          aria-label="自动刷新间隔"
        />
        <label className="dbg-inline-switch">
          <Switch size="small" checked={traces.starredOnly} onChange={traces.setStarredOnly} />
          <span>只看收藏</span>
        </label>
        {traces.activeFilterCount > 0 && <Button size="small" type="link" onClick={traces.clearFilters}>清除全部筛选</Button>}
      </div>}
    </div>

    {traces.newCount > 0 && <button type="button" className="dbg-new-banner" onClick={traces.acknowledgeNew}>
      有 {traces.newCount} 条新 Trace，点击跳到最新
    </button>}

    <div className="dbg-nav-list" ref={listRef}>
      {traces.selectedTraceUnavailable && <Alert
        type="info"
        showIcon
        className="dbg-inline-alert"
        message="当前 Trace 已离开缓冲或当前筛选"
        description="已加载的详情会继续保留，不会自动跳回最新。"
      />}
      {traces.listError && traces.summaries.length === 0
        ? <QueryErrorAlert error={traces.listError} onRetry={traces.reload} />
        : traces.listLoading && traces.summaries.length === 0
          ? <Skeleton active paragraph={{ rows: 6 }} />
          : traces.visibleSummaries.length === 0
            ? <AdminEmpty
              description={traces.summaries.length === 0 ? "暂无真实执行 Trace。让 Agent 处理一条触发消息，或者先在实验室模拟一轮。" : "没有符合当前筛选的 Trace"}
              action={traces.summaries.length === 0
                ? <Button type="primary" onClick={onOpenLab}>去模拟一轮</Button>
                : <Button onClick={traces.clearFilters}>清除筛选</Button>}
            />
            : <div role="listbox" aria-label="Trace" className="dbg-nav-items">
              {traces.visibleSummaries.map((trace) => <TraceNavItem
                key={trace.traceId}
                trace={trace}
                now={now}
                maxDuration={maxDuration}
                starred={traces.starred.includes(trace.traceId)}
                selected={trace.traceId === traces.selectedTraceId}
                onSelect={() => traces.setSelectedTraceId(trace.traceId)}
                onToggleStar={() => traces.toggleStar(trace.traceId)}
              />)}
            </div>}
      {traces.listError && traces.summaries.length > 0 && <Text type="danger" className="dbg-small">刷新列表失败：{traces.listError}</Text>}
      <Text type="secondary" className="dbg-buffer-note">
        Trace 只保存在当前 Bot 进程的内存里，每个群最近 {traces.stats?.capacity ?? 12} 条；列表只加载摘要，选中后才拉完整事件。
      </Text>
    </div>
  </section>;
});

function TraceNavItem({
  trace,
  now,
  maxDuration,
  starred,
  selected,
  onSelect,
  onToggleStar,
}: {
  trace: AgentExecutionTraceSummary;
  now: number;
  maxDuration: number;
  starred: boolean;
  selected: boolean;
  onSelect: () => void;
  onToggleStar: () => void;
}): React.JSX.Element {
  const status = traceVisualStatus(trace);
  const width = maxDuration > 0 && trace.durationMs != null ? Math.max(4, (trace.durationMs / maxDuration) * 100) : 0;
  return <div
    role="option"
    tabIndex={0}
    aria-selected={selected}
    className={`dbg-nav-item tone-${status.tone}${selected ? " is-selected" : ""}`}
    onClick={onSelect}
    onKeyDown={(event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        onSelect();
      }
    }}
  >
    <div className="dbg-nav-item-top">
      <Tooltip title={formatTime(trace.startedAt)}><Text strong className="dbg-nav-time">{relativeTime(trace.startedAt, now)}</Text></Tooltip>
      <Tag className="dbg-tag-soft">{agentDebugModeLabel(trace.mode)}</Tag>
      <Tag color={status.color}>{status.label}</Tag>
      <button
        type="button"
        className={`dbg-star${starred ? " is-on" : ""}`}
        aria-label={starred ? "取消收藏" : "收藏"}
        aria-pressed={starred}
        onClick={(event) => {
          event.stopPropagation();
          onToggleStar();
        }}
      >{starred ? <StarFilled /> : <StarOutlined />}</button>
    </div>
    <div className={`dbg-nav-preview${trace.preview ? "" : " is-empty"}`}>
      {trace.preview || (trace.triggerSource ? triggerSourceLabel(trace.triggerSource) : traceOutcomeLabel(trace.outcome ?? trace.status))}
    </div>
    <div className="dbg-nav-meta">
      <span className="dbg-nav-duration">
        <span className="dbg-nav-duration-bar"><span style={{ width: `${width}%` }} /></span>
        {formatDuration(trace.durationMs)}
      </span>
      <span>{trace.eventCount} 事件</span>
      {trace.triggerSource && trace.preview && <span>{triggerSourceLabel(trace.triggerSource)}</span>}
      <span className="dbg-nav-flags">
        {trace.hasTool && <Tooltip title={trace.toolNames?.length ? `工具：${trace.toolNames.join("、")}` : "调用了工具"}><ToolOutlined aria-label="工具" /></Tooltip>}
        {trace.hasMedia && <Tooltip title="包含媒体"><PictureOutlined aria-label="媒体" /></Tooltip>}
        {trace.hasOutboundProblem && <Tooltip title="发送异常"><SendOutlined aria-label="发送异常" className="is-warn" /></Tooltip>}
        {trace.hasFailure && <Tooltip title="有失败事件"><CloseCircleFilled aria-label="失败" className="is-error" /></Tooltip>}
        {(trace.llmRounds ?? 0) > 1 && <Tooltip title={`模型 ${trace.llmRounds} 轮`}><span className="dbg-rounds"><ApiOutlined /> {trace.llmRounds}</span></Tooltip>}
      </span>
    </div>
  </div>;
}
