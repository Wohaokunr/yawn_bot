// 关系指标条：轻量摘要接口驱动，不需要全量图谱。数字可点击直达对应筛选/视图。
import { Skeleton, Tooltip, Typography } from "antd";
import { RELATION_SOURCE_META } from "../agent-meta";
import { relationTypeColor } from "../relation-meta";
import type { AgentRelationSummary } from "../types";
import { DistributionBar } from "./RelationCharts";
import { relativeTime } from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;

interface Kpi {
  key: string;
  label: string;
  value: string;
  hint: string;
  tone?: "warn" | "good";
  onClick?: () => void;
  tip: string;
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function RelationMetrics({
  ctx,
  summary,
  error,
}: {
  ctx: RelationsContext;
  summary: AgentRelationSummary | null;
  error: string;
}): React.JSX.Element {
  if (!summary) {
    return (
      <div className="rel-metrics">
        {error ? <Text type="secondary">关系统计加载失败：{error}</Text> : <Skeleton active paragraph={{ rows: 1 }} title={false} />}
      </div>
    );
  }
  const edges = summary.edgeCount;
  const members = summary.linkedMemberCount;
  const review = (summary.staleEdgeCount ?? 0) + (summary.lowConfidenceCount ?? 0);
  const kpis: Kpi[] = [
    { key: "edges", label: "关系边", value: String(edges), hint: `${summary.typeCounts.length} 种类型`, tip: "当前可见的全部关系边（已排除隐私退出成员）" },
    { key: "members", label: "关系成员", value: String(members), hint: members > 1 ? `人均 ${(edges / members).toFixed(1)} 条` : "—", tip: "至少出现在一条关系边里的成员数", onClick: () => ctx.setView("members") },
    {
      key: "mutual",
      label: "互相成立",
      value: summary.mutualEdgeCount === undefined || edges === 0 ? "—" : percent(summary.mutualEdgeCount / edges),
      hint: summary.mutualEdgeCount === undefined ? "旧后端未提供" : `${summary.mutualEdgeCount} 条有反向边`,
      tip: "A→B 同时存在同类型 B→A 的比例",
    },
    {
      key: "confidence",
      label: "平均置信",
      value: summary.avgConfidence == null ? "—" : percent(summary.avgConfidence),
      hint: summary.lowConfidenceCount === undefined ? "" : `${summary.lowConfidenceCount} 条存疑`,
      tone: summary.avgConfidence != null && summary.avgConfidence < 0.5 ? "warn" : undefined,
      tip: "点击只看置信度低于 40% 的关系",
      onClick: summary.lowConfidenceCount ? () => { ctx.patchFilters({ minConfidence: null, maxConfidence: 0.39 }); ctx.setView("table"); } : undefined,
    },
    {
      key: "fresh",
      label: "近 7 天活跃",
      value: summary.freshEdgeCount === undefined ? "—" : String(summary.freshEdgeCount),
      hint: summary.lastSeenAt ? `最近 ${relativeTime(summary.lastSeenAt)}` : "暂无记录",
      tone: summary.freshEdgeCount ? "good" : undefined,
      tip: "最近 7 天内被再次见到或更新的关系边",
    },
    {
      key: "review",
      label: "待复核",
      value: summary.staleEdgeCount === undefined ? "—" : String(review),
      hint: summary.staleEdgeCount === undefined ? "" : `${summary.staleEdgeCount} 条超 60 天未见`,
      tone: review > 0 ? "warn" : undefined,
      tip: "长期未见 + 低置信，点击到洞察页集中处理",
      onClick: review > 0 ? () => ctx.setView("insights") : undefined,
    },
  ];
  return (
    <div className="rel-metrics" aria-label="关系概况">
      <div className="rel-kpis">
        {kpis.map((kpi) => {
          const body = <>
            <span className="rel-kpi-label">{kpi.label}</span>
            <span className="rel-kpi-value">{kpi.value}</span>
            <span className="rel-kpi-hint">{kpi.hint}</span>
          </>;
          return (
            <Tooltip key={kpi.key} title={kpi.tip}>
              {kpi.onClick
                ? <button type="button" className={`rel-kpi is-clickable${kpi.tone ? ` is-${kpi.tone}` : ""}`} onClick={kpi.onClick}>{body}</button>
                : <div className={`rel-kpi${kpi.tone ? ` is-${kpi.tone}` : ""}`}>{body}</div>}
            </Tooltip>
          );
        })}
      </div>
      <div className="rel-dist">
        <div className="rel-dist-head">
          <Text className="rel-dist-title">类型分布</Text>
          <Text type="secondary" className="rel-small">点色条或标签按类型筛选{ctx.filters.type ? "，再点一次取消" : ""}</Text>
        </div>
        <DistributionBar
          ariaLabel="关系类型分布"
          active={ctx.filters.type || undefined}
          items={summary.typeCounts.map((item) => ({ key: item.type, label: item.type, count: item.count }))}
          onSelect={(type) => ctx.patchFilters({ type: ctx.filters.type === type ? "" : type })}
        />
        <div className="rel-dist-legend">
          {summary.typeCounts.map((item) => (
            <button
              key={item.type}
              type="button"
              className={`rel-legend-chip${ctx.filters.type === item.type ? " is-active" : ""}${ctx.filters.type && ctx.filters.type !== item.type ? " is-dimmed" : ""}`}
              onClick={() => ctx.patchFilters({ type: ctx.filters.type === item.type ? "" : item.type })}
              aria-pressed={ctx.filters.type === item.type}
            >
              <span className="rel-type-dot" style={{ background: relationTypeColor(item.type) }} />
              {item.type}<span className="rel-legend-count">{item.count}</span>
            </button>
          ))}
          {summary.typeCounts.length === 0 && <Text type="secondary">暂无关系记忆</Text>}
        </div>
        {!ctx.readOnly && summary.sourceCounts && summary.sourceCounts.length > 0 && (
          <div className="rel-source-line">
            <Text type="secondary" className="rel-small">来源</Text>
            {summary.sourceCounts.map((item) => (
              <button
                key={item.source}
                type="button"
                className={`rel-source-chip${ctx.filters.source === item.source ? " is-active" : ""}`}
                onClick={() => ctx.patchFilters({ source: ctx.filters.source === item.source ? "" : item.source })}
                aria-pressed={ctx.filters.source === item.source}
              >
                {RELATION_SOURCE_META[item.source]?.label ?? item.source} {item.count}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
