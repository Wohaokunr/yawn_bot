// 关系图谱视图：d3-force 计算力导向布局，渲染为手写 SVG（节点=成员、边=关系）。
// 布局/曲线/缩放等均为导出纯函数，便于 vitest 直接断言。
import {
  AimOutlined,
  CompressOutlined,
  ExpandOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from "@ant-design/icons";
import { Alert, Button, Drawer, Empty, List, Popconfirm, Progress, Space, Spin, Switch, Tag, Tooltip, Typography } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from "d3-force";
import type { Simulation } from "d3-force";
import type { AgentRelationGraph, AgentRelationGraphNode, AgentRelationItem } from "./types";
import { formatTime } from "./shared";
import { isDashedRelationType, nodeDisplayName, relationTypeColor } from "./relation-meta";
export {
  RELATION_TYPE_GRAPH_COLORS,
  isDashedRelationType,
  nodeDisplayName,
  relationTypeColor,
} from "./relation-meta";

const { Text } = Typography;

export interface LayoutPosition {
  x: number;
  y: number;
}

export interface RelationLayoutLink {
  source: string;
  target: string;
  confidence?: number;
}

export interface SimNode {
  id: string;
  x: number;
  y: number;
  fx: number | null;
  fy: number | null;
}

export interface SimLink {
  source: string | SimNode;
  target: string | SimNode;
  confidence: number;
}

// 布局画布固定虚拟尺寸：力导向在此坐标系计算，渲染时按容器 fit/缩放。
const LAYOUT_WIDTH = 900;
const LAYOUT_HEIGHT = 640;

export function runRelationLayout(
  nodeIds: string[],
  links: RelationLayoutLink[],
  options: { pinned?: ReadonlyMap<string, LayoutPosition>; iterations?: number } = {},
): Map<string, LayoutPosition> {
  if (nodeIds.length === 0) return new Map();
  const sorted = [...nodeIds].sort();
  const ringRadius = Math.min(LAYOUT_WIDTH, LAYOUT_HEIGHT) * 0.36;
  const cx = LAYOUT_WIDTH / 2;
  const cy = LAYOUT_HEIGHT / 2;
  // 初始环形布局保证确定性，且避免节点重合触发库内抖动。
  const nodes: SimNode[] = sorted.map((id, index) => {
    const pin = options.pinned?.get(id);
    const angle = (index / sorted.length) * Math.PI * 2 - Math.PI / 2;
    return {
      id,
      x: pin?.x ?? cx + ringRadius * Math.cos(angle),
      y: pin?.y ?? cy + ringRadius * Math.sin(angle),
      fx: pin ? pin.x : null,
      fy: pin ? pin.y : null,
    };
  });
  const simLinks: SimLink[] = links.map((link) => ({
    source: link.source,
    target: link.target,
    confidence: link.confidence ?? 0.5,
  }));
  const iterations =
    options.iterations ?? (nodeIds.length > 400 ? 140 : 300);
  const simulation = forceSimulation<SimNode>(nodes)
    .force(
      "link",
      forceLink<SimNode, SimLink>(simLinks)
        .id((node) => node.id)
        // 高置信边理想距离更短，关系强的成员靠得更近。
        .distance((link) => 80 + (1 - Math.min(Math.max(link.confidence, 0), 1)) * 90)
        .strength(0.35),
    )
    .force("charge", forceManyBody<SimNode>().strength(-180))
    .force("collide", forceCollide<SimNode>(30))
    .force("x", forceX<SimNode>(cx).strength(0.05))
    .force("y", forceY<SimNode>(cy).strength(0.08));
  simulation.stop();
  for (let i = 0; i < iterations; i += 1) simulation.tick();
  return new Map(nodes.map((node) => [node.id, { x: node.x, y: node.y }]));
}

// 实时模拟节点数上限：超过后拖拽退化为仅固定被拖节点，保住超大图谱的交互性能。
export const LIVE_SIM_MAX_NODES = 300;

// 交互期实时力导向：力参数与 runRelationLayout 一致，但从当前展示位置种子化并保留
// alpha 冷却语义 —— 拖拽节点设 fx/fy 时相邻节点被弹簧力实时牵动，松手后系统沉降。
export function createRelationSimulation(
  nodeIds: string[],
  links: RelationLayoutLink[],
  positions: ReadonlyMap<string, LayoutPosition>,
  pinned?: ReadonlyMap<string, LayoutPosition>,
): Simulation<SimNode, SimLink> | null {
  if (nodeIds.length === 0) return null;
  const sorted = [...nodeIds].sort();
  const cx = LAYOUT_WIDTH / 2;
  const cy = LAYOUT_HEIGHT / 2;
  const nodes: SimNode[] = sorted.map((id) => {
    const pos = positions.get(id) ?? { x: cx, y: cy };
    const pin = pinned?.get(id);
    return {
      id,
      x: pos.x,
      y: pos.y,
      fx: pin ? pin.x : null,
      fy: pin ? pin.y : null,
    };
  });
  const simLinks: SimLink[] = links.map((link) => ({
    source: link.source,
    target: link.target,
    confidence: link.confidence ?? 0.5,
  }));
  const simulation = forceSimulation<SimNode>(nodes)
    .force(
      "link",
      forceLink<SimNode, SimLink>(simLinks)
        .id((node) => node.id)
        .distance((link) => 80 + (1 - Math.min(Math.max(link.confidence, 0), 1)) * 90)
        .strength(0.35),
    )
    .force("charge", forceManyBody<SimNode>().strength(-180))
    .force("collide", forceCollide<SimNode>(30))
    .force("x", forceX<SimNode>(cx).strength(0.05))
    .force("y", forceY<SimNode>(cy).strength(0.08));
  simulation.stop();
  return simulation;
}

export interface GroupedRelationEdge {
  edge: AgentRelationItem;
  index: number;
  count: number;
  // 组内第 index 条边的曲线法向偏移，同一对节点多条边互不重叠。
  offset: number;
}

// 按无序端点对分组：A→B 与 B→A 属同一组，组内对称展开。
export function groupEdgesByEndpoint(
  edges: AgentRelationItem[],
  spacing = 34,
): GroupedRelationEdge[] {
  const groups = new Map<string, AgentRelationItem[]>();
  for (const edge of edges) {
    const key = [edge.subjectUserId, edge.objectUserId].sort().join("→");
    const bucket = groups.get(key);
    if (bucket) bucket.push(edge);
    else groups.set(key, [edge]);
  }
  const result: GroupedRelationEdge[] = [];
  for (const bucket of groups.values()) {
    bucket.forEach((edge, index) => {
      result.push({
        edge,
        index,
        count: bucket.length,
        offset: (index - (bucket.length - 1) / 2) * spacing,
      });
    });
  }
  return result;
}

export interface RelationEdgeGeometry {
  path: string;
  mid: LayoutPosition;
  end: LayoutPosition;
  // 边终点处曲线方向（弧度），箭头按此旋转。
  endAngle: number;
}

// 二次贝塞尔曲线：端点截断到节点圆外（目标端多留箭头余量），中点供 label 定位。
export function edgePath(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  offset: number,
  sourceRadius: number,
  targetRadius: number,
): RelationEdgeGeometry {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const length = Math.hypot(dx, dy) || 1;
  const nx = -dy / length;
  const ny = dx / length;
  const cx = (x1 + x2) / 2 + nx * offset;
  const cy = (y1 + y2) / 2 + ny * offset;
  const t1x = cx - x1;
  const t1y = cy - y1;
  const t1len = Math.hypot(t1x, t1y) || 1;
  const t2x = x2 - cx;
  const t2y = y2 - cy;
  const t2len = Math.hypot(t2x, t2y) || 1;
  const ax = x1 + (t1x / t1len) * (sourceRadius + 2);
  const ay = y1 + (t1y / t1len) * (sourceRadius + 2);
  const bx = x2 - (t2x / t2len) * (targetRadius + 12);
  const by = y2 - (t2y / t2len) * (targetRadius + 12);
  return {
    path: `M ${ax.toFixed(2)} ${ay.toFixed(2)} Q ${cx.toFixed(2)} ${cy.toFixed(2)} ${bx.toFixed(2)} ${by.toFixed(2)}`,
    mid: { x: (ax + 2 * cx + bx) / 4, y: (ay + 2 * cy + by) / 4 },
    end: { x: bx, y: by },
    endAngle: Math.atan2(by - cy, bx - cx),
  };
}

export function nodeRadius(degree: number): number {
  return 15 + Math.min(9, Math.sqrt(Math.max(degree, 0)) * 2.6);
}

export function edgeWidth(confidence: number): number {
  return 1.4 + Math.min(Math.max(confidence, 0), 1) * 2.2;
}

export interface FilteredRelationGraph {
  nodes: AgentRelationGraphNode[];
  edges: AgentRelationItem[];
}

// 类型过滤边；默认只保留过滤后仍出现在边中的节点，开 showIsolated 才显示无边成员。
export function filterGraphData(
  graph: AgentRelationGraph,
  typeFilter: string,
  showIsolated: boolean,
): FilteredRelationGraph {
  const edges = typeFilter
    ? graph.edges.filter((edge) => edge.type === typeFilter)
    : graph.edges;
  if (showIsolated) return { nodes: graph.nodes, edges };
  const linked = new Set<string>();
  for (const edge of edges) {
    linked.add(edge.subjectUserId);
    linked.add(edge.objectUserId);
  }
  return { nodes: graph.nodes.filter((node) => linked.has(node.userId)), edges };
}

export interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

// 计算 world→screen 的平移缩放，使包围盒居中适配容器。
export function fitTransform(
  points: LayoutPosition[],
  width: number,
  height: number,
  padding = 56,
): ViewTransform {
  if (points.length === 0 || width <= 0 || height <= 0)
    return { x: width / 2, y: height / 2, k: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
  }
  const bw = Math.max(maxX - minX, 1);
  const bh = Math.max(maxY - minY, 1);
  const k = Math.max(
    0.15,
    Math.min((width - padding * 2) / bw, (height - padding * 2) / bh, 1.4),
  );
  return {
    k,
    x: (width - (maxX + minX) * k) / 2,
    y: (height - (maxY + minY) * k) / 2,
  };
}

// 围绕指针位置 (cx, cy) 缩放，保持指针下的世界坐标不动。
export function wheelZoom(
  transform: ViewTransform,
  factor: number,
  cx: number,
  cy: number,
  min = 0.25,
  max = 4,
): ViewTransform {
  const k = Math.min(max, Math.max(min, transform.k * factor));
  const ratio = k / transform.k;
  return {
    k,
    x: cx - (cx - transform.x) * ratio,
    y: cy - (cy - transform.y) * ratio,
  };
}

// 节点糖果渐变：角色决定基色，左上高光营造立体感；key 同时用作 <defs> 渐变 id。
const NODE_GRADIENTS: Record<string, { base: string; highlight: string }> = {
  owner: { base: "#f2c14e", highlight: "#ffe9ad" },
  admin: { base: "#8fd6bd", highlight: "#d2f7e8" },
  bot: { base: "#9db9e8", highlight: "#dbe9fc" },
  member: { base: "#f4a7c3", highlight: "#ffd9e9" },
  unlinked: { base: "#d8cdd4", highlight: "#f0e9ee" },
};

function nodeGradientKey(node: AgentRelationGraphNode): string {
  if (!node.linked) return "unlinked";
  return NODE_GRADIENTS[node.role] ? node.role : "member";
}

function nodeFillUrl(node: AgentRelationGraphNode): string {
  return `url(#rg-grad-${nodeGradientKey(node)})`;
}

interface RelationGraphViewProps {
  graph: AgentRelationGraph;
  typeFilter: string;
  onEditRelation: (edge: AgentRelationItem) => void;
  onDeleteRelation: (edge: AgentRelationItem) => void;
}

export function RelationGraphView({
  graph,
  typeFilter,
  onEditRelation,
  onDeleteRelation,
}: RelationGraphViewProps): React.JSX.Element {
  const [showIsolated, setShowIsolated] = useState(false);
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, k: 1 });
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);
  const [hoverEdgeId, setHoverEdgeId] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [relayoutCounter, setRelayoutCounter] = useState(0);
  const [dragPos, setDragPos] = useState<{ id: string; pos: LayoutPosition } | null>(null);
  const [maximized, setMaximized] = useState(false);
  // 程序性变换（适配/缩放按钮）启用 CSS 过渡；滚轮与拖拽期间关闭保证即时响应。
  const [smoothTransform, setSmoothTransform] = useState(false);
  // 实时模拟期间的位置覆盖层；null 表示未在模拟。
  const [simPositions, setSimPositions] = useState<Map<string, LayoutPosition> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const smoothTimerRef = useRef(0);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const simVersionRef = useRef("");
  const simNodesRef = useRef(new Map<string, SimNode>());
  // 手动拖拽固定的节点位置；ref 存放以免拖动触发整个布局重算。
  const pinnedRef = useRef(new Map<string, LayoutPosition>());
  const dragRef = useRef<{
    id: string;
    startClientX: number;
    startClientY: number;
    startPos: LayoutPosition;
    moved: boolean;
  } | null>(null);
  const clickWasDragRef = useRef(false);
  // 手动重排（清钉/解钉）时置位：模拟失效清理不再回写位置，否则会把刚清掉的固定加回去。
  const resetLayoutRef = useRef(false);
  const panRef = useRef<{ startClientX: number; startClientY: number; start: ViewTransform } | null>(null);

  const filtered = useMemo(
    () => filterGraphData(graph, typeFilter, showIsolated),
    [graph, typeFilter, showIsolated],
  );
  const nodeById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.userId, node])),
    [graph.nodes],
  );
  const groupedEdges = useMemo(
    () => groupEdgesByEndpoint(filtered.edges),
    [filtered.edges],
  );
  // 布局签名：数据 reload 产生新对象但节点/边集合不变时复用旧布局，不打断用户浏览。
  const layoutSignature = useMemo(
    () =>
      `${filtered.nodes.map((node) => node.userId).join(",")}|${filtered.edges
        .map((edge) => edge.id)
        .join(",")}`,
    [filtered],
  );
  const layout = useMemo(() => {
    void relayoutCounter; // 重新布局按钮递增此值触发重算
    return runRelationLayout(
      filtered.nodes.map((node) => node.userId),
      filtered.edges.map((edge) => ({
        source: edge.subjectUserId,
        target: edge.objectUserId,
        confidence: edge.confidence,
      })),
      { pinned: pinnedRef.current },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature, relayoutCounter]);

  // 当前生效位置 = 布局结果 + 手动固定覆盖 + 实时模拟覆盖 + 正在拖拽的节点（大图退化路径）。
  const positions = new Map(layout);
  for (const [id, pos] of pinnedRef.current) positions.set(id, pos);
  if (simPositions)
    for (const [id, pos] of simPositions) positions.set(id, pos);
  if (dragPos) positions.set(dragPos.id, dragPos.pos);
  // doFit 需要读取最新位置（含手动固定/拖拽中），用 ref 避免闭包过期且不触发重算。
  const positionsRef = useRef(positions);
  positionsRef.current = positions;
  const sizeRef = useRef(size);
  sizeRef.current = size;

  const applyTransformAnimated = useCallback((updater: (prev: ViewTransform) => ViewTransform) => {
    setSmoothTransform(true);
    setTransform(updater);
    window.clearTimeout(smoothTimerRef.current);
    smoothTimerRef.current = window.setTimeout(() => setSmoothTransform(false), 480);
  }, []);

  const doFit = useCallback(() => {
    applyTransformAnimated(() =>
      fitTransform([...positionsRef.current.values()], sizeRef.current.w, sizeRef.current.h),
    );
  }, [applyTransformAnimated]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setSize({ w: Math.round(rect.width), h: Math.round(rect.height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (size.w > 0 && size.h > 0 && layout.size > 0) doFit();
  }, [layout, size.w, size.h, doFit]);

  // React onWheel 为 passive 事件，须用原生监听才能阻止页面滚动。
  useEffect(() => {
    const element = svgRef.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setSmoothTransform(false);
      window.clearTimeout(smoothTimerRef.current);
      const rect = element.getBoundingClientRect();
      setTransform((prev) =>
        wheelZoom(
          prev,
          event.deltaY < 0 ? 1.15 : 1 / 1.15,
          event.clientX - rect.left,
          event.clientY - rect.top,
        ),
      );
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, []);

  // 模拟版本 = 布局签名 + 重排计数：拓扑变化或手动重排后旧模拟即失效，
  // 先把沉降结果固化进 pinnedRef 再丢弃，避免清空覆盖层时画面跳回。
  const simVersion = `${layoutSignature}#${relayoutCounter}`;
  useEffect(() => {
    return () => {
      const simulation = simRef.current;
      if (simulation && !dragRef.current && !resetLayoutRef.current) {
        for (const node of simulation.nodes()) {
          pinnedRef.current.set(node.id, { x: node.fx ?? node.x, y: node.fy ?? node.y });
        }
      }
      resetLayoutRef.current = false;
      simulation?.stop();
      simRef.current = null;
      setSimPositions(null);
    };
  }, [simVersion]);

  useEffect(
    () => () => {
      simRef.current?.stop();
      simRef.current = null;
      window.clearTimeout(smoothTimerRef.current);
    },
    [],
  );

  // 最大化：Esc 还原（详情抽屉打开时留给抽屉），期间锁定页面滚动。
  useEffect(() => {
    if (!maximized) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !selectedNodeId && !selectedEdgeId) setMaximized(false);
    };
    window.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [maximized, selectedNodeId, selectedEdgeId]);

  const liveSimEnabled = filtered.nodes.length <= LIVE_SIM_MAX_NODES;

  // 惰性创建/复用实时模拟；拖拽结束系统冷却停止时（"end"）统一固化位置。
  const ensureSimulation = (): Simulation<SimNode, SimLink> | null => {
    if (simRef.current && simVersionRef.current === simVersion) return simRef.current;
    simRef.current?.stop();
    const simulation = createRelationSimulation(
      filtered.nodes.map((node) => node.userId),
      filtered.edges.map((edge) => ({
        source: edge.subjectUserId,
        target: edge.objectUserId,
        confidence: edge.confidence,
      })),
      positionsRef.current,
      pinnedRef.current,
    );
    if (!simulation) return null;
    simNodesRef.current = new Map(simulation.nodes().map((node) => [node.id, node]));
    simulation.on("tick", () => {
      setSimPositions(
        new Map(simulation.nodes().map((node) => [node.id, { x: node.x, y: node.y }])),
      );
    });
    simulation.on("end", () => {
      if (dragRef.current) return;
      for (const node of simulation.nodes()) {
        pinnedRef.current.set(node.id, { x: node.fx ?? node.x, y: node.fy ?? node.y });
      }
      setSimPositions(null);
    });
    simVersionRef.current = simVersion;
    simRef.current = simulation;
    return simulation;
  };

  const handleBackgroundPointerDown = (event: React.PointerEvent) => {
    panRef.current = {
      startClientX: event.clientX,
      startClientY: event.clientY,
      start: transform,
    };
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
  };

  const liveSimActive = (): boolean =>
    liveSimEnabled && simRef.current !== null && simVersionRef.current === simVersion;

  const handlePointerMove = (event: React.PointerEvent) => {
    const pan = panRef.current;
    if (pan && !dragRef.current) {
      setSmoothTransform(false);
      window.clearTimeout(smoothTimerRef.current);
      setTransform({
        ...pan.start,
        x: pan.start.x + event.clientX - pan.startClientX,
        y: pan.start.y + event.clientY - pan.startClientY,
      });
      return;
    }
    const drag = dragRef.current;
    if (drag) {
      if (
        Math.abs(event.clientX - drag.startClientX) +
          Math.abs(event.clientY - drag.startClientY) >
        3
      )
        drag.moved = true;
      if (liveSimActive()) {
        // 实时模拟模式：被拖节点直接锚定到指针世界坐标，邻居由弹簧力牵动。
        const simNode = simNodesRef.current.get(drag.id);
        const rect = svgRef.current?.getBoundingClientRect();
        if (simNode && rect) {
          simNode.fx = (event.clientX - rect.left - transform.x) / transform.k;
          simNode.fy = (event.clientY - rect.top - transform.y) / transform.k;
        }
        return;
      }
      const dx = (event.clientX - drag.startClientX) / transform.k;
      const dy = (event.clientY - drag.startClientY) / transform.k;
      setDragPos({
        id: drag.id,
        pos: { x: drag.startPos.x + dx, y: drag.startPos.y + dy },
      });
    }
  };

  const handlePointerUp = () => {
    const drag = dragRef.current;
    if (drag && liveSimActive()) {
      // 松手后系统冷却沉降，位置在模拟 "end" 事件里统一固化。
      simRef.current?.alphaTarget(0);
    } else if (drag && dragPos) {
      pinnedRef.current.set(drag.id, dragPos.pos);
    }
    clickWasDragRef.current = drag?.moved ?? false;
    dragRef.current = null;
    panRef.current = null;
    setDragPos(null);
  };

  const handleNodePointerDown = (event: React.PointerEvent, id: string) => {
    event.stopPropagation();
    const pos = positions.get(id);
    if (!pos) return;
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
    if (liveSimEnabled) {
      const simulation = ensureSimulation();
      const simNode = simNodesRef.current.get(id);
      if (simulation && simNode) {
        simNode.fx = pos.x;
        simNode.fy = pos.y;
        simulation.alpha(1).alphaTarget(0.28).restart();
      }
    }
    dragRef.current = {
      id,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startPos: pos,
      moved: false,
    };
  };

  const handleNodeClick = (id: string) => {
    if (clickWasDragRef.current) return;
    setSelectedNodeId((prev) => (prev === id ? null : id));
    setSelectedEdgeId(null);
  };

  const handleNodeDoubleClick = (id: string) => {
    resetLayoutRef.current = true;
    pinnedRef.current.delete(id);
    setRelayoutCounter((counter) => counter + 1);
  };

  const handleEdgeClick = (id: string) => {
    setSelectedEdgeId((prev) => (prev === id ? null : id));
    setSelectedNodeId(null);
  };

  const selectedNode = selectedNodeId ? (nodeById.get(selectedNodeId) ?? null) : null;
  const selectedEdge = selectedEdgeId
    ? (graph.edges.find((edge) => edge.id === selectedEdgeId) ?? null)
    : null;
  const focusNode = hoverNodeId ?? selectedNodeId;
  const focusEdge = hoverEdgeId ?? selectedEdgeId;
  const hasFocus = focusNode !== null || focusEdge !== null;
  const legendTypes = useMemo(
    () =>
      Array.from(new Set(filtered.edges.map((edge) => edge.type))).sort(
        (a, b) => a.localeCompare(b, "zh-Hans-CN"),
      ),
    [filtered.edges],
  );

  const renderEdge = (grouped: GroupedRelationEdge) => {
    const { edge, offset } = grouped;
    const from = positions.get(edge.subjectUserId);
    const to = positions.get(edge.objectUserId);
    if (!from || !to) return null;
    const fromNode = nodeById.get(edge.subjectUserId);
    const toNode = nodeById.get(edge.objectUserId);
    const geometry = edgePath(
      from.x,
      from.y,
      to.x,
      to.y,
      offset,
      nodeRadius(fromNode?.degree ?? 1),
      nodeRadius(toNode?.degree ?? 1),
    );
    const color = relationTypeColor(edge.type);
    const active =
      (focusNode !== null &&
        (edge.subjectUserId === focusNode || edge.objectUserId === focusNode)) ||
      edge.id === focusEdge;
    const opacity = active ? 1 : hasFocus ? 0.12 : 0.8;
    const width = edgeWidth(edge.confidence);
    const dashed = isDashedRelationType(edge.type);
    const showLabel = active;
    return (
      <g
        key={edge.id}
        className={`rg-edge${active ? " rg-edge-active" : ""}${dashed ? " rg-edge-dashed" : ""}`}
        opacity={opacity}
      >
        <path
          d={geometry.path}
          fill="none"
          stroke="transparent"
          strokeWidth={width + 12}
          style={{ cursor: "pointer" }}
          pointerEvents="stroke"
          onPointerEnter={() => setHoverEdgeId(edge.id)}
          onPointerLeave={() => setHoverEdgeId(null)}
          onClick={() => handleEdgeClick(edge.id)}
        />
        {active && (
          <path
            className="rg-edge-glow"
            d={geometry.path}
            fill="none"
            stroke={color}
            strokeWidth={width + 5}
            strokeLinecap="round"
            pointerEvents="none"
          />
        )}
        <path
          className="rg-edge-line"
          d={geometry.path}
          fill="none"
          stroke={color}
          strokeWidth={width}
          strokeLinecap="round"
          strokeDasharray={dashed ? "6 6" : undefined}
          pointerEvents="none"
        />
        <polygon
          points="10,0 -7,5.5 -7,-5.5"
          fill={color}
          transform={`translate(${geometry.end.x.toFixed(2)},${geometry.end.y.toFixed(2)}) rotate(${((geometry.endAngle * 180) / Math.PI).toFixed(2)})`}
          pointerEvents="none"
        />
        {showLabel && (
          <text
            x={geometry.mid.x}
            y={geometry.mid.y - 6}
            textAnchor="middle"
            className="rg-edge-label"
            pointerEvents="none"
          >
            {edge.type}
          </text>
        )}
      </g>
    );
  };

  const renderNode = (node: AgentRelationGraphNode, index: number) => {
    const pos = positions.get(node.userId);
    if (!pos) return null;
    const radius = nodeRadius(node.degree);
    const selected = selectedNodeId === node.userId;
    const name = nodeDisplayName(node, node.userId);
    return (
      <g
        key={node.userId}
        className="rg-node"
        transform={`translate(${pos.x.toFixed(2)},${pos.y.toFixed(2)})`}
        style={{ "--i": Math.min(index, 24) } as React.CSSProperties}
        onPointerDown={(event) => handleNodePointerDown(event, node.userId)}
        onPointerEnter={() => setHoverNodeId(node.userId)}
        onPointerLeave={() => setHoverNodeId(null)}
        onClick={() => handleNodeClick(node.userId)}
        onDoubleClick={() => handleNodeDoubleClick(node.userId)}
      >
        {selected && <circle className="rg-node-select-ring" r={radius + 6} fill="none" />}
        <g className="rg-node-core">
          <circle
            r={radius}
            fill={nodeFillUrl(node)}
            stroke={selected ? "#d63f71" : "rgba(255,255,255,0.9)"}
            strokeWidth={selected ? 2.5 : 2}
          />
          <text textAnchor="middle" dominantBaseline="central" className="rg-node-initial" pointerEvents="none">
            {name.slice(0, 1)}
          </text>
        </g>
        <text y={radius + 15} textAnchor="middle" className="rg-node-label" pointerEvents="none">
          {name}
        </text>
      </g>
    );
  };

  const nodeEdgeList = (userId: string): AgentRelationItem[] =>
    graph.edges.filter(
      (edge) => edge.subjectUserId === userId || edge.objectUserId === userId,
    );

  const renderEdgeListItem = (edge: AgentRelationItem, userId: string) => {
    const outgoing = edge.subjectUserId === userId;
    const otherId = outgoing ? edge.objectUserId : edge.subjectUserId;
    return (
      <List.Item
        key={edge.id}
        actions={[
          <Button key="edit" type="link" size="small" onClick={() => { setSelectedNodeId(null); onEditRelation(edge); }}>
            编辑
          </Button>,
          <Popconfirm key="delete" title="删除这条关系边？" onConfirm={() => { setSelectedNodeId(null); onDeleteRelation(edge); }}>
            <Button type="link" size="small" danger>删除</Button>
          </Popconfirm>,
        ]}
      >
        <List.Item.Meta
          title={
            <Space size={6} wrap>
              <Tag color={relationTypeColor(edge.type)} style={{ marginInlineEnd: 0 }}>{edge.type}</Tag>
              <Text type="secondary">{outgoing ? "→" : "←"}</Text>
              <Text>{nodeDisplayName(nodeById.get(otherId), otherId)}</Text>
              <Text type="secondary" copyable>{otherId}</Text>
            </Space>
          }
          description={
            <>
              <Progress percent={Math.round(edge.confidence * 100)} size="small" showInfo={false} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                置信度 {edge.confidence.toFixed(2)} · 证据 {edge.evidenceCount} · {edge.note || "无备注"}
              </Text>
            </>
          }
        />
      </List.Item>
    );
  };

  if (filtered.nodes.length === 0) {
    return (
      <Empty
        description={graph.edges.length === 0 ? "暂无关系记忆" : "当前过滤条件下暂无关系"}
        style={{ padding: "48px 0" }}
      />
    );
  }

  return (
    <>
      {maximized && <div className="rg-maximize-backdrop" onClick={() => setMaximized(false)} />}
      <div className={maximized ? "rg-wrap rg-maximized" : "rg-wrap"} ref={containerRef}>
      {graph.meta.relationTruncated && (
        <Alert
          className="rg-truncated"
          type="warning"
          showIcon
          message="关系边超过 5000 条，图谱仅展示前 5000 条"
        />
      )}
      <svg
        ref={svgRef}
        className="rg-svg"
        width={size.w || 900}
        height={size.h || 620}
        onPointerDown={handleBackgroundPointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
      >
        <defs>
          {Object.entries(NODE_GRADIENTS).map(([key, stops]) => (
            <radialGradient key={key} id={`rg-grad-${key}`} cx="0.32" cy="0.28" r="0.95">
              <stop offset="0%" stopColor={stops.highlight} />
              <stop offset="100%" stopColor={stops.base} />
            </radialGradient>
          ))}
        </defs>
        <g
          className={smoothTransform ? "rg-viewport rg-viewport-anim" : "rg-viewport"}
          style={{
            transform: `translate(${transform.x.toFixed(2)}px, ${transform.y.toFixed(2)}px) scale(${transform.k.toFixed(4)})`,
          }}
        >
          {groupedEdges.map(renderEdge)}
          {filtered.nodes.map((node, index) => renderNode(node, index))}
        </g>
      </svg>
      {size.w === 0 && <Spin className="rg-loading" />}
      <div className="rg-toolbar">
        <Space size={4} wrap>
          <Tooltip title="重新自动布局（清除手动固定）">
            <Button size="small" icon={<ReloadOutlined />} onClick={() => { resetLayoutRef.current = true; pinnedRef.current.clear(); setRelayoutCounter((c) => c + 1); }} />
          </Tooltip>
          <Tooltip title="适配全图">
            <Button size="small" icon={<AimOutlined />} onClick={doFit} />
          </Tooltip>
          <Tooltip title="放大">
            <Button size="small" icon={<ZoomInOutlined />} onClick={() => applyTransformAnimated((t) => wheelZoom(t, 1.25, size.w / 2, size.h / 2))} />
          </Tooltip>
          <Tooltip title="缩小">
            <Button size="small" icon={<ZoomOutOutlined />} onClick={() => applyTransformAnimated((t) => wheelZoom(t, 1 / 1.25, size.w / 2, size.h / 2))} />
          </Tooltip>
          <Tooltip title={maximized ? "还原（Esc）" : "最大化查看"}>
            <Button
              size="small"
              icon={maximized ? <CompressOutlined /> : <ExpandOutlined />}
              onClick={() => setMaximized((value) => !value)}
            />
          </Tooltip>
        </Space>
        <span className="rg-isolate-toggle">
          显示无边成员 <Switch size="small" checked={showIsolated} onChange={setShowIsolated} />
        </span>
      </div>
      <div className="rg-legend">
        {legendTypes.map((type) => {
          const color = relationTypeColor(type);
          return (
            <span key={type} className="rg-legend-item">
              <span
                className="rg-legend-dot"
                style={{ background: color, boxShadow: `0 0 6px ${color}66` }}
              />
              {type}
            </span>
          );
        })}
      </div>
      <div className="rg-hint">
        拖动节点会牵动相邻 · 双击解除固定 · 滚轮缩放 · 空白拖拽平移
        {maximized ? " · Esc 退出最大化" : ""}
      </div>
      <Drawer
        open={!!selectedNode}
        width={440}
        title={
          selectedNode
            ? `${nodeDisplayName(selectedNode, selectedNode.userId)} · 关系明细`
            : ""
        }
        onClose={() => setSelectedNodeId(null)}
      >
        {selectedNode && (
          <>
            <Space direction="vertical" size={4} style={{ marginBottom: 12 }}>
              <Text type="secondary" copyable>{selectedNode.userId}</Text>
              <Text type="secondary">
                {selectedNode.linked ? `共 ${selectedNode.degree} 条关系` : "暂无关系边"}
                {selectedNode.groupNickname ? ` · 群昵称 ${selectedNode.groupNickname}` : ""}
              </Text>
            </Space>
            {nodeEdgeList(selectedNode.userId).length > 0 ? (
              <List
                dataSource={nodeEdgeList(selectedNode.userId)}
                renderItem={(edge) => renderEdgeListItem(edge, selectedNode.userId)}
              />
            ) : (
              <Empty description="暂无关系边" />
            )}
          </>
        )}
      </Drawer>
      <Drawer
        open={!!selectedEdge}
        width={440}
        title="关系边详情"
        onClose={() => setSelectedEdgeId(null)}
      >
        {selectedEdge && (
          <>
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Space size={8} wrap>
                <Tag color={relationTypeColor(selectedEdge.type)}>{selectedEdge.type}</Tag>
                <Text>{nodeDisplayName(nodeById.get(selectedEdge.subjectUserId), selectedEdge.subjectUserId)}</Text>
                <Text type="secondary">→</Text>
                <Text>{nodeDisplayName(nodeById.get(selectedEdge.objectUserId), selectedEdge.objectUserId)}</Text>
              </Space>
              <Text type="secondary">
                {selectedEdge.subjectUserId} → {selectedEdge.objectUserId}
              </Text>
              <Progress percent={Math.round(selectedEdge.confidence * 100)} size="small" />
              <Text type="secondary" style={{ fontSize: 12 }}>
                置信度 {selectedEdge.confidence.toFixed(2)} · 证据 {selectedEdge.evidenceCount} ·
                最后见到 {selectedEdge.lastSeenAt ? formatTime(selectedEdge.lastSeenAt) : "—"}
              </Text>
              {selectedEdge.note && <Text>{selectedEdge.note}</Text>}
            </Space>
            <div style={{ marginTop: 16 }}>
              <Space>
                <Button type="primary" onClick={() => { setSelectedEdgeId(null); onEditRelation(selectedEdge); }}>
                  编辑备注 / 置信度
                </Button>
                <Popconfirm title="删除这条关系边？" onConfirm={() => { setSelectedEdgeId(null); onDeleteRelation(selectedEdge); }}>
                  <Button danger>删除</Button>
                </Popconfirm>
              </Space>
            </div>
          </>
        )}
      </Drawer>
      </div>
    </>
  );
}
