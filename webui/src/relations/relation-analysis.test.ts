import { describe, expect, it } from "vitest";
import type { AgentRelationGraph, AgentRelationGraphNode, AgentRelationItem } from "../types";
import {
  betweenness,
  confidenceHistogram,
  confidenceTier,
  connectedComponents,
  detectCommunities,
  detectConflicts,
  egoNetwork,
  filterEdges,
  freshnessBuckets,
  freshnessTier,
  healthScore,
  lowConfidenceEdges,
  matrixMembers,
  memberProfiles,
  mutualEdgeIds,
  networkStats,
  oneSidedEdges,
  pathEdgeIds,
  relationSentence,
  relationsToCsv,
  relativeTime,
  shortestPath,
  sortMemberProfiles,
  staleEdges,
  typeStats,
  weeklyActivity,
} from "./relation-analysis";

const NOW = Date.parse("2026-10-23T08:00:00+08:00");
const daysAgo = (days: number) => new Date(NOW - days * 86_400_000).toISOString();

let nextId = 1;
function edge(subject: string, object: string, type = "好友", extra: Partial<AgentRelationItem> = {}): AgentRelationItem {
  return {
    id: String(nextId++),
    groupId: "100",
    subjectUserId: subject,
    objectUserId: object,
    type,
    sourceKind: "auto",
    note: "",
    confidence: 0.8,
    evidenceCount: 1,
    lastSeenAt: daysAgo(1),
    ...extra,
  };
}

function node(userId: string, extra: Partial<AgentRelationGraphNode> = {}): AgentRelationGraphNode {
  return { userId, nickname: `成员${userId}`, groupNickname: null, role: "member", linked: true, degree: 0, ...extra };
}

function graph(nodes: AgentRelationGraphNode[], edges: AgentRelationItem[]): AgentRelationGraph {
  return { nodes, edges, meta: { relationTruncated: false, memberTruncated: false } };
}

// 两个三角形小圈子 a-b-c、x-y-z，由 c—x 一条桥连接；另有孤立成员 q。
const twoTriangles = [
  edge("a", "b"), edge("b", "c"), edge("c", "a"),
  edge("x", "y"), edge("y", "z"), edge("z", "x"),
  edge("c", "x", "同事", { confidence: 0.3 }),
];
const triangleGraph = graph(["a", "b", "c", "x", "y", "z", "q"].map((id) => node(id)), twoTriangles);

describe("格式化", () => {
  it("相对时间覆盖各个区间", () => {
    expect(relativeTime(null, NOW)).toBe("—");
    expect(relativeTime(new Date(NOW - 30_000).toISOString(), NOW)).toBe("刚刚");
    expect(relativeTime(new Date(NOW - 5 * 60_000).toISOString(), NOW)).toBe("5 分钟前");
    expect(relativeTime(new Date(NOW - 3 * 3_600_000).toISOString(), NOW)).toBe("3 小时前");
    expect(relativeTime(daysAgo(1), NOW)).toBe("昨天");
    expect(relativeTime(daysAgo(12), NOW)).toBe("12 天前");
    expect(relativeTime(daysAgo(65), NOW)).toBe("2 个月前");
    expect(relativeTime(daysAgo(800), NOW)).toBe("2 年前");
  });

  it("置信度与新鲜度分档", () => {
    expect(confidenceTier(0.9)).toBe("high");
    expect(confidenceTier(0.5)).toBe("mid");
    expect(confidenceTier(0.2)).toBe("low");
    expect(freshnessTier(daysAgo(2), NOW)).toBe("fresh");
    expect(freshnessTier(daysAgo(20), NOW)).toBe("recent");
    expect(freshnessTier(daysAgo(45), NOW)).toBe("aging");
    expect(freshnessTier(daysAgo(90), NOW)).toBe("stale");
    expect(freshnessTier(null, NOW)).toBe("unknown");
  });

  it("关系句式按类型变化", () => {
    expect(relationSentence("甲", "好友", "乙")).toBe("甲 把 乙 视为好友");
    expect(relationSentence("甲", "mentions", "乙")).toBe("甲 经常提到 乙");
    expect(relationSentence("甲", "对立", "乙")).toBe("甲 与 乙 不对付");
  });
});

describe("结构分析", () => {
  it("互相边：同类型反向才算", () => {
    const edges = [edge("a", "b"), edge("b", "a"), edge("a", "c"), edge("c", "a", "对立")];
    expect([...mutualEdgeIds(edges)].sort()).toEqual([edges[0].id, edges[1].id].sort());
  });

  it("连通分量按规模降序", () => {
    const components = connectedComponents(["a", "b", "c", "x", "y", "z", "q"], twoTriangles.slice(0, 6));
    expect(components.map((item) => item.length)).toEqual([3, 3, 1]);
  });

  it("社群识别把两个三角形分开，结果确定", () => {
    const ids = ["a", "b", "c", "x", "y", "z"];
    const first = detectCommunities(ids, twoTriangles);
    const second = detectCommunities(ids, twoTriangles);
    expect([...first.entries()]).toEqual([...second.entries()]);
    expect(first.get("a")).toBe(first.get("b"));
    expect(first.get("x")).toBe(first.get("y"));
    expect(first.get("a")).not.toBe(first.get("x"));
  });

  it("介数中心性：桥两端最高", () => {
    const scores = betweenness(["a", "b", "c", "x", "y", "z"], twoTriangles);
    const ranked = [...scores.entries()].sort((p, q) => q[1] - p[1]).map(([id]) => id);
    expect(ranked.slice(0, 2).sort()).toEqual(["c", "x"]);
    expect(scores.get("a")).toBe(0);
    expect(scores.get("c")).toBeCloseTo(6, 5);
  });

  it("最短路径与路径边", () => {
    const path = shortestPath(twoTriangles, "a", "z");
    expect(path).toEqual(["a", "c", "x", "z"]);
    expect(pathEdgeIds(twoTriangles, path).size).toBe(3);
    expect(shortestPath(twoTriangles, "a", "q")).toBeNull();
    expect(shortestPath(twoTriangles, "a", "a")).toEqual(["a"]);
  });

  it("k 跳邻域", () => {
    expect([...egoNetwork(twoTriangles, "a", 1)].sort()).toEqual(["a", "b", "c"]);
    expect([...egoNetwork(twoTriangles, "a", 2)].sort()).toEqual(["a", "b", "c", "x"]);
  });

  it("网络统计与健康分", () => {
    const stats = networkStats(triangleGraph, NOW);
    expect(stats.linkedCount).toBe(6);
    expect(stats.isolatedCount).toBe(1);
    expect(stats.pairCount).toBe(7);
    expect(stats.density).toBeCloseTo(7 / 15, 5);
    expect(stats.componentCount).toBe(1);
    expect(stats.lowConfidenceCount).toBe(1);
    const health = healthScore(stats);
    expect(health.score).toBeGreaterThan(0);
    expect(health.score).toBeLessThanOrEqual(100);
    expect(health.factors.map((factor) => factor.key)).toEqual(["confidence", "freshness", "reciprocity", "coverage"]);
    expect(healthScore(networkStats(graph([], []), NOW)).score).toBe(0);
  });
});

describe("成员画像", () => {
  it("统计主动/被动/互相与最强关系", () => {
    const edges = [edge("a", "b", "好友", { confidence: 0.9 }), edge("b", "a"), edge("c", "a", "同学", { confidence: 0.5 })];
    const profiles = memberProfiles(graph([node("a"), node("b"), node("c")], edges));
    const a = profiles.find((profile) => profile.userId === "a")!;
    expect(a.total).toBe(3);
    expect(a.outgoing).toBe(1);
    expect(a.incoming).toBe(2);
    expect(a.mutual).toBe(1);
    expect(a.neighbors).toBe(2);
    expect(a.strongest?.otherId).toBe("b");
    expect(a.types[0]).toEqual({ type: "好友", count: 2 });
    expect(sortMemberProfiles(profiles, "degree")[0].userId).toBe("a");
    expect(sortMemberProfiles(profiles, "name").map((profile) => profile.name)).toEqual(["成员a", "成员b", "成员c"]);
  });

  it("矩阵成员：取度数最高的 N 人并按社群排列", () => {
    const communities = detectCommunities(["a", "b", "c", "x", "y", "z"], twoTriangles);
    const profiles = memberProfiles(triangleGraph, communities);
    const top = matrixMembers(profiles, 4);
    expect(top).toHaveLength(4);
    expect(top.map((profile) => profile.userId)).toContain("c");
    expect(top.map((profile) => profile.userId)).toContain("x");
    const order = top.map((profile) => profile.community);
    expect([...order].sort((p, q) => (p ?? 0) - (q ?? 0))).toEqual(order);
  });
});

describe("待复核队列", () => {
  it("矛盾、单向亲密、长期未见、低置信", () => {
    const edges = [
      edge("a", "b", "好友"), edge("b", "a", "对立"),
      edge("c", "d", "情侣"), edge("e", "f", "情侣"), edge("f", "e", "情侣"),
      edge("g", "h", "同学", { lastSeenAt: daysAgo(100) }),
      edge("i", "j", "搭子", { confidence: 0.2 }),
    ];
    const conflicts = detectConflicts(edges);
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0].reason).toContain("对立");
    expect(oneSidedEdges(edges).map((item) => item.subjectUserId)).toEqual(["c"]);
    expect(staleEdges(edges, NOW).map((item) => item.subjectUserId)).toEqual(["g"]);
    expect(lowConfidenceEdges(edges).map((item) => item.subjectUserId)).toEqual(["i"]);
  });
});

describe("分布与过滤", () => {
  it("直方图、新鲜度、周活跃与类型统计", () => {
    const edges = [
      edge("a", "b", "好友", { confidence: 0 }),
      edge("b", "a", "好友", { confidence: 1 }),
      edge("a", "c", "同学", { confidence: 0.55, lastSeenAt: daysAgo(20) }),
      edge("c", "d", "同学", { lastSeenAt: daysAgo(200) }),
    ];
    const histogram = confidenceHistogram(edges);
    expect(histogram[0]).toBe(1);
    expect(histogram[9]).toBe(1);
    expect(histogram[8]).toBe(1);
    expect(histogram[5]).toBe(1);
    expect(freshnessBuckets(edges, NOW).map((item) => [item.tier, item.count])).toEqual([["fresh", 2], ["recent", 1], ["aging", 0], ["stale", 1]]);
    const weeks = weeklyActivity(edges, NOW, 4);
    expect(weeks).toEqual([0, 1, 0, 2]);
    const types = typeStats(edges);
    expect(types.map((item) => item.count)).toEqual([2, 2]);
    expect(types.find((item) => item.type === "好友")?.mutualRate).toBe(1);
  });

  it("前端过滤与列表接口同口径", () => {
    const edges = [edge("a", "b", "好友", { note: "开黑" }), edge("b", "c", "同学", { sourceKind: "manual", confidence: 0.3 })];
    const nameOf = (id: string) => ({ a: "阿一", b: "小二", c: "张三" }[id] ?? id);
    expect(filterEdges(edges, { types: new Set(["同学"]) })).toHaveLength(1);
    expect(filterEdges(edges, { source: "manual" })).toHaveLength(1);
    expect(filterEdges(edges, { maxConfidence: 0.39 })).toHaveLength(1);
    expect(filterEdges(edges, { member: "c" })).toHaveLength(1);
    expect(filterEdges(edges, { search: "张三" }, nameOf)).toHaveLength(1);
    expect(filterEdges(edges, { search: "开黑" }, nameOf)).toHaveLength(1);
    expect(filterEdges(edges, { freshness: "stale" }, nameOf, NOW)).toHaveLength(0);
  });

  it("CSV 带 BOM、转义并防公式注入", () => {
    const csv = relationsToCsv([edge("1", "2", "好友", { note: '=HYPERLINK("x"),"引号"' })], (id) => (id === "1" ? "甲,乙" : "丙"), true);
    expect(csv.startsWith("﻿主体QQ,主体,类型")).toBe(true);
    expect(csv).toContain('"甲,乙"');
    expect(csv).toContain(`"'=HYPERLINK(""x""),""引号"""`);
    expect(csv.trimEnd().split("\r\n")[0]).toContain("来源,证据数");
    expect(relationsToCsv([], (id) => id, false)).not.toContain("来源");
  });
});
