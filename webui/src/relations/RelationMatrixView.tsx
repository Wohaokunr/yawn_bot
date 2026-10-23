// 矩阵视图：关系最多的 N 位成员两两交叉，行=主体、列=客体；同社群相邻排列，
// 对角线附近的色块团就是小圈子。格子颜色=类型色，深浅=置信度，多类型时拆分成条纹。
import { Empty, Segmented, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { relationTypeColor } from "../relation-meta";
import type { AgentRelationGraph, AgentRelationItem } from "../types";
import {
  communityColor, detectCommunities, filterEdges, matrixMembers, memberProfiles,
} from "./relation-analysis";
import type { RelationsContext } from "./relations-context";

const { Text } = Typography;

export function RelationMatrixView({ ctx, graph }: { ctx: RelationsContext; graph: AgentRelationGraph }): React.JSX.Element {
  const [limit, setLimit] = useState(24);
  const [hover, setHover] = useState<{ row: number; col: number } | null>(null);
  const filters = ctx.filters;

  const edges = useMemo(() => filterEdges(graph.edges, {
    types: filters.type ? new Set([filters.type]) : undefined,
    source: filters.source || undefined,
    minConfidence: filters.minConfidence ?? undefined,
    maxConfidence: filters.maxConfidence ?? undefined,
  }), [graph.edges, filters.type, filters.source, filters.minConfidence, filters.maxConfidence]);

  const members = useMemo(() => {
    const communities = detectCommunities(graph.nodes.map((node) => node.userId), edges);
    return matrixMembers(memberProfiles({ ...graph, edges }, communities), limit);
  }, [graph, edges, limit]);

  const cells = useMemo(() => {
    const map = new Map<string, AgentRelationItem[]>();
    for (const edge of edges) {
      const key = `${edge.subjectUserId}>${edge.objectUserId}`;
      const bucket = map.get(key);
      if (bucket) bucket.push(edge);
      else map.set(key, [edge]);
    }
    return map;
  }, [edges]);

  const shownTypes = useMemo(() => {
    const ids = new Set(members.map((member) => member.userId));
    return [...new Set(edges.filter((edge) => ids.has(edge.subjectUserId) && ids.has(edge.objectUserId)).map((edge) => edge.type))]
      .sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [members, edges]);

  if (members.length < 2) return <Empty description="至少需要两位有关系的成员才能画矩阵" />;

  return (
    <div className="rel-matrix-view">
      <div className="rel-subbar">
        <Text type="secondary" className="rel-small">行 = 主体，列 = 客体；同色头像属于同一个小圈子。点格子看关系详情。</Text>
        <span className="rel-grow" />
        <Segmented
          size="small"
          value={limit}
          onChange={(value) => setLimit(Number(value))}
          options={[{ value: 16, label: "前 16" }, { value: 24, label: "前 24" }, { value: 40, label: "前 40" }]}
        />
      </div>
      <div className="rel-matrix-scroll" onPointerLeave={() => setHover(null)}>
        <table className="rel-matrix" style={{ "--rel-matrix-n": members.length } as React.CSSProperties}>
          <thead>
            <tr>
              <th className="rel-matrix-corner" scope="col"><span>主体 \ 客体</span></th>
              {members.map((member, col) => (
                <th key={member.userId} scope="col" className={hover?.col === col ? "is-hover" : undefined}>
                  <button type="button" className="rel-matrix-colhead" onClick={() => ctx.openMember(member.userId)} title={member.name}>
                    <span className="rel-matrix-dot" style={{ background: communityColor(member.community) }} />
                    <span className="rel-matrix-colname">{member.name}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {members.map((rowMember, row) => (
              <tr key={rowMember.userId}>
                <th scope="row" className={hover?.row === row ? "is-hover" : undefined}>
                  <button type="button" className="rel-matrix-rowhead" onClick={() => ctx.openMember(rowMember.userId)} title={rowMember.name}>
                    <span className="rel-matrix-dot" style={{ background: communityColor(rowMember.community) }} />
                    <span className="rel-ellipsis">{rowMember.name}</span>
                    <span className="rel-matrix-deg">{rowMember.total}</span>
                  </button>
                </th>
                {members.map((colMember, col) => {
                  const list = cells.get(`${rowMember.userId}>${colMember.userId}`) ?? [];
                  const crosshair = hover && (hover.row === row || hover.col === col);
                  if (row === col) return <td key={colMember.userId} className="rel-matrix-self" aria-hidden="true" />;
                  if (list.length === 0) {
                    return <td key={colMember.userId} className={crosshair ? "is-cross" : undefined} onPointerEnter={() => setHover({ row, col })} />;
                  }
                  const best = Math.max(...list.map((edge) => edge.confidence));
                  const background = list.length === 1
                    ? relationTypeColor(list[0].type)
                    : `linear-gradient(135deg, ${list.map((edge, index) => `${relationTypeColor(edge.type)} ${(index / list.length) * 100}% ${((index + 1) / list.length) * 100}%`).join(", ")})`;
                  return (
                    <td key={colMember.userId} className={`has-edge${crosshair ? " is-cross" : ""}`} onPointerEnter={() => setHover({ row, col })}>
                      <Tooltip
                        title={<div className="rel-matrix-tip">
                          {list.map((edge) => (
                            <div key={edge.id}>{rowMember.name} → {colMember.name}：{edge.type} · {Math.round(edge.confidence * 100)}%{edge.note ? ` · ${edge.note}` : ""}</div>
                          ))}
                        </div>}
                      >
                        <button
                          type="button"
                          className="rel-matrix-cell"
                          style={{ background, opacity: 0.35 + best * 0.65 }}
                          aria-label={`${rowMember.name} 对 ${colMember.name}：${list.map((edge) => edge.type).join("、")}`}
                          onClick={() => ctx.openEdge(list[0])}
                        />
                      </Tooltip>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="rel-matrix-legend">
        {shownTypes.map((type) => (
          <span key={type} className="rel-legend-item"><span className="rel-type-dot" style={{ background: relationTypeColor(type) }} />{type}</span>
        ))}
        <span className="rel-legend-item rel-matrix-legend-scale">
          浅<span className="rel-matrix-scale" aria-hidden="true" />深 = 置信度
        </span>
      </div>
    </div>
  );
}
