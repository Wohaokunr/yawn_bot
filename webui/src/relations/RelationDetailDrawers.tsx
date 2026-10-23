// 关系边详情与成员关系档案两个抽屉；所有视图点击边/成员都汇到这里，行为一致。
import {
  AimOutlined, DeleteOutlined, EditOutlined, FilterOutlined, PlusOutlined, RetweetOutlined,
} from "@ant-design/icons";
import { Button, Descriptions, Drawer, Empty, Popconfirm, Segmented, Space, Spin, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { RELATION_SOURCE_META } from "../agent-meta";
import { relationTypeColor } from "../relation-meta";
import { formatTime } from "../shared";
import type { AgentRelationItem, AgentRelationListItem } from "../types";
import { Meter } from "./RelationCharts";
import { roleTag } from "./MemberPicker";
import {
  CONFIDENCE_TIER_LABEL, confidenceTier, freshnessTier, FRESHNESS_LABEL, memberProfiles,
  mutualEdgeIds, relationSentence, relativeTime,
} from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text, Paragraph } = Typography;

export function ConfidencePill({ value }: { value: number }): React.JSX.Element {
  const tier = confidenceTier(value);
  return (
    <Tooltip title={`置信度 ${value.toFixed(2)} · ${CONFIDENCE_TIER_LABEL[tier]}`}>
      <span className={`rel-conf is-${tier}`}>
        <span className="rel-conf-track"><span className="rel-conf-fill" style={{ width: `${Math.round(value * 100)}%` }} /></span>
        <span className="rel-conf-num">{Math.round(value * 100)}%</span>
      </span>
    </Tooltip>
  );
}

export function FreshnessText({ iso }: { iso: string | null }): React.JSX.Element {
  const tier = freshnessTier(iso);
  return (
    <Tooltip title={iso ? `${formatTime(iso)} · ${FRESHNESS_LABEL[tier]}` : "没有记录时间"}>
      <span className={`rel-fresh is-${tier}`}><span className="rel-fresh-dot" aria-hidden="true" />{relativeTime(iso)}</span>
    </Tooltip>
  );
}

export function MemberChip({ userId, name, onClick }: { userId: string; name: string; onClick?: () => void }): React.JSX.Element {
  const content = <>
    <span className="rel-avatar is-xs" aria-hidden="true" data-initial={name.slice(0, 1)} />
    <span className="rel-member-chip-name">{name}</span>
  </>;
  return onClick
    ? <button type="button" className="rel-member-chip" onClick={onClick} title={`${name}（${userId}）`}>{content}</button>
    : <span className="rel-member-chip" title={`${name}（${userId}）`}>{content}</span>;
}

export function EdgeDetailDrawer({
  ctx,
  edge,
  onClose,
}: {
  ctx: RelationsContext;
  edge: AgentRelationListItem | null;
  onClose: () => void;
}): React.JSX.Element {
  const graphEdges = ctx.graph.data?.edges;
  // 同一对成员之间的其它关系边（任意方向），用于判断是否互相、是否存在冲突。
  const siblings = useMemo(() => {
    if (!edge || !graphEdges) return null;
    return graphEdges.filter((item) => item.id !== edge.id
      && ((item.subjectUserId === edge.subjectUserId && item.objectUserId === edge.objectUserId)
        || (item.subjectUserId === edge.objectUserId && item.objectUserId === edge.subjectUserId)));
  }, [edge, graphEdges]);
  const reverse = siblings?.find((item) => item.subjectUserId === edge?.objectUserId && item.type === edge?.type) ?? null;
  const mutual = siblings ? !!reverse : !!edge?.mutual;
  const subjectName = edge ? ctx.nameOf(edge.subjectUserId) : "";
  const objectName = edge ? ctx.nameOf(edge.objectUserId) : "";
  const writable = !ctx.readOnly && !ctx.locked;

  return (
    <Drawer open={!!edge} width={460} title="关系边详情" onClose={onClose} className="rel-edge-drawer">
      {edge && (
        <div className="rel-drawer-stack">
          <div className="rel-edge-hero">
            <MemberChip userId={edge.subjectUserId} name={subjectName} onClick={() => ctx.openMember(edge.subjectUserId)} />
            <div className="rel-edge-hero-link" style={{ "--rel-type-color": relationTypeColor(edge.type) } as React.CSSProperties}>
              <span className="rel-edge-hero-type">{edge.type}</span>
              <span className="rel-edge-hero-line" aria-hidden="true">{mutual ? "⇄" : "→"}</span>
            </div>
            <MemberChip userId={edge.objectUserId} name={objectName} onClick={() => ctx.openMember(edge.objectUserId)} />
          </div>
          <Paragraph className="rel-sentence">{relationSentence(subjectName, edge.type, objectName)}{mutual ? "，而且是互相的。" : "。"}</Paragraph>
          <Descriptions column={1} size="small" bordered className="rel-desc">
            <Descriptions.Item label="置信度"><ConfidencePill value={edge.confidence} /></Descriptions.Item>
            <Descriptions.Item label="最后见到"><FreshnessText iso={edge.lastSeenAt} /></Descriptions.Item>
            {!ctx.readOnly && <Descriptions.Item label="来源"><Tag color={RELATION_SOURCE_META[edge.sourceKind]?.color}>{RELATION_SOURCE_META[edge.sourceKind]?.label ?? edge.sourceKind}</Tag></Descriptions.Item>}
            {!ctx.readOnly && <Descriptions.Item label="证据数">{edge.evidenceCount} 次</Descriptions.Item>}
            <Descriptions.Item label="备注">{edge.note || <Text type="secondary">无备注</Text>}</Descriptions.Item>
            <Descriptions.Item label="QQ"><Text copyable className="rel-mono">{edge.subjectUserId}</Text> → <Text copyable className="rel-mono">{edge.objectUserId}</Text></Descriptions.Item>
          </Descriptions>
          {siblings && siblings.length > 0 && (
            <section>
              <div className="rel-section-title">这两人之间的其它关系</div>
              <div className="rel-mini-list">
                {siblings.map((item) => (
                  <button key={item.id} type="button" className="rel-mini-row" onClick={() => ctx.openEdge(item)}>
                    <Tag color={relationTypeColor(item.type)}>{item.type}</Tag>
                    <Text>{ctx.nameOf(item.subjectUserId)} → {ctx.nameOf(item.objectUserId)}</Text>
                    <span className="rel-grow" />
                    <ConfidencePill value={item.confidence} />
                  </button>
                ))}
              </div>
            </section>
          )}
          <Space wrap>
            <Button icon={<AimOutlined />} onClick={() => ctx.focusInGraph(edge.subjectUserId)}>在图谱中定位</Button>
            {writable && <Button icon={<EditOutlined />} type="primary" onClick={() => ctx.openEdit(edge)}>编辑</Button>}
            {writable && !mutual && (
              <Tooltip title={`新建 ${objectName} → ${subjectName} 的同类型关系`}>
                <Button icon={<RetweetOutlined />} onClick={() => { void ctx.createReverse([edge]); }}>补全反向</Button>
              </Tooltip>
            )}
            {writable && (
              <Popconfirm title="删除这条关系边？" okButtonProps={{ danger: true }} onConfirm={async () => { if (await ctx.removeEdges([edge.id])) onClose(); }}>
                <Button danger icon={<DeleteOutlined />}>删除</Button>
              </Popconfirm>
            )}
          </Space>
        </div>
      )}
    </Drawer>
  );
}

type MemberEdgeFilter = "all" | "out" | "in";

export function MemberDrawer({
  ctx,
  userId,
  onClose,
}: {
  ctx: RelationsContext;
  userId: string | null;
  onClose: () => void;
}): React.JSX.Element {
  const graph = ctx.graph.data;
  const [direction, setDirection] = useState<MemberEdgeFilter>("all");
  const profile = useMemo(() => {
    if (!userId || !graph) return null;
    return memberProfiles(graph).find((item) => item.userId === userId) ?? null;
  }, [graph, userId]);
  const mutual = useMemo(() => (graph ? mutualEdgeIds(graph.edges) : new Set<string>()), [graph]);
  const edges = useMemo(() => {
    if (!userId || !graph) return [];
    return graph.edges
      .filter((edge) => (direction !== "in" && edge.subjectUserId === userId) || (direction !== "out" && edge.objectUserId === userId))
      .sort((a, b) => b.confidence - a.confidence);
  }, [graph, userId, direction]);
  // 按类型分组展示，组内按置信度降序。
  const grouped = useMemo(() => {
    const map = new Map<string, AgentRelationItem[]>();
    for (const edge of edges) {
      const bucket = map.get(edge.type);
      if (bucket) bucket.push(edge);
      else map.set(edge.type, [edge]);
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [edges]);
  const name = userId ? ctx.nameOf(userId) : "";

  return (
    <Drawer open={!!userId} width={480} title={userId ? `${name} · 关系档案` : ""} onClose={onClose} className="rel-member-drawer">
      {userId && !graph && <div className="rel-center"><Spin /></div>}
      {userId && graph && (
        <div className="rel-drawer-stack">
          <div className="rel-member-hero">
            <span className="rel-avatar is-lg" aria-hidden="true" data-initial={name.slice(0, 1)} />
            <div className="rel-member-hero-text">
              <Space size={6} wrap><Text strong className="rel-member-hero-name">{name}</Text>{roleTag(profile?.role)}</Space>
              <Text type="secondary" copyable className="rel-mono">{userId}</Text>
            </div>
          </div>
          {profile && profile.total > 0 ? (
            <>
              <div className="rel-stat-grid">
                <div><span>关系</span><strong>{profile.total}</strong></div>
                <div><span>认识的人</span><strong>{profile.neighbors}</strong></div>
                <div><span>主动 / 被动</span><strong>{profile.outgoing} / {profile.incoming}</strong></div>
                <div><span>互相成立</span><strong>{profile.mutual}</strong></div>
              </div>
              <div>
                <div className="rel-section-title">平均置信度 {Math.round(profile.avgConfidence * 100)}%</div>
                <Meter value={profile.avgConfidence} label="平均置信度" />
              </div>
              {profile.strongest && (
                <Text type="secondary" className="rel-small">
                  最铁的关系：{ctx.nameOf(profile.strongest.otherId)}（{profile.strongest.edge.type} · {Math.round(profile.strongest.edge.confidence * 100)}%）· 最近一次 {relativeTime(profile.lastSeenAt)}
                </Text>
              )}
              <Space wrap>
                <Button size="small" icon={<AimOutlined />} onClick={() => ctx.focusInGraph(userId)}>图谱中聚焦</Button>
                <Button size="small" icon={<FilterOutlined />} onClick={() => { ctx.patchFilters({ member: userId }); ctx.setView("table"); onClose(); }}>列表只看 TA</Button>
                {!ctx.readOnly && <Button size="small" icon={<PlusOutlined />} disabled={ctx.locked} onClick={() => ctx.openCreate({ subjectUserId: userId })}>新增与 TA 的关系</Button>}
              </Space>
              <div className="rel-member-edges-head">
                <div className="rel-section-title">关系明细</div>
                <Segmented
                  size="small"
                  value={direction}
                  onChange={(value) => setDirection(value as MemberEdgeFilter)}
                  options={[
                    { value: "all", label: `全部 ${profile.total}` },
                    { value: "out", label: `TA → 别人 ${profile.outgoing}` },
                    { value: "in", label: `别人 → TA ${profile.incoming}` },
                  ]}
                />
              </div>
              {grouped.length === 0 ? <Empty description="这个方向暂无关系" /> : grouped.map(([type, list]) => (
                <section key={type} className="rel-member-type-group">
                  <div className="rel-member-type-head">
                    <span className="rel-type-dot" style={{ background: relationTypeColor(type) }} />
                    <Text strong>{type}</Text>
                    <Text type="secondary">{list.length}</Text>
                  </div>
                  <div className="rel-mini-list">
                    {list.map((edge) => {
                      const outgoing = edge.subjectUserId === userId;
                      const otherId = outgoing ? edge.objectUserId : edge.subjectUserId;
                      return (
                        <button key={edge.id} type="button" className="rel-mini-row" onClick={() => ctx.openEdge(edge)}>
                          <span className="rel-dir" title={mutual.has(edge.id) ? "互相" : outgoing ? "TA → 对方" : "对方 → TA"}>{mutual.has(edge.id) ? "⇄" : outgoing ? "→" : "←"}</span>
                          <span className="rel-avatar is-xs" aria-hidden="true" data-initial={ctx.nameOf(otherId).slice(0, 1)} />
                          <Text ellipsis className="rel-mini-name">{ctx.nameOf(otherId)}</Text>
                          {edge.note && <Text type="secondary" ellipsis className="rel-mini-note">{edge.note}</Text>}
                          <span className="rel-grow" />
                          <ConfidencePill value={edge.confidence} />
                        </button>
                      );
                    })}
                  </div>
                </section>
              ))}
            </>
          ) : (
            <Empty description="这位成员还没有关系记录">
              {!ctx.readOnly && <Button type="primary" disabled={ctx.locked} onClick={() => ctx.openCreate({ subjectUserId: userId })}>为 TA 新增关系</Button>}
            </Empty>
          )}
        </div>
      )}
    </Drawer>
  );
}
