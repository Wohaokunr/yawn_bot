// 成员视图：以人为中心的关系卡片墙，可搜索、排序、看社群归属与桥梁角色。
import { Button, Empty, Input, Segmented, Switch, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { relationTypeColor } from "../relation-meta";
import type { AgentRelationGraph } from "../types";
import { roleTag } from "./MemberPicker";
import {
  betweenness, communityColor, detectCommunities, filterEdges, memberProfiles, relativeTime,
  sortMemberProfiles, type MemberSort,
} from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;
const PAGE = 60;

export function RelationMembersView({ ctx, graph }: { ctx: RelationsContext; graph: AgentRelationGraph }): React.JSX.Element {
  const [keyword, setKeyword] = useState("");
  const [sort, setSort] = useState<MemberSort>("degree");
  const [showIsolated, setShowIsolated] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  const filters = ctx.filters;

  const filteredGraph = useMemo<AgentRelationGraph>(() => ({
    ...graph,
    edges: filterEdges(graph.edges, {
      types: filters.type ? new Set([filters.type]) : undefined,
      source: filters.source || undefined,
      minConfidence: filters.minConfidence ?? undefined,
      maxConfidence: filters.maxConfidence ?? undefined,
    }),
  }), [graph, filters.type, filters.source, filters.minConfidence, filters.maxConfidence]);

  const profiles = useMemo(() => {
    const ids = graph.nodes.map((node) => node.userId);
    const communities = detectCommunities(ids, filteredGraph.edges);
    const bridges = betweenness(ids, filteredGraph.edges);
    return memberProfiles(filteredGraph, communities, bridges);
  }, [graph.nodes, filteredGraph]);
  // 桥梁成员：介数排名前 10% 且大于 0。
  const bridgeThreshold = useMemo(() => {
    const values = profiles.map((profile) => profile.bridge).filter((value) => value > 0).sort((a, b) => b - a);
    return values[Math.max(0, Math.floor(values.length * 0.1) - 1)] ?? Infinity;
  }, [profiles]);

  const visible = useMemo(() => {
    const term = (keyword || filters.search).trim().toLowerCase();
    const list = profiles.filter((profile) => {
      if (!showIsolated && profile.total === 0) return false;
      if (filters.member && profile.userId !== filters.member) return false;
      if (term && !profile.name.toLowerCase().includes(term) && !profile.userId.includes(term)) return false;
      return true;
    });
    return sortMemberProfiles(list, sort);
  }, [profiles, keyword, filters.search, filters.member, showIsolated, sort]);
  const maxTotal = Math.max(1, ...visible.map((profile) => profile.total));

  return (
    <div className="rel-members-view">
      <div className="rel-subbar">
        <Input.Search
          allowClear
          placeholder="在成员里找人"
          className="rel-subbar-search"
          onChange={(event) => { setKeyword(event.target.value); setLimit(PAGE); }}
        />
        <Segmented
          size="small"
          value={sort}
          onChange={(value) => setSort(value as MemberSort)}
          options={[
            { value: "degree", label: "关系最多" },
            { value: "incoming", label: "被提到最多" },
            { value: "bridge", label: "桥梁" },
            { value: "confidence", label: "最可信" },
            { value: "recent", label: "最近活跃" },
            { value: "name", label: "名字" },
          ]}
        />
        {!ctx.readOnly && (
          <label className="rel-inline-switch">
            <Switch size="small" checked={showIsolated} onChange={setShowIsolated} />含无关系成员
          </label>
        )}
        <span className="rel-grow" />
        <Text type="secondary" className="rel-small">{visible.length} 人</Text>
      </div>
      {visible.length === 0 ? (
        <Empty description={keyword || filters.search ? "没有找到这位成员" : "暂无有关系记录的成员"} />
      ) : (
        <div className="rel-member-grid">
          {visible.slice(0, limit).map((profile) => {
            const color = communityColor(profile.community);
            const isBridge = profile.bridge >= bridgeThreshold && profile.bridge > 0;
            return (
              <button
                key={profile.userId}
                type="button"
                className="rel-member-card"
                style={{ "--rel-community": color } as React.CSSProperties}
                onClick={() => ctx.openMember(profile.userId)}
                aria-label={`${profile.name}，${profile.total} 条关系`}
              >
                <div className="rel-member-card-head">
                  <span className="rel-avatar" aria-hidden="true" data-initial={profile.name.slice(0, 1)} />
                  <div className="rel-member-card-title">
                    <span className="rel-member-card-name">{profile.name}</span>
                    <span className="rel-mono rel-member-card-id">{profile.userId}</span>
                  </div>
                  {roleTag(profile.role)}
                  {isBridge && <Tooltip title="连接多个小圈子的关键成员（介数中心性靠前）"><span className="rel-badge-bridge">桥梁</span></Tooltip>}
                </div>
                <div className="rel-member-card-bar" aria-hidden="true">
                  <span style={{ width: `${(profile.total / maxTotal) * 100}%` }} />
                </div>
                <div className="rel-member-card-stats">
                  <span><strong>{profile.total}</strong> 关系</span>
                  <span><strong>{profile.outgoing}</strong> 主动</span>
                  <span><strong>{profile.incoming}</strong> 被动</span>
                  <span><strong>{profile.mutual}</strong> 互相</span>
                </div>
                {profile.types.length > 0 && (
                  <div className="rel-member-card-types">
                    {profile.types.slice(0, 4).map((item) => (
                      <span key={item.type} className="rel-type-chip" style={{ "--rel-type-color": relationTypeColor(item.type) } as React.CSSProperties}>
                        {item.type} {item.count}
                      </span>
                    ))}
                    {profile.types.length > 4 && <span className="rel-type-chip is-more">+{profile.types.length - 4}</span>}
                  </div>
                )}
                <div className="rel-member-card-foot">
                  {profile.strongest
                    ? <span className="rel-ellipsis">最铁：{ctx.nameOf(profile.strongest.otherId)} · {Math.round(profile.strongest.edge.confidence * 100)}%</span>
                    : <span className="rel-ellipsis">还没有关系记录</span>}
                  <span className="rel-member-card-time">{relativeTime(profile.lastSeenAt)}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}
      {visible.length > limit && (
        <div className="rel-center"><Button onClick={() => setLimit((value) => value + PAGE)}>再显示 {Math.min(PAGE, visible.length - limit)} 人</Button></div>
      )}
    </div>
  );
}
