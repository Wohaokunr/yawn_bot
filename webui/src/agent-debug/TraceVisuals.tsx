import { CheckCircleFilled, CloseCircleFilled, InfoCircleFilled, WarningFilled } from "@ant-design/icons";
import { Button, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import { AdminEmpty } from "../shared";
import type { AgentExecutionTrace, AgentExecutionTraceStats } from "../types";
import { TRACE_STATUS_META } from "./debug-utils";
import {
  formatCompact,
  formatDuration,
  phaseBreakdown,
  phaseLabel,
  type TraceInsight,
  type TraceKpis,
  waterfallRows,
} from "./trace-analysis";

const { Text } = Typography;

export function TraceKpiRow({ kpis }: { kpis: TraceKpis }): React.JSX.Element {
  const cacheRate = kpis.promptTokens && kpis.cachedTokens != null ? Math.round((kpis.cachedTokens / kpis.promptTokens) * 100) : null;
  const items: Array<{ key: string; label: string; value: string; hint?: string; tone?: string }> = [
    { key: "duration", label: "总耗时", value: formatDuration(kpis.durationMs) },
    { key: "events", label: "事件", value: String(kpis.events) },
    { key: "llm", label: "模型轮数", value: String(kpis.llmRounds) },
    { key: "tools", label: "工具调用", value: String(kpis.toolCalls) },
    { key: "failed", label: "失败事件", value: String(kpis.failedEvents), tone: kpis.failedEvents > 0 ? "error" : undefined },
    {
      key: "tokens",
      label: "输入 Token",
      value: formatCompact(kpis.promptTokens),
      // 入/出两个数挤在一格里放不下，输出量和缓存命中率放到提示行。
      hint: [kpis.completionTokens != null ? `输出 ${formatCompact(kpis.completionTokens)}` : "", cacheRate != null ? `缓存 ${cacheRate}%` : ""].filter(Boolean).join(" · ") || undefined,
    },
  ];
  return <div className="dbg-kpis">
    {items.map((item) => <div key={item.key} className={`dbg-kpi${item.tone ? ` is-${item.tone}` : ""}`}>
      <span className="dbg-kpi-label">{item.label}</span>
      <strong className="dbg-kpi-value" title={item.value}>{item.value}</strong>
      {item.hint && <span className="dbg-kpi-hint">{item.hint}</span>}
    </div>)}
  </div>;
}

export function TraceWaterfall({
  trace,
  onSelectEvent,
}: {
  trace: AgentExecutionTrace;
  onSelectEvent?: (eventId: string) => void;
}): React.JSX.Element {
  const { totalMs, rows } = useMemo(() => waterfallRows(trace), [trace]);
  if (rows.length === 0) return <AdminEmpty description="没有可绘制的事件" />;
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return <div className="dbg-waterfall" role="table" aria-label="事件瀑布图">
    <div className="dbg-wf-row dbg-wf-axis" role="row">
      <span className="dbg-wf-label" role="columnheader">事件</span>
      <span className="dbg-wf-track" role="columnheader">
        {ticks.map((tick) => <span key={tick} className="dbg-wf-tick" style={{ left: `${tick * 100}%` }}>{tick === 0 ? "0" : formatDuration(totalMs * tick)}</span>)}
      </span>
    </div>
    {rows.map((row, index) => {
      const meta = TRACE_STATUS_META[row.event.status] ?? { label: row.event.status, color: "default" };
      const tooltip = `${phaseLabel(row.event.phase)} · ${row.event.label} · ${meta.label}\n开始 +${formatDuration(row.startMs)}，结束 +${formatDuration(row.endMs)}${row.instant ? "（瞬时事件）" : `，耗时 ${formatDuration(row.event.durationMs)}`}`;
      return <div key={row.event.id} className="dbg-wf-row" role="row">
        <span className="dbg-wf-label" role="cell">
          <span className="dbg-wf-index">{index + 1}</span>
          <Tag className={`dbg-phase-tag phase-${row.event.phase}`}>{phaseLabel(row.event.phase)}</Tag>
          <span className="dbg-wf-name" title={row.event.label}>{row.event.label}</span>
        </span>
        <span className="dbg-wf-track" role="cell">
          {ticks.slice(1, -1).map((tick) => <span key={tick} className="dbg-wf-grid" style={{ left: `${tick * 100}%` }} />)}
          <Tooltip title={<span style={{ whiteSpace: "pre-line" }}>{tooltip}</span>}>
            <button
              type="button"
              className={`dbg-wf-bar phase-${row.event.phase} status-${row.event.status}${row.instant ? " is-instant" : ""}`}
              style={{ left: `${row.startPct}%`, width: row.instant ? undefined : `${row.widthPct}%` }}
              aria-label={`${row.event.label}，${row.instant ? "瞬时" : formatDuration(row.event.durationMs)}`}
              onClick={() => onSelectEvent?.(row.event.id)}
            />
          </Tooltip>
          {!row.instant && <span
            className="dbg-wf-value"
            // 靠近右端的长条把耗时标在条形左侧，避免压在条形上。
            style={row.startPct + row.widthPct > 78 ? { right: `calc(${100 - row.startPct}% + 6px)` } : { left: `calc(${row.startPct + row.widthPct}% + 6px)` }}
          >{formatDuration(row.event.durationMs)}</span>}
        </span>
      </div>;
    })}
    <Text type="secondary" className="dbg-small">条形从“开始”画到“记录时刻”；没有耗时的事件显示为圆点。点击条形跳到时间线中的对应事件。</Text>
  </div>;
}

export function PhaseBreakdownView({
  trace,
  stats,
}: {
  trace: AgentExecutionTrace;
  stats: AgentExecutionTraceStats | null;
}): React.JSX.Element {
  const rows = useMemo(() => phaseBreakdown(trace), [trace]);
  const bufferAvg = useMemo(() => new Map((stats?.phases ?? []).map((item) => [item.phase, item.avgMs])), [stats]);
  const measured = rows.some((row) => row.totalMs > 0);
  if (rows.length === 0) return <AdminEmpty description="没有事件" />;
  return <div className="dbg-phases">
    {!measured && <Text type="secondary">这条 Trace 的事件都没有记录耗时，只能看数量分布。</Text>}
    {rows.map((row) => {
      const avg = bufferAvg.get(row.phase);
      const perEvent = row.count > 0 ? row.totalMs / row.count : 0;
      const slower = avg != null && avg > 0 && perEvent > avg * 1.5;
      return <div key={row.phase} className="dbg-phase-row">
        <div className="dbg-phase-head">
          <Tag className={`dbg-phase-tag phase-${row.phase}`}>{row.label}</Tag>
          <Text strong>{formatDuration(row.totalMs)}</Text>
          <Text type="secondary">{Math.round(row.share * 100)}% · {row.count} 个事件{row.maxMs > 0 ? ` · 最长 ${formatDuration(row.maxMs)}` : ""}</Text>
          {row.failures > 0 && <Tag color="red">失败 {row.failures}</Tag>}
          {avg != null && <Text type={slower ? "warning" : "secondary"} className="dbg-phase-avg">
            缓冲平均每事件 {formatDuration(avg)}{slower ? "，这次明显偏慢" : ""}
          </Text>}
        </div>
        <div className="dbg-meter" aria-hidden="true"><span className={`phase-${row.phase}`} style={{ width: `${Math.max(row.share * 100, row.totalMs > 0 ? 1.5 : 0)}%` }} /></div>
      </div>;
    })}
  </div>;
}

const INSIGHT_ICON: Record<TraceInsight["level"], React.ReactNode> = {
  error: <CloseCircleFilled />,
  warning: <WarningFilled />,
  info: <InfoCircleFilled />,
  success: <CheckCircleFilled />,
};

export function TraceDiagnosis({
  insights,
  onSelectEvent,
  compact = false,
}: {
  insights: TraceInsight[];
  onSelectEvent?: (eventId: string) => void;
  compact?: boolean;
}): React.JSX.Element {
  const shown = compact ? insights.filter((item) => item.level !== "info").slice(0, 3) : insights;
  if (shown.length === 0) return <></>;
  return <ul className={`dbg-insights${compact ? " is-compact" : ""}`}>
    {shown.map((insight) => <li key={insight.key} className={`dbg-insight is-${insight.level}`}>
      <span className="dbg-insight-icon" aria-hidden="true">{INSIGHT_ICON[insight.level]}</span>
      <div className="dbg-insight-copy">
        <strong>{insight.title}</strong>
        <span>{insight.detail}</span>
      </div>
      {insight.eventId && onSelectEvent && <Button size="small" type="link" onClick={() => onSelectEvent(insight.eventId ?? "")}>定位事件</Button>}
    </li>)}
  </ul>;
}
