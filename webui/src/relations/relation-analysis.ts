// 人物关系的纯分析函数：网络指标、社群、桥梁成员、路径、待复核队列与导出。
// 全部无副作用、输入只读，便于 vitest 直接断言；图谱/成员/矩阵/洞察视图共用。
import type { AgentRelationGraph, AgentRelationGraphNode, AgentRelationItem } from "../types";
import { nodeDisplayName } from "../relation-meta";

const DAY_MS = 86_400_000;

// 默认按“互相成立”理解的关系类型：新增时默认勾选“同时建立反向关系”。
export const SYMMETRIC_RELATION_TYPES = new Set(["好友", "死党", "情侣", "伴侣", "亲属", "同事", "同学", "搭子", "对立"]);
// 亲密类型与“对立”同时出现在同一对成员之间视为冲突。
export const POSITIVE_RELATION_TYPES = new Set(["好友", "死党", "情侣", "伴侣", "亲属", "搭子"]);
export const INTIMATE_RELATION_TYPES = new Set(["情侣", "伴侣"]);
export const LOW_CONFIDENCE = 0.4;
export const STALE_DAYS = 60;

export type ConfidenceTier = "high" | "mid" | "low";
export type FreshnessTier = "fresh" | "recent" | "aging" | "stale" | "unknown";

export function confidenceTier(confidence: number): ConfidenceTier {
  if (confidence >= 0.75) return "high";
  if (confidence >= LOW_CONFIDENCE) return "mid";
  return "low";
}

export const CONFIDENCE_TIER_LABEL: Record<ConfidenceTier, string> = {
  high: "确定",
  mid: "较可信",
  low: "存疑",
};

export function daysSince(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return null;
  return Math.max(0, (now - time) / DAY_MS);
}

export function freshnessTier(iso: string | null | undefined, now = Date.now()): FreshnessTier {
  const days = daysSince(iso, now);
  if (days === null) return "unknown";
  if (days < 7) return "fresh";
  if (days < 30) return "recent";
  if (days < STALE_DAYS) return "aging";
  return "stale";
}

export const FRESHNESS_LABEL: Record<FreshnessTier, string> = {
  fresh: "7 天内",
  recent: "30 天内",
  aging: "60 天内",
  stale: "超过 60 天",
  unknown: "未知",
};

// 中文相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前 / N 个月前 / N 年前。
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return "—";
  const diff = now - time;
  if (diff < 0) return "刚刚";
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "昨天";
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

// 一句话描述关系边，供详情、预览与无障碍标签使用。
export function relationSentence(subject: string, type: string, object: string): string {
  if (type === "mentions") return `${subject} 经常提到 ${object}`;
  if (type === "对立") return `${subject} 与 ${object} 不对付`;
  if (type === "师徒") return `${subject} 是 ${object} 的师父`;
  return `${subject} 把 ${object} 视为${type}`;
}

export function pairKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

function edgeKey(subject: string, object: string, type: string): string {
  return `${subject}→${object}#${type}`;
}

// 是否存在同类型反向边。
export function mutualEdgeIds(edges: AgentRelationItem[]): Set<string> {
  const keys = new Set(edges.map((edge) => edgeKey(edge.subjectUserId, edge.objectUserId, edge.type)));
  const result = new Set<string>();
  for (const edge of edges) {
    if (keys.has(edgeKey(edge.objectUserId, edge.subjectUserId, edge.type))) result.add(edge.id);
  }
  return result;
}

// 无向邻接表（多条边折叠为一个邻居，权重取最大置信度）。
export function buildAdjacency(edges: AgentRelationItem[]): Map<string, Map<string, number>> {
  const adjacency = new Map<string, Map<string, number>>();
  const link = (a: string, b: string, weight: number) => {
    let bucket = adjacency.get(a);
    if (!bucket) {
      bucket = new Map();
      adjacency.set(a, bucket);
    }
    bucket.set(b, Math.max(bucket.get(b) ?? 0, weight));
  };
  for (const edge of edges) {
    if (edge.subjectUserId === edge.objectUserId) continue;
    link(edge.subjectUserId, edge.objectUserId, edge.confidence);
    link(edge.objectUserId, edge.subjectUserId, edge.confidence);
  }
  return adjacency;
}

// 连通分量，按规模降序。
export function connectedComponents(nodeIds: string[], edges: AgentRelationItem[]): string[][] {
  const adjacency = buildAdjacency(edges);
  const seen = new Set<string>();
  const components: string[][] = [];
  for (const start of [...nodeIds].sort()) {
    if (seen.has(start)) continue;
    const component: string[] = [];
    const queue = [start];
    seen.add(start);
    while (queue.length) {
      const current = queue.shift() as string;
      component.push(current);
      for (const next of adjacency.get(current)?.keys() ?? []) {
        if (!seen.has(next)) {
          seen.add(next);
          queue.push(next);
        }
      }
    }
    components.push(component);
  }
  return components.sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]));
}

// 加权标签传播的确定性版本：固定遍历顺序、并列取最小标签，结果按社群规模重新编号。
export function detectCommunities(nodeIds: string[], edges: AgentRelationItem[], rounds = 12): Map<string, number> {
  const adjacency = buildAdjacency(edges);
  const ordered = [...nodeIds].sort();
  const labels = new Map(ordered.map((id) => [id, id]));
  for (let round = 0; round < rounds; round += 1) {
    let changed = false;
    for (const id of ordered) {
      const neighbors = adjacency.get(id);
      if (!neighbors || neighbors.size === 0) continue;
      const scores = new Map<string, number>();
      for (const [neighbor, weight] of neighbors) {
        const label = labels.get(neighbor) ?? neighbor;
        scores.set(label, (scores.get(label) ?? 0) + 0.3 + weight);
      }
      let best = labels.get(id) as string;
      let bestScore = scores.get(best) ?? -1;
      for (const [label, score] of [...scores.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
        if (score > bestScore + 1e-9) {
          best = label;
          bestScore = score;
        }
      }
      if (best !== labels.get(id)) {
        labels.set(id, best);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const groups = new Map<string, string[]>();
  for (const [id, label] of labels) {
    const bucket = groups.get(label);
    if (bucket) bucket.push(id);
    else groups.set(label, [id]);
  }
  const ranked = [...groups.values()].sort((a, b) => b.length - a.length || a[0].localeCompare(b[0]));
  const result = new Map<string, number>();
  ranked.forEach((members, index) => members.forEach((id) => result.set(id, index)));
  return result;
}

// 低饱和社群色，与单一樱粉强调色并存时不抢戏。
export const COMMUNITY_COLORS = ["#e4558a", "#5b8def", "#3fa58a", "#d68b3a", "#8f6fd1", "#3aa3b8", "#c75f5f", "#7d9a3d", "#b5739d", "#6b7a99"];

export function communityColor(index: number | undefined): string {
  if (index === undefined || index < 0) return "#b8b5c0";
  return COMMUNITY_COLORS[index % COMMUNITY_COLORS.length];
}

// Brandes 介数中心性（无向、无权）；超大图直接返回空，避免阻塞主线程。
export function betweenness(nodeIds: string[], edges: AgentRelationItem[], maxNodes = 400): Map<string, number> {
  const result = new Map<string, number>(nodeIds.map((id) => [id, 0]));
  if (nodeIds.length > maxNodes || nodeIds.length < 3) return result;
  const adjacency = buildAdjacency(edges);
  for (const source of nodeIds) {
    const stack: string[] = [];
    const predecessors = new Map<string, string[]>();
    const sigma = new Map<string, number>([[source, 1]]);
    const distance = new Map<string, number>([[source, 0]]);
    const queue = [source];
    while (queue.length) {
      const v = queue.shift() as string;
      stack.push(v);
      for (const w of adjacency.get(v)?.keys() ?? []) {
        if (!distance.has(w)) {
          distance.set(w, (distance.get(v) as number) + 1);
          queue.push(w);
        }
        if (distance.get(w) === (distance.get(v) as number) + 1) {
          sigma.set(w, (sigma.get(w) ?? 0) + (sigma.get(v) ?? 0));
          const list = predecessors.get(w);
          if (list) list.push(v);
          else predecessors.set(w, [v]);
        }
      }
    }
    const delta = new Map<string, number>();
    while (stack.length) {
      const w = stack.pop() as string;
      for (const v of predecessors.get(w) ?? []) {
        const share = ((sigma.get(v) ?? 0) / (sigma.get(w) || 1)) * (1 + (delta.get(w) ?? 0));
        delta.set(v, (delta.get(v) ?? 0) + share);
      }
      if (w !== source) result.set(w, (result.get(w) ?? 0) + (delta.get(w) ?? 0));
    }
  }
  // 无向图每条最短路被两端各算一次。
  for (const [id, value] of result) result.set(id, value / 2);
  return result;
}

// 无向最短路径（BFS），返回途经成员；不可达返回 null。
export function shortestPath(edges: AgentRelationItem[], from: string, to: string): string[] | null {
  if (from === to) return [from];
  const adjacency = buildAdjacency(edges);
  const previous = new Map<string, string>();
  const queue = [from];
  const seen = new Set([from]);
  while (queue.length) {
    const current = queue.shift() as string;
    for (const next of [...(adjacency.get(current)?.keys() ?? [])].sort()) {
      if (seen.has(next)) continue;
      seen.add(next);
      previous.set(next, current);
      if (next === to) {
        const path = [to];
        let cursor = to;
        while (previous.has(cursor)) {
          cursor = previous.get(cursor) as string;
          path.unshift(cursor);
        }
        return path;
      }
      queue.push(next);
    }
  }
  return null;
}

// 路径上相邻成员之间的边（任一方向都算）。
export function pathEdgeIds(edges: AgentRelationItem[], path: string[] | null): Set<string> {
  const result = new Set<string>();
  if (!path || path.length < 2) return result;
  const pairs = new Set<string>();
  for (let index = 1; index < path.length; index += 1) pairs.add(pairKey(path[index - 1], path[index]));
  for (const edge of edges) if (pairs.has(pairKey(edge.subjectUserId, edge.objectUserId))) result.add(edge.id);
  return result;
}

// 以 center 为中心的 k 跳邻域。
export function egoNetwork(edges: AgentRelationItem[], center: string, hops: number): Set<string> {
  const adjacency = buildAdjacency(edges);
  const result = new Set([center]);
  let frontier = [center];
  for (let hop = 0; hop < hops; hop += 1) {
    const next: string[] = [];
    for (const id of frontier) {
      for (const neighbor of adjacency.get(id)?.keys() ?? []) {
        if (!result.has(neighbor)) {
          result.add(neighbor);
          next.push(neighbor);
        }
      }
    }
    frontier = next;
  }
  return result;
}

export interface NetworkStats {
  memberCount: number;
  linkedCount: number;
  isolatedCount: number;
  edgeCount: number;
  pairCount: number;
  density: number;
  reciprocity: number;
  mutualEdgeCount: number;
  avgConfidence: number;
  avgDegree: number;
  componentCount: number;
  largestComponent: number;
  freshCount: number;
  staleCount: number;
  lowConfidenceCount: number;
}

export function networkStats(graph: AgentRelationGraph, now = Date.now()): NetworkStats {
  const edges = graph.edges;
  const linked = new Set<string>();
  const pairs = new Set<string>();
  let confidenceSum = 0;
  let fresh = 0;
  let stale = 0;
  let low = 0;
  for (const edge of edges) {
    linked.add(edge.subjectUserId);
    linked.add(edge.objectUserId);
    pairs.add(pairKey(edge.subjectUserId, edge.objectUserId));
    confidenceSum += edge.confidence;
    const tier = freshnessTier(edge.lastSeenAt, now);
    if (tier === "fresh") fresh += 1;
    if (tier === "stale") stale += 1;
    if (edge.confidence < LOW_CONFIDENCE) low += 1;
  }
  const n = linked.size;
  const mutual = mutualEdgeIds(edges).size;
  const components = connectedComponents([...linked], edges);
  return {
    memberCount: graph.nodes.length,
    linkedCount: n,
    isolatedCount: graph.nodes.filter((node) => !linked.has(node.userId)).length,
    edgeCount: edges.length,
    pairCount: pairs.size,
    density: n > 1 ? pairs.size / ((n * (n - 1)) / 2) : 0,
    reciprocity: edges.length ? mutual / edges.length : 0,
    mutualEdgeCount: mutual,
    avgConfidence: edges.length ? confidenceSum / edges.length : 0,
    avgDegree: n ? (pairs.size * 2) / n : 0,
    componentCount: components.length,
    largestComponent: components[0]?.length ?? 0,
    freshCount: fresh,
    staleCount: stale,
    lowConfidenceCount: low,
  };
}

export interface HealthFactor {
  key: string;
  label: string;
  score: number;
  hint: string;
}

// 关系数据健康分（0–100）：可信度、新鲜度、互相性与覆盖面加权，给出可执行的扣分原因。
export function healthScore(stats: NetworkStats): { score: number; factors: HealthFactor[] } {
  if (stats.edgeCount === 0) return { score: 0, factors: [] };
  const confidence = Math.min(1, stats.avgConfidence / 0.8);
  const freshness = 1 - stats.staleCount / stats.edgeCount;
  const reciprocity = Math.min(1, stats.reciprocity / 0.5);
  const coverage = stats.memberCount ? Math.min(1, stats.linkedCount / Math.max(stats.memberCount, 1) / 0.5) : 1;
  const factors: HealthFactor[] = [
    { key: "confidence", label: "可信度", score: confidence, hint: `平均置信 ${(stats.avgConfidence * 100).toFixed(0)}%，${stats.lowConfidenceCount} 条存疑` },
    { key: "freshness", label: "新鲜度", score: freshness, hint: `${stats.staleCount} 条超过 ${STALE_DAYS} 天未再见到` },
    { key: "reciprocity", label: "互相性", score: reciprocity, hint: `${(stats.reciprocity * 100).toFixed(0)}% 的边有同类型反向边` },
    { key: "coverage", label: "覆盖面", score: coverage, hint: `${stats.linkedCount}/${stats.memberCount || stats.linkedCount} 名成员有关系记录` },
  ];
  const weights: Record<string, number> = { confidence: 0.35, freshness: 0.3, reciprocity: 0.2, coverage: 0.15 };
  const score = factors.reduce((sum, factor) => sum + factor.score * weights[factor.key], 0);
  return { score: Math.round(score * 100), factors };
}

export interface MemberRelationProfile {
  userId: string;
  name: string;
  role: string;
  node: AgentRelationGraphNode | undefined;
  outgoing: number;
  incoming: number;
  total: number;
  neighbors: number;
  mutual: number;
  avgConfidence: number;
  types: Array<{ type: string; count: number }>;
  strongest: { edge: AgentRelationItem; otherId: string } | null;
  lastSeenAt: string | null;
  community: number | undefined;
  bridge: number;
}

export function memberProfiles(
  graph: AgentRelationGraph,
  communities: Map<string, number> = new Map(),
  bridges: Map<string, number> = new Map(),
): MemberRelationProfile[] {
  const nodeById = new Map(graph.nodes.map((node) => [node.userId, node]));
  const mutual = mutualEdgeIds(graph.edges);
  const byMember = new Map<string, AgentRelationItem[]>();
  for (const edge of graph.edges) {
    for (const id of new Set([edge.subjectUserId, edge.objectUserId])) {
      const bucket = byMember.get(id);
      if (bucket) bucket.push(edge);
      else byMember.set(id, [edge]);
    }
  }
  const ids = new Set([...graph.nodes.map((node) => node.userId), ...byMember.keys()]);
  return [...ids].map((userId) => {
    const node = nodeById.get(userId);
    const list = byMember.get(userId) ?? [];
    const typeCounts = new Map<string, number>();
    const neighborIds = new Set<string>();
    let strongest: MemberRelationProfile["strongest"] = null;
    let confidenceSum = 0;
    let lastSeenAt: string | null = null;
    for (const edge of list) {
      typeCounts.set(edge.type, (typeCounts.get(edge.type) ?? 0) + 1);
      const otherId = edge.subjectUserId === userId ? edge.objectUserId : edge.subjectUserId;
      neighborIds.add(otherId);
      confidenceSum += edge.confidence;
      if (!strongest || edge.confidence > strongest.edge.confidence) strongest = { edge, otherId };
      if (edge.lastSeenAt && (!lastSeenAt || edge.lastSeenAt > lastSeenAt)) lastSeenAt = edge.lastSeenAt;
    }
    return {
      userId,
      name: nodeDisplayName(node, userId),
      role: node?.role ?? "member",
      node,
      outgoing: list.filter((edge) => edge.subjectUserId === userId).length,
      incoming: list.filter((edge) => edge.objectUserId === userId).length,
      total: list.length,
      neighbors: neighborIds.size,
      mutual: list.filter((edge) => mutual.has(edge.id) && edge.subjectUserId === userId).length,
      avgConfidence: list.length ? confidenceSum / list.length : 0,
      types: [...typeCounts.entries()].map(([type, count]) => ({ type, count })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type, "zh-Hans-CN")),
      strongest,
      lastSeenAt,
      community: communities.get(userId),
      bridge: bridges.get(userId) ?? 0,
    };
  });
}

export type MemberSort = "degree" | "incoming" | "confidence" | "recent" | "bridge" | "name";

export function sortMemberProfiles(profiles: MemberRelationProfile[], sort: MemberSort): MemberRelationProfile[] {
  const by: Record<MemberSort, (a: MemberRelationProfile, b: MemberRelationProfile) => number> = {
    degree: (a, b) => b.total - a.total,
    incoming: (a, b) => b.incoming - a.incoming,
    confidence: (a, b) => b.avgConfidence - a.avgConfidence,
    recent: (a, b) => (b.lastSeenAt ?? "").localeCompare(a.lastSeenAt ?? ""),
    bridge: (a, b) => b.bridge - a.bridge,
    name: (a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"),
  };
  return [...profiles].sort((a, b) => by[sort](a, b) || a.userId.localeCompare(b.userId));
}

export interface RelationConflict {
  key: string;
  a: string;
  b: string;
  edges: AgentRelationItem[];
  reason: string;
}

// 同一对成员既有亲密关系又有“对立”。
export function detectConflicts(edges: AgentRelationItem[]): RelationConflict[] {
  const byPair = new Map<string, AgentRelationItem[]>();
  for (const edge of edges) {
    const key = pairKey(edge.subjectUserId, edge.objectUserId);
    const bucket = byPair.get(key);
    if (bucket) bucket.push(edge);
    else byPair.set(key, [edge]);
  }
  const result: RelationConflict[] = [];
  for (const [key, list] of byPair) {
    const positive = list.filter((edge) => POSITIVE_RELATION_TYPES.has(edge.type));
    const hostile = list.filter((edge) => edge.type === "对立");
    if (positive.length && hostile.length) {
      const [a, b] = key.split("|");
      result.push({ key, a, b, edges: [...positive, ...hostile], reason: `${[...new Set(positive.map((edge) => edge.type))].join("、")} 与 对立 并存` });
    }
  }
  return result.sort((x, y) => x.key.localeCompare(y.key));
}

// 对称类型却只有单向记录的边：提示补全反向边。
export function oneSidedEdges(edges: AgentRelationItem[], types: Set<string> = INTIMATE_RELATION_TYPES): AgentRelationItem[] {
  const mutual = mutualEdgeIds(edges);
  return edges.filter((edge) => types.has(edge.type) && !mutual.has(edge.id));
}

export function staleEdges(edges: AgentRelationItem[], now = Date.now(), days = STALE_DAYS): AgentRelationItem[] {
  return edges
    .filter((edge) => (daysSince(edge.lastSeenAt, now) ?? 0) >= days)
    .sort((a, b) => (a.lastSeenAt ?? "").localeCompare(b.lastSeenAt ?? ""));
}

export function lowConfidenceEdges(edges: AgentRelationItem[], threshold = LOW_CONFIDENCE): AgentRelationItem[] {
  return edges.filter((edge) => edge.confidence < threshold).sort((a, b) => a.confidence - b.confidence);
}

export function confidenceHistogram(edges: AgentRelationItem[], bins = 10): number[] {
  const result = new Array<number>(bins).fill(0);
  for (const edge of edges) {
    const clamped = Math.min(Math.max(edge.confidence, 0), 1);
    result[Math.min(bins - 1, Math.floor(clamped * bins))] += 1;
  }
  return result;
}

export function freshnessBuckets(edges: AgentRelationItem[], now = Date.now()): Array<{ tier: FreshnessTier; count: number }> {
  const order: FreshnessTier[] = ["fresh", "recent", "aging", "stale", "unknown"];
  const counts = new Map<FreshnessTier, number>(order.map((tier) => [tier, 0]));
  for (const edge of edges) {
    const tier = freshnessTier(edge.lastSeenAt, now);
    counts.set(tier, (counts.get(tier) ?? 0) + 1);
  }
  return order.map((tier) => ({ tier, count: counts.get(tier) ?? 0 })).filter((item) => item.tier !== "unknown" || item.count > 0);
}

// 最近 weeks 周每周有多少条关系被（再次）见到，最后一格是本周。
export function weeklyActivity(edges: AgentRelationItem[], now = Date.now(), weeks = 12): number[] {
  const result = new Array<number>(weeks).fill(0);
  for (const edge of edges) {
    const days = daysSince(edge.lastSeenAt, now);
    if (days === null) continue;
    const index = weeks - 1 - Math.floor(days / 7);
    if (index >= 0) result[index] += 1;
  }
  return result;
}

export interface TypeStat {
  type: string;
  count: number;
  avgConfidence: number;
  mutualRate: number;
}

export function typeStats(edges: AgentRelationItem[]): TypeStat[] {
  const mutual = mutualEdgeIds(edges);
  const map = new Map<string, { count: number; confidence: number; mutual: number }>();
  for (const edge of edges) {
    const entry = map.get(edge.type) ?? { count: 0, confidence: 0, mutual: 0 };
    entry.count += 1;
    entry.confidence += edge.confidence;
    if (mutual.has(edge.id)) entry.mutual += 1;
    map.set(edge.type, entry);
  }
  return [...map.entries()]
    .map(([type, entry]) => ({ type, count: entry.count, avgConfidence: entry.confidence / entry.count, mutualRate: entry.mutual / entry.count }))
    .sort((a, b) => b.count - a.count || a.type.localeCompare(b.type, "zh-Hans-CN"));
}

// 矩阵视图成员顺序：取关系最多的 limit 人，同社群相邻、社群内按度数降序。
export function matrixMembers(
  profiles: MemberRelationProfile[],
  limit: number,
): MemberRelationProfile[] {
  const top = [...profiles].filter((profile) => profile.total > 0).sort((a, b) => b.total - a.total || a.userId.localeCompare(b.userId)).slice(0, limit);
  return top.sort((a, b) => (a.community ?? 999) - (b.community ?? 999) || b.total - a.total || a.userId.localeCompare(b.userId));
}

export interface RelationFilter {
  types?: Set<string>;
  minConfidence?: number;
  maxConfidence?: number;
  source?: string;
  member?: string;
  search?: string;
  freshness?: FreshnessTier | "";
}

// 前端侧过滤（图谱/成员/矩阵/洞察/导出共用，口径与列表接口一致）。
export function filterEdges(
  edges: AgentRelationItem[],
  filter: RelationFilter,
  nameOf: (userId: string) => string = (userId) => userId,
  now = Date.now(),
): AgentRelationItem[] {
  const keyword = filter.search?.trim().toLowerCase() ?? "";
  return edges.filter((edge) => {
    if (filter.types && filter.types.size > 0 && !filter.types.has(edge.type)) return false;
    if (filter.minConfidence !== undefined && edge.confidence < filter.minConfidence) return false;
    if (filter.maxConfidence !== undefined && edge.confidence > filter.maxConfidence) return false;
    if (filter.source && edge.sourceKind !== filter.source) return false;
    if (filter.member && edge.subjectUserId !== filter.member && edge.objectUserId !== filter.member) return false;
    if (filter.freshness && freshnessTier(edge.lastSeenAt, now) !== filter.freshness) return false;
    if (keyword) {
      const haystack = [edge.subjectUserId, edge.objectUserId, nameOf(edge.subjectUserId), nameOf(edge.objectUserId), edge.note]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(keyword)) return false;
    }
    return true;
  });
}

function csvCell(value: unknown): string {
  const text = String(value ?? "");
  // 以公式字符开头的单元格加前导单引号，避免表格软件执行注入公式。
  const safe = /^[=+\-@\t\r]/.test(text) ? `'${text}` : text;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export function relationsToCsv(
  edges: AgentRelationItem[],
  nameOf: (userId: string) => string,
  includeAdminFields: boolean,
): string {
  const header = ["主体QQ", "主体", "类型", "客体QQ", "客体", "置信度", "备注", "最后见到"];
  if (includeAdminFields) header.push("来源", "证据数");
  const rows = edges.map((edge) => {
    const row: unknown[] = [edge.subjectUserId, nameOf(edge.subjectUserId), edge.type, edge.objectUserId, nameOf(edge.objectUserId), edge.confidence.toFixed(2), edge.note, edge.lastSeenAt ?? ""];
    if (includeAdminFields) row.push(edge.sourceKind, edge.evidenceCount);
    return row.map(csvCell).join(",");
  });
  // BOM 让 Excel 正确识别 UTF-8 中文。
  return `﻿${[header.join(","), ...rows].join("\r\n")}\r\n`;
}

export function downloadText(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
