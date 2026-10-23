// 洞察视图：健康分 + 网络结构 + 关键人物 + 小圈子 + 待复核队列 + 分布图表。
// 所有数字都来自全量图谱的前端计算，与筛选条件联动（类型/来源/置信度）。
import { DeleteOutlined, RetweetOutlined } from "@ant-design/icons";
import { Button, Checkbox, Empty, Popconfirm, Segmented, Space, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { RELATION_SOURCE_META } from "../agent-meta";
import { relationTypeColor } from "../relation-meta";
import type { AgentRelationGraph, AgentRelationItem } from "../types";
import { DistributionBar, Meter, MiniBars, ScoreRing } from "./RelationCharts";
import { ConfidencePill, FreshnessText } from "./RelationDetailDrawers";
import {
  betweenness, communityColor, confidenceHistogram, detectCommunities, detectConflicts, filterEdges,
  freshnessBuckets, FRESHNESS_LABEL, healthScore, lowConfidenceEdges, memberProfiles, networkStats,
  oneSidedEdges, staleEdges, typeStats, weeklyActivity,
} from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;

type QueueKey = "conflict" | "oneSided" | "stale" | "low";

function Panel({ title, extra, children, className }: { title: React.ReactNode; extra?: React.ReactNode; children: React.ReactNode; className?: string }): React.JSX.Element {
  return (
    <section className={`rel-panel${className ? ` ${className}` : ""}`}>
      <div className="rel-panel-head"><span className="rel-panel-title">{title}</span>{extra}</div>
      {children}
    </section>
  );
}

function PersonList({ ctx, items, metric }: { ctx: RelationsContext; items: Array<{ userId: string; name: string; value: number }>; metric: (value: number) => string }): React.JSX.Element {
  if (items.length === 0) return <Text type="secondary" className="rel-small">暂无</Text>;
  const max = Math.max(1, ...items.map((item) => item.value));
  return (
    <ol className="rel-rank">
      {items.map((item, index) => (
        <li key={item.userId}>
          <button type="button" className="rel-rank-row" onClick={() => ctx.openMember(item.userId)}>
            <span className="rel-rank-no">{index + 1}</span>
            <span className="rel-avatar is-xs" aria-hidden="true" data-initial={item.name.slice(0, 1)} />
            <span className="rel-ellipsis rel-rank-name">{item.name}</span>
            <span className="rel-rank-bar" aria-hidden="true"><span style={{ width: `${(item.value / max) * 100}%` }} /></span>
            <span className="rel-rank-value">{metric(item.value)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

export function RelationInsightsView({ ctx, graph }: { ctx: RelationsContext; graph: AgentRelationGraph }): React.JSX.Element {
  const [queue, setQueue] = useState<QueueKey>("conflict");
  const [checked, setChecked] = useState<string[]>([]);
  const filters = ctx.filters;
  const now = Date.now();

  const edges = useMemo(() => filterEdges(graph.edges, {
    types: filters.type ? new Set([filters.type]) : undefined,
    source: filters.source || undefined,
    minConfidence: filters.minConfidence ?? undefined,
    maxConfidence: filters.maxConfidence ?? undefined,
    member: filters.member || undefined,
  }), [graph.edges, filters.type, filters.source, filters.minConfidence, filters.maxConfidence, filters.member]);
  const scoped = useMemo<AgentRelationGraph>(() => ({ ...graph, edges }), [graph, edges]);

  const analysis = useMemo(() => {
    const ids = graph.nodes.map((node) => node.userId);
    const stats = networkStats(scoped, now);
    const communities = detectCommunities(ids, edges);
    const bridges = betweenness(ids, edges);
    const profiles = memberProfiles(scoped, communities, bridges).filter((profile) => profile.total > 0);
    const groups = new Map<number, string[]>();
    for (const profile of profiles) {
      if (profile.community === undefined) continue;
      const bucket = groups.get(profile.community);
      if (bucket) bucket.push(profile.userId);
      else groups.set(profile.community, [profile.userId]);
    }
    const communityList = [...groups.entries()]
      .filter(([, members]) => members.length >= 2)
      .map(([index, members]) => {
        const inside = new Set(members);
        const internal = edges.filter((edge) => inside.has(edge.subjectUserId) && inside.has(edge.objectUserId));
        const types = typeStats(internal);
        return { index, members, internal: internal.length, dominant: types[0]?.type ?? "" };
      })
      .sort((a, b) => b.members.length - a.members.length);
    return { stats, health: healthScore(stats), profiles, communityList };
    // now 在同一次渲染里固定即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph.nodes, scoped, edges]);

  const queues = useMemo(() => ({
    conflict: detectConflicts(edges),
    oneSided: oneSidedEdges(edges),
    stale: staleEdges(edges, now),
    low: lowConfidenceEdges(edges),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [edges]);

  const { stats, health, profiles, communityList } = analysis;
  if (stats.edgeCount === 0) return <Empty description="当前筛选下没有关系，无法生成洞察" />;

  const top = <K extends "total" | "incoming" | "bridge">(key: K) => [...profiles]
    .filter((profile) => profile[key] > 0)
    .sort((a, b) => b[key] - a[key] || a.userId.localeCompare(b.userId))
    .slice(0, 5)
    .map((profile) => ({ userId: profile.userId, name: profile.name, value: profile[key] }));

  const queueEdges: AgentRelationItem[] = queue === "conflict"
    ? queues.conflict.flatMap((item) => item.edges)
    : queue === "oneSided" ? queues.oneSided : queue === "stale" ? queues.stale : queues.low;
  const visibleChecked = checked.filter((id) => queueEdges.some((edge) => edge.id === id));
  const writable = !ctx.readOnly && !ctx.locked;
  const histogram = confidenceHistogram(edges);
  const weeks = weeklyActivity(edges, now);
  const freshness = freshnessBuckets(edges, now);
  const types = typeStats(edges);
  const sources = new Map<string, number>();
  for (const edge of edges) if (edge.sourceKind) sources.set(edge.sourceKind, (sources.get(edge.sourceKind) ?? 0) + 1);

  const renderEdgeRow = (edge: AgentRelationItem) => (
    <div key={edge.id} className="rel-queue-row">
      {!ctx.readOnly && (
        <Checkbox
          checked={visibleChecked.includes(edge.id)}
          disabled={ctx.locked}
          onChange={(event) => setChecked((current) => event.target.checked ? [...current, edge.id] : current.filter((id) => id !== edge.id))}
          aria-label="选择这条关系"
        />
      )}
      <button type="button" className="rel-queue-main" onClick={() => ctx.openEdge(edge)}>
        <span className="rel-ellipsis">{ctx.nameOf(edge.subjectUserId)}</span>
        <Tag color={relationTypeColor(edge.type)} className="rel-cell-type">{edge.type}</Tag>
        <span className="rel-ellipsis">{ctx.nameOf(edge.objectUserId)}</span>
      </button>
      <ConfidencePill value={edge.confidence} />
      <FreshnessText iso={edge.lastSeenAt} />
    </div>
  );

  return (
    <div className="rel-insights">
      <div className="rel-insights-top">
        <Panel title="关系数据健康度" className="rel-health">
          <div className="rel-health-body">
            <ScoreRing score={health.score} label="健康分" />
            <div className="rel-health-factors">
              {health.factors.map((factor) => (
                <div key={factor.key} className="rel-health-factor">
                  <div className="rel-health-factor-head"><Text>{factor.label}</Text><Text type="secondary">{Math.round(factor.score * 100)}</Text></div>
                  <Meter value={factor.score} label={factor.label} color={factor.score >= 0.75 ? "var(--rel-good)" : factor.score >= 0.5 ? "var(--rel-warn)" : "var(--rel-bad)"} />
                  <Text type="secondary" className="rel-small">{factor.hint}</Text>
                </div>
              ))}
            </div>
          </div>
        </Panel>
        <Panel title="网络结构">
          <div className="rel-fact-grid">
            <Tooltip title="实际有关系的成员对 ÷ 所有可能的成员对"><div><span>密度</span><strong>{(stats.density * 100).toFixed(1)}%</strong></div></Tooltip>
            <Tooltip title="每人平均认识几位（按成员对去重）"><div><span>平均度</span><strong>{stats.avgDegree.toFixed(1)}</strong></div></Tooltip>
            <Tooltip title="互不相连的小团体数"><div><span>连通块</span><strong>{stats.componentCount}</strong></div></Tooltip>
            <Tooltip title="最大连通块的人数"><div><span>最大圈子</span><strong>{stats.largestComponent} 人</strong></div></Tooltip>
            <Tooltip title="同类型双向记录的比例"><div><span>互相率</span><strong>{Math.round(stats.reciprocity * 100)}%</strong></div></Tooltip>
            {!ctx.readOnly && <Tooltip title="群里还没有任何关系记录的成员"><div><span>未连接成员</span><strong>{stats.isolatedCount}</strong></div></Tooltip>}
          </div>
        </Panel>
      </div>

      <div className="rel-insights-people">
        <Panel title="社交核心" extra={<Text type="secondary" className="rel-small">关系最多</Text>}>
          <PersonList ctx={ctx} items={top("total")} metric={(value) => `${value} 条`} />
        </Panel>
        <Panel title="桥梁人物" extra={<Tooltip title="介数中心性：最多最短路径经过 TA，往往是连接不同小圈子的人"><Text type="secondary" className="rel-small">连接圈子</Text></Tooltip>}>
          <PersonList ctx={ctx} items={top("bridge")} metric={(value) => value.toFixed(1)} />
        </Panel>
        <Panel title="最受关注" extra={<Text type="secondary" className="rel-small">被指向最多</Text>}>
          <PersonList ctx={ctx} items={top("incoming")} metric={(value) => `${value} 次`} />
        </Panel>
      </div>

      <Panel title={`小圈子 · ${communityList.length}`} extra={<Button size="small" onClick={() => ctx.setView("graph")}>在图谱中按圈子着色</Button>}>
        {communityList.length === 0 ? <Text type="secondary">还没有形成两人以上的圈子</Text> : (
          <div className="rel-community-grid">
            {communityList.slice(0, 8).map((community) => (
              <div key={community.index} className="rel-community" style={{ "--rel-community": communityColor(community.index) } as React.CSSProperties}>
                <div className="rel-community-head">
                  <span className="rel-community-swatch" />
                  <Text strong>圈子 {community.index + 1}</Text>
                  <Text type="secondary" className="rel-small">{community.members.length} 人 · {community.internal} 条内部关系{community.dominant ? ` · 多为${community.dominant}` : ""}</Text>
                </div>
                <div className="rel-community-members">
                  {community.members.slice(0, 10).map((userId) => (
                    <button key={userId} type="button" className="rel-community-member" onClick={() => ctx.openMember(userId)}>
                      <span className="rel-avatar is-xs" aria-hidden="true" data-initial={ctx.nameOf(userId).slice(0, 1)} />{ctx.nameOf(userId)}
                    </button>
                  ))}
                  {community.members.length > 10 && <Text type="secondary" className="rel-small">等 {community.members.length} 人</Text>}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="待复核"
        className="rel-queue-panel"
        extra={
          <Segmented
            size="small"
            value={queue}
            onChange={(value) => { setQueue(value as QueueKey); setChecked([]); }}
            options={[
              { value: "conflict", label: `矛盾 ${queues.conflict.length}` },
              { value: "oneSided", label: `单向亲密 ${queues.oneSided.length}` },
              { value: "stale", label: `长期未见 ${queues.stale.length}` },
              { value: "low", label: `低置信 ${queues.low.length}` },
            ]}
          />
        }
      >
        <Text type="secondary" className="rel-small rel-queue-hint">
          {queue === "conflict" && "同一对成员既有亲密关系又有“对立”，通常有一条是误判或已经过时。"}
          {queue === "oneSided" && "情侣/伴侣只记录了一个方向，可以一键补全反向关系。"}
          {queue === "stale" && "超过 60 天没有再被见到的关系，可能已经淡了。"}
          {queue === "low" && "置信度低于 40% 的关系，建议确认后提高置信度或删除。"}
        </Text>
        {queueEdges.length === 0 ? <Empty description="这一类没有需要处理的关系 =ω=" /> : (
          <>
            {!ctx.readOnly && (
              <div className="rel-queue-actions">
                <Checkbox
                  checked={visibleChecked.length > 0 && visibleChecked.length === queueEdges.length}
                  indeterminate={visibleChecked.length > 0 && visibleChecked.length < queueEdges.length}
                  disabled={ctx.locked}
                  onChange={(event) => setChecked(event.target.checked ? queueEdges.map((edge) => edge.id) : [])}
                >全选</Checkbox>
                <Space>
                  {queue === "oneSided" && (
                    <Button size="small" icon={<RetweetOutlined />} disabled={!writable || visibleChecked.length === 0}
                      onClick={() => { void ctx.createReverse(queueEdges.filter((edge) => visibleChecked.includes(edge.id))).then(() => setChecked([])); }}>
                      补全反向（{visibleChecked.length}）
                    </Button>
                  )}
                  <Popconfirm title={`删除选中的 ${visibleChecked.length} 条关系？`} okButtonProps={{ danger: true }} disabled={visibleChecked.length === 0}
                    onConfirm={async () => { if (await ctx.removeEdges(visibleChecked)) setChecked([]); }}>
                    <Button size="small" danger icon={<DeleteOutlined />} disabled={!writable || visibleChecked.length === 0}>删除（{visibleChecked.length}）</Button>
                  </Popconfirm>
                </Space>
              </div>
            )}
            <div className="rel-queue-list">
              {queue === "conflict"
                ? queues.conflict.slice(0, 50).map((conflict) => (
                    <div key={conflict.key} className="rel-conflict">
                      <Text strong className="rel-small">{ctx.nameOf(conflict.a)} ↔ {ctx.nameOf(conflict.b)} · {conflict.reason}</Text>
                      {conflict.edges.map(renderEdgeRow)}
                    </div>
                  ))
                : queueEdges.slice(0, 80).map(renderEdgeRow)}
              {queueEdges.length > 80 && <Text type="secondary" className="rel-small">仅显示前 80 条，可在列表视图按条件继续处理。</Text>}
            </div>
          </>
        )}
      </Panel>

      <div className="rel-insights-charts">
        <Panel title="类型画像">
          <div className="rel-type-table" role="table" aria-label="关系类型统计">
            <div className="rel-type-table-row is-head" role="row">
              <span role="columnheader">类型</span><span role="columnheader">数量</span><span role="columnheader">平均置信</span><span role="columnheader">互相率</span>
            </div>
            {types.map((item) => (
              <button key={item.type} type="button" role="row" className="rel-type-table-row" onClick={() => ctx.patchFilters({ type: filters.type === item.type ? "" : item.type })}>
                <span role="cell"><span className="rel-type-dot" style={{ background: relationTypeColor(item.type) }} />{item.type}</span>
                <span role="cell" className="rel-num">{item.count}</span>
                <span role="cell"><Meter value={item.avgConfidence} label={`${item.type} 平均置信`} color={relationTypeColor(item.type)} /></span>
                <span role="cell" className="rel-num">{Math.round(item.mutualRate * 100)}%</span>
              </button>
            ))}
          </div>
        </Panel>
        <Panel title="置信度分布">
          <MiniBars
            ariaLabel="置信度直方图"
            values={histogram}
            labels={histogram.map((_, index) => `${index * 10}–${index * 10 + 10}%`)}
            color={(index) => (index < 4 ? "var(--rel-bad)" : index < 7 ? "var(--rel-warn)" : "var(--rel-good)")}
            formatTip={(value, index) => `置信 ${index * 10}–${index * 10 + 10}%：${value} 条`}
          />
          <div className="rel-axis"><span>0%</span><span>50%</span><span>100%</span></div>
        </Panel>
        <Panel title="近 12 周活跃">
          <MiniBars
            ariaLabel="近 12 周被见到的关系数"
            values={weeks}
            labels={weeks.map((_, index) => (index === weeks.length - 1 ? "本周" : `${weeks.length - 1 - index} 周前`))}
            highlightLast
            formatTip={(value, index) => `${index === weeks.length - 1 ? "本周" : `${weeks.length - 1 - index} 周前`}：${value} 条`}
          />
          <div className="rel-axis"><span>12 周前</span><span>本周</span></div>
        </Panel>
        <Panel title="新鲜度">
          <DistributionBar
            ariaLabel="关系新鲜度分布"
            height={12}
            items={freshness.map((item) => ({ key: item.tier, label: FRESHNESS_LABEL[item.tier], count: item.count, color: `var(--rel-fresh-${item.tier})` }))}
          />
          <div className="rel-fresh-legend">
            {freshness.map((item) => (
              <span key={item.tier} className="rel-legend-item"><span className={`rel-fresh-dot is-${item.tier}`} />{FRESHNESS_LABEL[item.tier]} {item.count}</span>
            ))}
          </div>
          {!ctx.readOnly && sources.size > 0 && (
            <>
              <div className="rel-section-title rel-mt">来源</div>
              <DistributionBar
                ariaLabel="关系来源分布"
                items={[...sources.entries()].map(([source, count], index) => ({ key: source, label: RELATION_SOURCE_META[source]?.label ?? source, count, color: communityColor(index + 1) }))}
              />
              <div className="rel-fresh-legend">
                {[...sources.entries()].map(([source, count], index) => (
                  <span key={source} className="rel-legend-item"><span className="rel-type-dot" style={{ background: communityColor(index + 1) }} />{RELATION_SOURCE_META[source]?.label ?? source} {count}</span>
                ))}
              </div>
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
