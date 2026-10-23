import { describe, expect, it } from "vitest";
import {
  buildExportSvg,
  centerTransform,
  circularLayout,
  communityCenters,
  createRelationSimulation,
  edgePath,
  edgeWidth,
  filterGraphData,
  fitTransform,
  groupEdgesByEndpoint,
  isDashedRelationType,
  LIVE_SIM_MAX_NODES,
  nodeDisplayName,
  nodeRadius,
  relationTypeColor,
  runRelationLayout,
  smartLabelIds,
  wheelZoom,
  type LayoutPosition,
  type RelationLayoutLink,
  type SimLink,
  type SimNode,
} from "./relation-graph";
import type { Simulation } from "d3-force";
import type { AgentRelationGraph, AgentRelationGraphNode, AgentRelationItem } from "./types";

function makeEdge(overrides: Partial<AgentRelationItem> = {}): AgentRelationItem {
  return {
    id: "1",
    groupId: "100",
    subjectUserId: "111",
    objectUserId: "222",
    type: "好友",
    sourceKind: "auto",
    note: "",
    confidence: 0.8,
    evidenceCount: 1,
    lastSeenAt: null,
    ...overrides,
  };
}

function makeNode(overrides: Partial<AgentRelationGraphNode> = {}): AgentRelationGraphNode {
  return {
    userId: "111",
    nickname: "小明",
    groupNickname: null,
    role: "member",
    linked: true,
    degree: 1,
    ...overrides,
  };
}

function makeGraph(
  nodes: AgentRelationGraphNode[],
  edges: AgentRelationItem[],
): AgentRelationGraph {
  return { nodes, edges, meta: { relationTruncated: false, memberTruncated: false } };
}

describe("runRelationLayout", () => {
  const ids = ["111", "222", "333"];
  const links = [
    { source: "111", target: "222" },
    { source: "222", target: "333" },
  ];

  it("相同输入产出确定性布局且无 NaN", () => {
    const first = runRelationLayout(ids, links);
    const second = runRelationLayout(ids, links);
    expect([...first.entries()]).toEqual([...second.entries()]);
    for (const pos of first.values()) {
      expect(Number.isFinite(pos.x)).toBe(true);
      expect(Number.isFinite(pos.y)).toBe(true);
    }
  });

  it("相连节点经力导向后比初始环形位置更近", () => {
    const layout = runRelationLayout(ids, links);
    // 初始环形：排序后相邻节点间隔 120°，弦长 = 2R·sin(60°) ≈ 398.6。
    const dist = (a: { x: number; y: number }, b: { x: number; y: number }) =>
      Math.hypot(a.x - b.x, a.y - b.y);
    expect(dist(layout.get("111")!, layout.get("222")!)).toBeLessThan(360);
    expect(dist(layout.get("222")!, layout.get("333")!)).toBeLessThan(360);
  });

  it("高置信边两端比低置信边靠得更近", () => {
    const strong = runRelationLayout(["a", "b"], [{ source: "a", target: "b", confidence: 1 }]);
    const weak = runRelationLayout(["a", "b"], [{ source: "a", target: "b", confidence: 0 }]);
    const dist = (m: Map<string, { x: number; y: number }>) =>
      Math.hypot(m.get("a")!.x - m.get("b")!.x, m.get("a")!.y - m.get("b")!.y);
    expect(dist(strong)).toBeLessThan(dist(weak));
  });

  it("固定位置的节点保持不动", () => {
    const pinned = new Map([["111", { x: 123, y: 456 }]]);
    const layout = runRelationLayout(ids, links, { pinned });
    expect(layout.get("111")).toEqual({ x: 123, y: 456 });
  });

  it("空输入返回空布局", () => {
    expect(runRelationLayout([], []).size).toBe(0);
  });
});

describe("groupEdgesByEndpoint", () => {
  it("无序端点对分组：A→B 与 B→A 同组且对称偏移", () => {
    const groups = groupEdgesByEndpoint([
      makeEdge({ id: "1", subjectUserId: "111", objectUserId: "222" }),
      makeEdge({ id: "2", subjectUserId: "222", objectUserId: "111", type: "对立" }),
      makeEdge({ id: "3", subjectUserId: "333", objectUserId: "444" }),
    ]);
    const byId = new Map(groups.map((item) => [item.edge.id, item]));
    expect(byId.get("1")!.count).toBe(2);
    expect(byId.get("2")!.count).toBe(2);
    expect(byId.get("1")!.offset).toBeCloseTo(-17, 5);
    expect(byId.get("2")!.offset).toBeCloseTo(17, 5);
    expect(byId.get("3")!.count).toBe(1);
    expect(byId.get("3")!.offset).toBe(0);
  });

  it("自定义间距参与偏移计算", () => {
    const groups = groupEdgesByEndpoint(
      [
        makeEdge({ id: "1", subjectUserId: "111", objectUserId: "222" }),
        makeEdge({ id: "2", subjectUserId: "111", objectUserId: "222", type: "死党" }),
      ],
      40,
    );
    expect(groups[0].offset).toBeCloseTo(-20, 5);
    expect(groups[1].offset).toBeCloseTo(20, 5);
  });
});

describe("edgePath", () => {
  it("零偏移时为直线并截断到节点半径外", () => {
    const geometry = edgePath(0, 0, 100, 0, 0, 10, 10);
    expect(geometry.mid.x).toBeCloseTo(47.5, 5);
    expect(geometry.mid.y).toBeCloseTo(0, 5);
    expect(geometry.end.x).toBeCloseTo(78, 5);
    expect(geometry.end.y).toBeCloseTo(0, 5);
    expect(geometry.endAngle).toBeCloseTo(0, 5);
  });

  it("偏移沿法向弯曲曲线", () => {
    const geometry = edgePath(0, 0, 100, 0, 30, 10, 10);
    // 控制点在 (50, 30)，端点截断沿切线方向，曲线中点落在连线与控制点之间。
    expect(geometry.mid.y).toBeCloseTo(19.3732, 3);
    expect(geometry.mid.y).toBeGreaterThan(0);
    expect(geometry.mid.y).toBeLessThan(30);
    expect(geometry.mid.x).toBeCloseTo(47.8563, 3);
  });
});

describe("relationTypeColor", () => {
  it("预设类型有专属颜色，未知类型回退灰色", () => {
    expect(relationTypeColor("好友")).not.toBe(relationTypeColor("对立"));
    expect(relationTypeColor("自定义关系")).toBe("#9c8f96");
    expect(isDashedRelationType("mentions")).toBe(true);
    expect(isDashedRelationType("好友")).toBe(false);
  });
});

describe("filterGraphData", () => {
  const graph = makeGraph(
    [
      makeNode({ userId: "111", linked: true, degree: 2 }),
      makeNode({ userId: "222", linked: true, degree: 1 }),
      makeNode({ userId: "555", linked: false, degree: 0, nickname: "小刚" }),
    ],
    [
      makeEdge({ id: "1", subjectUserId: "111", objectUserId: "222", type: "好友" }),
      makeEdge({ id: "2", subjectUserId: "111", objectUserId: "222", type: "对立" }),
    ],
  );

  it("默认只保留出现在边中的节点", () => {
    const result = filterGraphData(graph, "", false);
    expect(result.nodes.map((node) => node.userId)).toEqual(["111", "222"]);
    expect(result.edges).toHaveLength(2);
  });

  it("类型过滤同时作用于边与节点连通性", () => {
    const result = filterGraphData(graph, "对立", false);
    expect(result.edges.map((edge) => edge.id)).toEqual(["2"]);
    expect(result.nodes.map((node) => node.userId)).toEqual(["111", "222"]);
    const none = filterGraphData(graph, "师徒", false);
    expect(none.edges).toHaveLength(0);
    expect(none.nodes).toHaveLength(0);
  });

  it("开启孤立开关后显示全部成员节点", () => {
    const result = filterGraphData(graph, "好友", true);
    expect(result.nodes.map((node) => node.userId)).toEqual(["111", "222", "555"]);
    expect(result.edges).toHaveLength(1);
  });
});

describe("nodeDisplayName / nodeRadius / edgeWidth", () => {
  it("群昵称优先，缺省回退 userId", () => {
    expect(nodeDisplayName(makeNode({ groupNickname: "群昵称" }), "111")).toBe("群昵称");
    expect(nodeDisplayName(makeNode({ nickname: "", groupNickname: null }), "111")).toBe("111");
    expect(nodeDisplayName(undefined, "999")).toBe("999");
  });

  it("半径随度数增长且有上限，边宽随置信度增长", () => {
    expect(nodeRadius(9)).toBeGreaterThan(nodeRadius(1));
    expect(nodeRadius(10000)).toBe(nodeRadius(1000));
    expect(edgeWidth(1)).toBeGreaterThan(edgeWidth(0));
    expect(edgeWidth(5)).toBe(edgeWidth(1));
    expect(edgeWidth(-5)).toBe(edgeWidth(0));
  });
});

describe("fitTransform / wheelZoom", () => {
  it("fit 将包围盒居中并受缩放上限约束", () => {
    const transform = fitTransform(
      [
        { x: 0, y: 0 },
        { x: 100, y: 50 },
      ],
      200,
      100,
      10,
    );
    expect(transform.k).toBeCloseTo(1.4, 5);
    expect(transform.x).toBeCloseTo(30, 5);
    expect(transform.y).toBeCloseTo(15, 5);
  });

  it("空输入与无效尺寸安全返回单位视图", () => {
    expect(fitTransform([], 200, 100)).toEqual({ x: 100, y: 50, k: 1 });
    expect(fitTransform([{ x: 1, y: 1 }], 0, 0)).toEqual({ x: 0, y: 0, k: 1 });
  });

  it("缩放围绕指针位置且被夹取到范围", () => {
    const zoomed = wheelZoom({ x: 10, y: 20, k: 1 }, 2, 50, 60);
    expect(zoomed).toEqual({ x: -30, y: -20, k: 2 });
    // 指针下的世界坐标保持不动：world = (screen - t) / k
    const worldBefore = (50 - 10) / 1;
    expect(worldBefore * zoomed.k + zoomed.x).toBeCloseTo(50, 5);
    const clamped = wheelZoom({ x: 0, y: 0, k: 1 }, 100, 0, 0);
    expect(clamped.k).toBe(4);
    expect(wheelZoom({ x: 0, y: 0, k: 1 }, 0.001, 0, 0).k).toBeCloseTo(0.25, 5);
  });
});

describe("createRelationSimulation", () => {
  const positions = new Map<string, LayoutPosition>([
    ["a", { x: 200, y: 200 }],
    ["b", { x: 260, y: 200 }],
    ["c", { x: 700, y: 500 }],
  ]);
  const links: RelationLayoutLink[] = [{ source: "a", target: "b", confidence: 0.9 }];

  const nodeOf = (sim: Simulation<SimNode, SimLink>, id: string): SimNode =>
    sim.nodes().find((node) => node.id === id)!;

  it("空节点列表返回 null", () => {
    expect(createRelationSimulation([], [], new Map())).toBeNull();
  });

  it("相同输入两次构建行为一致（确定性）", () => {
    const run = () => {
      const sim = createRelationSimulation(["a", "b", "c"], links, positions)!;
      const a = nodeOf(sim, "a");
      a.fx = 420;
      a.fy = 380;
      sim.alphaTarget(0.3);
      for (let i = 0; i < 50; i += 1) sim.tick();
      return sim.nodes().map((node) => [node.id, Number(node.x.toFixed(6)), Number(node.y.toFixed(6))]);
    };
    expect(run()).toEqual(run());
  });

  it("拖拽节点牵动相连邻居", () => {
    // 种子取自平衡布局（与组件实际用法一致），再拖动 a 观察邻居跟随。
    const layout = runRelationLayout(["a", "b", "c"], links);
    const sim = createRelationSimulation(["a", "b", "c"], links, layout)!;
    const a = nodeOf(sim, "a");
    const before = Math.hypot(nodeOf(sim, "b").x - (a.x + 200), nodeOf(sim, "b").y - (a.y + 120));
    a.fx = a.x + 200;
    a.fy = a.y + 120;
    sim.alphaTarget(0.3);
    for (let i = 0; i < 80; i += 1) sim.tick();
    const b = nodeOf(sim, "b");
    const after = Math.hypot(b.x - a.x, b.y - a.y);
    expect(after).toBeLessThan(before);
    // 邻居确实被牵动了一段距离，而非原地不动。
    expect(Math.hypot(b.x - layout.get("b")!.x, b.y - layout.get("b")!.y)).toBeGreaterThan(10);
  });

  it("未相连远端节点位移远小于相连邻居", () => {
    const layout = runRelationLayout(["a", "b", "c"], links);
    const sim = createRelationSimulation(["a", "b", "c"], links, layout)!;
    const a = nodeOf(sim, "a");
    a.fx = a.x + 120;
    a.fy = a.y - 80;
    sim.alphaTarget(0.3);
    for (let i = 0; i < 80; i += 1) sim.tick();
    const moveB = Math.hypot(
      nodeOf(sim, "b").x - layout.get("b")!.x,
      nodeOf(sim, "b").y - layout.get("b")!.y,
    );
    const moveC = Math.hypot(
      nodeOf(sim, "c").x - layout.get("c")!.x,
      nodeOf(sim, "c").y - layout.get("c")!.y,
    );
    expect(moveC).toBeLessThan(moveB);
  });

  it("pinned 锚点在模拟期间保持固定", () => {
    const pinned = new Map<string, LayoutPosition>([["c", { x: 700, y: 500 }]]);
    const sim = createRelationSimulation(["a", "b", "c"], links, positions, pinned)!;
    const a = nodeOf(sim, "a");
    a.fx = 300;
    a.fy = 150;
    sim.alphaTarget(0.3);
    for (let i = 0; i < 80; i += 1) sim.tick();
    const c = nodeOf(sim, "c");
    expect(c.x).toBe(700);
    expect(c.y).toBe(500);
    expect(c.fx).toBe(700);
  });

  it("alphaTarget(0) 后系统冷却趋停", () => {
    const sim = createRelationSimulation(["a", "b"], links, positions)!;
    sim.alpha(1).alphaTarget(0);
    for (let i = 0; i < 200; i += 1) sim.tick();
    expect(sim.alpha()).toBeLessThan(0.05);
  });

  it("实时模拟阈值常量为正整数", () => {
    expect(Number.isInteger(LIVE_SIM_MAX_NODES)).toBe(true);
    expect(LIVE_SIM_MAX_NODES).toBeGreaterThan(0);
  });
});

describe("图谱 2.0 辅助函数", () => {
  it("图例关掉的类型从边中剔除，节点随之收敛", () => {
    const graph = makeGraph(
      [makeNode({ userId: "111" }), makeNode({ userId: "222" }), makeNode({ userId: "333" })],
      [
        makeEdge({ id: "1", subjectUserId: "111", objectUserId: "222", type: "好友" }),
        makeEdge({ id: "2", subjectUserId: "222", objectUserId: "333", type: "对立" }),
      ],
    );
    const result = filterGraphData(graph, "", false, new Set(["对立"]));
    expect(result.edges.map((edge) => edge.id)).toEqual(["1"]);
    expect(result.nodes.map((node) => node.userId)).toEqual(["111", "222"]);
  });

  it("分簇布局把同圈子成员拉到一起", () => {
    const ids = ["a", "b", "c", "x", "y", "z"];
    const communities = new Map([["a", 0], ["b", 0], ["c", 0], ["x", 1], ["y", 1], ["z", 1]]);
    const centers = communityCenters(ids, communities);
    expect(centers.get("a")).toEqual(centers.get("b"));
    expect(centers.get("a")).not.toEqual(centers.get("x"));
    const layout = runRelationLayout(ids, [], { centers });
    const dist = (p: string, q: string) => Math.hypot(layout.get(p)!.x - layout.get(q)!.x, layout.get(p)!.y - layout.get(q)!.y);
    expect(dist("a", "b")).toBeLessThan(dist("a", "x"));
  });

  it("环形布局确定、等距且同圈子相邻", () => {
    const communities = new Map([["a", 1], ["b", 0], ["c", 1], ["d", 0]]);
    const degree = new Map([["a", 1], ["b", 3], ["c", 2], ["d", 1]]);
    const layout = circularLayout(["a", "b", "c", "d"], communities, degree);
    expect([...layout.keys()]).toEqual(["b", "d", "c", "a"]);
    const radii = [...layout.values()].map((p) => Math.hypot(p.x - 450, p.y - 320));
    for (const radius of radii) expect(radius).toBeCloseTo(radii[0], 5);
    expect(circularLayout([], communities, degree).size).toBe(0);
  });

  it("居中变换把目标点放到视口中心", () => {
    const transform = centerTransform({ x: 100, y: 50 }, 800, 600, 0.5);
    expect(transform.k).toBeCloseTo(1.15, 5);
    expect(100 * transform.k + transform.x).toBeCloseTo(400, 5);
    expect(50 * transform.k + transform.y).toBeCloseTo(300, 5);
    expect(centerTransform({ x: 0, y: 0 }, 100, 100, 9).k).toBe(4);
  });

  it("智能标签：小图全显示，大图只显示度数靠前的", () => {
    const small = [makeNode({ userId: "1" }), makeNode({ userId: "2" })];
    expect(smartLabelIds(small).size).toBe(2);
    const big = Array.from({ length: 100 }, (_, index) => makeNode({ userId: String(index), degree: index }));
    const labels = smartLabelIds(big);
    expect(labels.size).toBe(25);
    expect(labels.has("99")).toBe(true);
    expect(labels.has("0")).toBe(false);
  });

  it("导出 SVG 自包含且转义名字", () => {
    const nodes = [makeNode({ userId: "1", nickname: "<甲&>" }), makeNode({ userId: "2", nickname: "乙" })];
    const edges = [makeEdge({ subjectUserId: "1", objectUserId: "2" })];
    const positions = new Map([["1", { x: 0, y: 0 }], ["2", { x: 200, y: 100 }]]);
    const { svg, width, height } = buildExportSvg(nodes, edges, positions, () => "#e4558a", "标题");
    expect(svg.startsWith("<svg xmlns=\"http://www.w3.org/2000/svg\"")).toBe(true);
    expect(svg).toContain("&lt;甲&amp;&gt;");
    expect(svg).not.toContain("<甲");
    expect(svg).toContain("<path");
    expect(width).toBeGreaterThan(200);
    expect(height).toBeGreaterThan(100);
  });
});
