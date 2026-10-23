// 关系图谱视图：d3-force 计算力导向布局，渲染为手写 SVG（节点=成员、边=关系）。
// 布局/曲线/缩放/导出等均为导出纯函数，便于 vitest 直接断言。
// 交互：搜索定位、邻域聚焦、最短路径、按圈子着色/分簇、类型图例开关、小地图、Shift 拖线新建关系、导出图片。
import {
  AimOutlined,
  CompressOutlined,
  DownloadOutlined,
  ExpandOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SettingOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from "@ant-design/icons";
import {
  Alert, AutoComplete, Button, Dropdown, Empty, Popover, Segmented, Space, Spin, Switch, Tooltip, Typography,
} from "antd";
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
import { isDashedRelationType, nodeDisplayName, relationTypeColor } from "./relation-meta";
import {
  communityColor,
  detectCommunities,
  egoNetwork,
  freshnessTier,
  pathEdgeIds,
  relationSentence,
  relativeTime,
  shortestPath,
} from "./relations/relation-analysis";
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
  options: {
    pinned?: ReadonlyMap<string, LayoutPosition>;
    iterations?: number;
    /** 分簇布局：每个节点被额外拉向所属圈子的中心。 */
    centers?: ReadonlyMap<string, LayoutPosition>;
  } = {},
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
  const centers = options.centers;
  const simulation = forceSimulation<SimNode>(nodes)
    .force(
      "link",
      forceLink<SimNode, SimLink>(simLinks)
        .id((node) => node.id)
        // 高置信边理想距离更短，关系强的成员靠得更近。
        .distance((link) => 80 + (1 - Math.min(Math.max(link.confidence, 0), 1)) * 90)
        .strength(centers ? 0.2 : 0.35),
    )
    .force("charge", forceManyBody<SimNode>().strength(-180))
    .force("collide", forceCollide<SimNode>(30))
    .force("x", forceX<SimNode>((node) => centers?.get(node.id)?.x ?? cx).strength(centers ? 0.28 : 0.05))
    .force("y", forceY<SimNode>((node) => centers?.get(node.id)?.y ?? cy).strength(centers ? 0.28 : 0.08));
  simulation.stop();
  for (let i = 0; i < iterations; i += 1) simulation.tick();
  return new Map(nodes.map((node) => [node.id, { x: node.x, y: node.y }]));
}

// 圈子中心：按圈子规模排在一个大环上，大圈子靠前；单人圈子收在中心附近。
export function communityCenters(
  nodeIds: string[],
  communities: ReadonlyMap<string, number>,
): Map<string, LayoutPosition> {
  const sizes = new Map<number, number>();
  for (const id of nodeIds) {
    const index = communities.get(id) ?? -1;
    sizes.set(index, (sizes.get(index) ?? 0) + 1);
  }
  const groups = [...sizes.entries()].filter(([, size]) => size > 1).map(([index]) => index).sort((a, b) => a - b);
  const cx = LAYOUT_WIDTH / 2;
  const cy = LAYOUT_HEIGHT / 2;
  const radius = groups.length > 1 ? Math.min(LAYOUT_WIDTH, LAYOUT_HEIGHT) * 0.34 : 0;
  const centerOf = new Map<number, LayoutPosition>();
  groups.forEach((index, order) => {
    const angle = (order / groups.length) * Math.PI * 2 - Math.PI / 2;
    centerOf.set(index, { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) });
  });
  return new Map(nodeIds.map((id) => [id, centerOf.get(communities.get(id) ?? -1) ?? { x: cx, y: cy }]));
}

// 环形布局：同圈子相邻、圈内按度数降序，适合一眼看清跨圈子的连线。
export function circularLayout(
  nodeIds: string[],
  communities: ReadonlyMap<string, number>,
  degree: ReadonlyMap<string, number>,
): Map<string, LayoutPosition> {
  if (nodeIds.length === 0) return new Map();
  const ordered = [...nodeIds].sort((a, b) =>
    (communities.get(a) ?? 999) - (communities.get(b) ?? 999)
    || (degree.get(b) ?? 0) - (degree.get(a) ?? 0)
    || a.localeCompare(b));
  const radius = Math.max(140, Math.min(LAYOUT_WIDTH, LAYOUT_HEIGHT) * 0.42, (ordered.length * 34) / (Math.PI * 2));
  return new Map(ordered.map((id, index) => {
    const angle = (index / ordered.length) * Math.PI * 2 - Math.PI / 2;
    return [id, { x: LAYOUT_WIDTH / 2 + radius * Math.cos(angle), y: LAYOUT_HEIGHT / 2 + radius * Math.sin(angle) }];
  }));
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

// 类型过滤边（单选筛选 + 图例里关掉的类型）；默认只保留过滤后仍出现在边中的节点，开 showIsolated 才显示无边成员。
export function filterGraphData(
  graph: AgentRelationGraph,
  typeFilter: string,
  showIsolated: boolean,
  hiddenTypes?: ReadonlySet<string>,
): FilteredRelationGraph {
  const edges = graph.edges.filter((edge) =>
    (!typeFilter || edge.type === typeFilter) && !hiddenTypes?.has(edge.type));
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

// 把世界坐标点移到视口中心，缩放至少到 minK。
export function centerTransform(
  point: LayoutPosition,
  width: number,
  height: number,
  currentK: number,
  minK = 1.15,
): ViewTransform {
  const k = Math.min(4, Math.max(currentK, minK));
  return { k, x: width / 2 - point.x * k, y: height / 2 - point.y * k };
}

// 智能标签：节点少时全显示；多时只显示度数靠前的一部分，避免文字糊成一片。
export function smartLabelIds(nodes: AgentRelationGraphNode[], limit = 40): Set<string> {
  if (nodes.length <= limit) return new Set(nodes.map((node) => node.userId));
  return new Set(
    [...nodes]
      .sort((a, b) => b.degree - a.degree || a.userId.localeCompare(b.userId))
      .slice(0, Math.max(12, Math.round(nodes.length * 0.25)))
      .map((node) => node.userId),
  );
}

function escapeXml(text: string): string {
  return text.replace(/[<>&"']/g, (char) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[char] as string));
}

// 生成独立 SVG（不依赖页面样式），用于导出 SVG/PNG。
export function buildExportSvg(
  nodes: AgentRelationGraphNode[],
  edges: AgentRelationItem[],
  positions: ReadonlyMap<string, LayoutPosition>,
  fillOf: (node: AgentRelationGraphNode) => string,
  title: string,
): { svg: string; width: number; height: number } {
  const points = nodes.map((node) => positions.get(node.userId)).filter(Boolean) as LayoutPosition[];
  const pad = 70;
  const minX = Math.min(...points.map((p) => p.x)) - pad;
  const minY = Math.min(...points.map((p) => p.y)) - pad - 24;
  const maxX = Math.max(...points.map((p) => p.x)) + pad;
  const maxY = Math.max(...points.map((p) => p.y)) + pad;
  const width = Math.round(maxX - minX);
  const height = Math.round(maxY - minY);
  const nodeById = new Map(nodes.map((node) => [node.userId, node]));
  const parts: string[] = [];
  for (const grouped of groupEdgesByEndpoint(edges)) {
    const { edge } = grouped;
    const from = positions.get(edge.subjectUserId);
    const to = positions.get(edge.objectUserId);
    if (!from || !to) continue;
    const geometry = edgePath(from.x - minX, from.y - minY, to.x - minX, to.y - minY, grouped.offset,
      nodeRadius(nodeById.get(edge.subjectUserId)?.degree ?? 1), nodeRadius(nodeById.get(edge.objectUserId)?.degree ?? 1));
    const color = relationTypeColor(edge.type);
    parts.push(`<path d="${geometry.path}" fill="none" stroke="${color}" stroke-width="${edgeWidth(edge.confidence).toFixed(2)}" stroke-linecap="round" opacity="0.85"${isDashedRelationType(edge.type) ? ' stroke-dasharray="6 6"' : ""}/>`);
    parts.push(`<polygon points="10,0 -7,5.5 -7,-5.5" fill="${color}" transform="translate(${geometry.end.x.toFixed(2)},${geometry.end.y.toFixed(2)}) rotate(${((geometry.endAngle * 180) / Math.PI).toFixed(2)})"/>`);
  }
  for (const node of nodes) {
    const pos = positions.get(node.userId);
    if (!pos) continue;
    const r = nodeRadius(node.degree);
    const name = nodeDisplayName(node, node.userId);
    const x = (pos.x - minX).toFixed(2);
    const y = (pos.y - minY).toFixed(2);
    parts.push(`<circle cx="${x}" cy="${y}" r="${r.toFixed(2)}" fill="${fillOf(node)}" stroke="#ffffff" stroke-width="2"/>`);
    parts.push(`<text x="${x}" y="${y}" text-anchor="middle" dominant-baseline="central" font-size="13" font-weight="700" fill="#ffffff">${escapeXml(name.slice(0, 1))}</text>`);
    parts.push(`<text x="${x}" y="${(pos.y - minY + r + 15).toFixed(2)}" text-anchor="middle" font-size="12" fill="#1f1d24" stroke="#ffffff" stroke-width="3" paint-order="stroke">${escapeXml(name)}</text>`);
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" font-family="'Nunito Variable','PingFang SC','Microsoft YaHei',sans-serif">`
    + `<rect width="100%" height="100%" fill="#fafafb"/>`
    + `<text x="20" y="30" font-size="15" font-weight="700" fill="#1f1d24">${escapeXml(title)}</text>`
    + parts.join("")
    + `</svg>`;
  return { svg, width, height };
}

// 节点基色：角色模式下低饱和的角色色，圈子模式下用社群色；未连接成员统一灰。
const ROLE_COLORS: Record<string, string> = {
  owner: "#e0a73f",
  admin: "#4fa98a",
  bot: "#6f8fd0",
  member: "#e4558a",
};
const UNLINKED_COLOR = "#c9c5cf";
const ROLE_LABEL: Record<string, string> = { owner: "群主", admin: "管理员", bot: "Bot", member: "成员" };

type ColorMode = "role" | "community";
type LabelMode = "smart" | "all" | "none";
type LayoutMode = "force" | "cluster" | "circle";

interface GraphSettings {
  colorMode: ColorMode;
  labelMode: LabelMode;
  edgeLabels: boolean;
  fadeStale: boolean;
  layout: LayoutMode;
  minimap: boolean;
}

const SETTINGS_KEY = "yawnbot.relations.graph";
const DEFAULT_SETTINGS: GraphSettings = {
  colorMode: "role",
  labelMode: "smart",
  edgeLabels: false,
  fadeStale: true,
  layout: "force",
  minimap: true,
};

function readSettings(): GraphSettings {
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    return raw ? { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<GraphSettings>) } : DEFAULT_SETTINGS;
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(settings: GraphSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // 本机偏好写不进去不影响使用。
  }
}

function triggerDownload(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const MINIMAP_W = 168;
const MINIMAP_H = 112;

interface RelationGraphViewProps {
  graph: AgentRelationGraph;
  typeFilter: string;
  readOnly?: boolean;
  /** 外部请求聚焦某成员（搜索、成员档案“在图谱中定位”），nonce 变化即触发。 */
  focusRequest?: { userId: string; nonce: number } | null;
  onOpenEdge?: (edge: AgentRelationItem) => void;
  onOpenMember?: (userId: string) => void;
  /** Shift 拖线或卡片“新增关系”时调用；object 为空表示只预填主体。 */
  onCreateBetween?: (subjectUserId: string, objectUserId?: string) => void;
}

export function RelationGraphView({
  graph,
  typeFilter,
  readOnly = false,
  focusRequest,
  onOpenEdge,
  onOpenMember,
  onCreateBetween,
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
  const [settings, setSettingsState] = useState<GraphSettings>(readSettings);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(() => new Set());
  const [egoHops, setEgoHops] = useState(0);
  const [pathTarget, setPathTarget] = useState<string | null>(null);
  const [searchText, setSearchText] = useState("");
  const [pathText, setPathText] = useState("");
  const [linking, setLinking] = useState<{ from: string; to: LayoutPosition } | null>(null);
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
  const linkingRef = useRef<string | null>(null);
  const clickWasDragRef = useRef(false);
  // 手动重排（清钉/解钉）时置位：模拟失效清理不再回写位置，否则会把刚清掉的固定加回去。
  const resetLayoutRef = useRef(false);
  const panRef = useRef<{ startClientX: number; startClientY: number; start: ViewTransform; moved: boolean } | null>(null);

  const setSettings = (patch: Partial<GraphSettings>) => {
    setSettingsState((current) => {
      const next = { ...current, ...patch };
      saveSettings(next);
      return next;
    });
  };

  const filtered = useMemo(
    () => filterGraphData(graph, typeFilter, showIsolated, hiddenTypes),
    [graph, typeFilter, showIsolated, hiddenTypes],
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
  const communities = useMemo(
    () => detectCommunities(filtered.nodes.map((node) => node.userId), filtered.edges),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layoutSignature],
  );
  const layout = useMemo(() => {
    void relayoutCounter; // 重新布局按钮递增此值触发重算
    const ids = filtered.nodes.map((node) => node.userId);
    if (settings.layout === "circle") {
      return circularLayout(ids, communities, new Map(filtered.nodes.map((node) => [node.userId, node.degree])));
    }
    return runRelationLayout(
      ids,
      filtered.edges.map((edge) => ({
        source: edge.subjectUserId,
        target: edge.objectUserId,
        confidence: edge.confidence,
      })),
      {
        pinned: pinnedRef.current,
        centers: settings.layout === "cluster" ? communityCenters(ids, communities) : undefined,
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature, relayoutCounter, settings.layout]);

  // 当前生效位置 = 布局结果 + 手动固定覆盖 + 实时模拟覆盖 + 正在拖拽的节点（大图退化路径）。
  const positions = new Map(layout);
  if (settings.layout !== "circle") for (const [id, pos] of pinnedRef.current) if (positions.has(id)) positions.set(id, pos);
  if (simPositions)
    for (const [id, pos] of simPositions) positions.set(id, pos);
  if (dragPos) positions.set(dragPos.id, dragPos.pos);
  // doFit 需要读取最新位置（含手动固定/拖拽中），用 ref 避免闭包过期且不触发重算。
  const positionsRef = useRef(positions);
  positionsRef.current = positions;
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const transformRef = useRef(transform);
  transformRef.current = transform;

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

  const centerOn = useCallback((userId: string) => {
    const pos = positionsRef.current.get(userId);
    if (!pos) return;
    applyTransformAnimated((prev) => centerTransform(pos, sizeRef.current.w, sizeRef.current.h, prev.k));
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

  // 外部聚焦请求：必要时打开“无边成员”，选中并居中。
  const pendingFocusRef = useRef<string | null>(null);
  useEffect(() => {
    if (!focusRequest) return;
    const node = nodeById.get(focusRequest.userId);
    if (!node) return;
    if (!filtered.nodes.some((item) => item.userId === node.userId)) setShowIsolated(true);
    setSelectedNodeId(node.userId);
    setSelectedEdgeId(null);
    pendingFocusRef.current = node.userId;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest?.nonce]);
  useEffect(() => {
    const target = pendingFocusRef.current;
    if (!target || size.w === 0 || !positionsRef.current.has(target)) return;
    pendingFocusRef.current = null;
    // 等 fit 动画起步后再居中，避免被布局变化的自动适配覆盖。
    const timer = window.setTimeout(() => centerOn(target), 60);
    return () => window.clearTimeout(timer);
  }, [layout, size.w, focusRequest?.nonce, centerOn]);

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
  }, [filtered.nodes.length === 0]);

  // 模拟版本 = 布局签名 + 重排计数：拓扑变化或手动重排后旧模拟即失效，
  // 先把沉降结果固化进 pinnedRef 再丢弃，避免清空覆盖层时画面跳回。
  const simVersion = `${layoutSignature}#${relayoutCounter}#${settings.layout}`;
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

  // 最大化期间锁定页面滚动。
  useEffect(() => {
    if (!maximized) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [maximized]);

  // 选中节点不在当前图里时（被过滤掉）清理相关状态。
  useEffect(() => {
    if (selectedNodeId && !filtered.nodes.some((node) => node.userId === selectedNodeId)) {
      setSelectedNodeId(null);
      setPathTarget(null);
      setEgoHops(0);
    }
  }, [filtered.nodes, selectedNodeId]);

  const liveSimEnabled = filtered.nodes.length <= LIVE_SIM_MAX_NODES && settings.layout !== "circle";

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

  const toWorld = (clientX: number, clientY: number): LayoutPosition => {
    const rect = svgRef.current?.getBoundingClientRect();
    const t = transformRef.current;
    return {
      x: (clientX - (rect?.left ?? 0) - t.x) / t.k,
      y: (clientY - (rect?.top ?? 0) - t.y) / t.k,
    };
  };

  const handleBackgroundPointerDown = (event: React.PointerEvent) => {
    panRef.current = {
      startClientX: event.clientX,
      startClientY: event.clientY,
      start: transform,
      moved: false,
    };
    (event.currentTarget as Element).setPointerCapture(event.pointerId);
  };

  const liveSimActive = (): boolean =>
    liveSimEnabled && simRef.current !== null && simVersionRef.current === simVersion;

  const handlePointerMove = (event: React.PointerEvent) => {
    if (linkingRef.current) {
      setLinking({ from: linkingRef.current, to: toWorld(event.clientX, event.clientY) });
      return;
    }
    const pan = panRef.current;
    if (pan && !dragRef.current) {
      if (Math.abs(event.clientX - pan.startClientX) + Math.abs(event.clientY - pan.startClientY) > 3) pan.moved = true;
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
        if (simNode) {
          const world = toWorld(event.clientX, event.clientY);
          simNode.fx = world.x;
          simNode.fy = world.y;
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

  const handlePointerUp = (event: React.PointerEvent) => {
    if (linkingRef.current) {
      const from = linkingRef.current;
      linkingRef.current = null;
      setLinking(null);
      // 指针被源节点捕获，目标节点要靠坐标反查。
      const target = (document.elementFromPoint(event.clientX, event.clientY) as Element | null)
        ?.closest("[data-node-id]")
        ?.getAttribute("data-node-id");
      if (target && target !== from) onCreateBetween?.(from, target);
      clickWasDragRef.current = true;
      return;
    }
    const pan = panRef.current;
    if (pan && !pan.moved && !dragRef.current) {
      // 点空白处：清掉选中，但保留邻域/路径设置以便下次选人继续用。
      setSelectedNodeId(null);
      setSelectedEdgeId(null);
      setPathTarget(null);
    }
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
    if (event.shiftKey && onCreateBetween && !readOnly) {
      linkingRef.current = id;
      setLinking({ from: id, to: pos });
      return;
    }
    if (readOnly || settings.layout === "circle") {
      dragRef.current = null;
      clickWasDragRef.current = false;
      return;
    }
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
    if (clickWasDragRef.current) {
      clickWasDragRef.current = false;
      return;
    }
    setSelectedNodeId((prev) => (prev === id ? null : id));
    setSelectedEdgeId(null);
    setPathTarget(null);
  };

  const handleNodeDoubleClick = (id: string) => {
    if (readOnly) {
      onOpenMember?.(id);
      return;
    }
    resetLayoutRef.current = true;
    pinnedRef.current.delete(id);
    setRelayoutCounter((counter) => counter + 1);
  };

  const handleEdgeClick = (edge: AgentRelationItem) => {
    setSelectedEdgeId(edge.id);
    setSelectedNodeId(null);
    onOpenEdge?.(edge);
  };

  const relayout = () => {
    resetLayoutRef.current = true;
    pinnedRef.current.clear();
    setRelayoutCounter((c) => c + 1);
  };

  const zoomBy = (factor: number) => applyTransformAnimated((t) => wheelZoom(t, factor, size.w / 2, size.h / 2));

  const onKeyDown = (event: React.KeyboardEvent) => {
    if ((event.target as HTMLElement).closest("input, textarea")) return;
    if (event.key === "f" || event.key === "F") doFit();
    else if (event.key === "+" || event.key === "=") zoomBy(1.25);
    else if (event.key === "-" || event.key === "_") zoomBy(1 / 1.25);
    else if ((event.key === "r" || event.key === "R") && !readOnly) relayout();
    else if (event.key === "Escape") {
      if (pathTarget) setPathTarget(null);
      else if (egoHops) setEgoHops(0);
      else if (selectedNodeId || selectedEdgeId) { setSelectedNodeId(null); setSelectedEdgeId(null); }
      else if (maximized) setMaximized(false);
      else return;
    } else return;
    event.preventDefault();
  };

  // ── 高亮计算：路径 > 邻域 > 悬停/选中 ──
  const path = useMemo(
    () => (selectedNodeId && pathTarget ? shortestPath(filtered.edges, selectedNodeId, pathTarget) : null),
    [filtered.edges, selectedNodeId, pathTarget],
  );
  const pathNodes = useMemo(() => new Set(path ?? []), [path]);
  const pathEdges = useMemo(() => pathEdgeIds(filtered.edges, path), [filtered.edges, path]);
  const egoSet = useMemo(
    () => (selectedNodeId && egoHops > 0 ? egoNetwork(filtered.edges, selectedNodeId, egoHops) : null),
    [filtered.edges, selectedNodeId, egoHops],
  );
  const focusNode = hoverNodeId ?? selectedNodeId;
  const focusEdge = hoverEdgeId ?? selectedEdgeId;
  const neighborIds = useMemo(() => {
    if (!focusNode) return null;
    const set = new Set([focusNode]);
    for (const edge of filtered.edges) {
      if (edge.subjectUserId === focusNode) set.add(edge.objectUserId);
      if (edge.objectUserId === focusNode) set.add(edge.subjectUserId);
    }
    return set;
  }, [filtered.edges, focusNode]);
  const pathActive = !!path && path.length > 1;
  const hasFocus = focusNode !== null || focusEdge !== null;
  const smartLabels = useMemo(() => smartLabelIds(filtered.nodes), [filtered.nodes]);
  const legendTypes = useMemo(() => {
    const counts = new Map<string, number>();
    for (const edge of graph.edges) {
      if (typeFilter && edge.type !== typeFilter) continue;
      counts.set(edge.type, (counts.get(edge.type) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-Hans-CN"));
  }, [graph.edges, typeFilter]);

  const nodeFill = (node: AgentRelationGraphNode): string => {
    if (!node.linked) return UNLINKED_COLOR;
    if (settings.colorMode === "community") return communityColor(communities.get(node.userId));
    return ROLE_COLORS[node.role] ?? ROLE_COLORS.member;
  };

  const edgeOpacity = (edge: AgentRelationItem): { opacity: number; active: boolean } => {
    if (pathActive) return pathEdges.has(edge.id) ? { opacity: 1, active: true } : { opacity: 0.06, active: false };
    const inEgo = !egoSet || (egoSet.has(edge.subjectUserId) && egoSet.has(edge.objectUserId));
    const active =
      (focusNode !== null && (edge.subjectUserId === focusNode || edge.objectUserId === focusNode)) ||
      edge.id === focusEdge;
    if (active && inEgo) return { opacity: 1, active: true };
    if (!inEgo) return { opacity: 0.05, active: false };
    if (hasFocus) return { opacity: 0.12, active: false };
    const stale = settings.fadeStale && freshnessTier(edge.lastSeenAt) === "stale";
    return { opacity: stale ? 0.28 : 0.78, active: false };
  };

  const nodeDimmed = (userId: string): boolean => {
    if (pathActive) return !pathNodes.has(userId);
    if (egoSet && !egoSet.has(userId)) return true;
    if (neighborIds && !neighborIds.has(userId)) return true;
    return false;
  };

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
    const { opacity, active } = edgeOpacity(edge);
    const width = edgeWidth(edge.confidence);
    const dashed = isDashedRelationType(edge.type);
    const showLabel = active || (settings.edgeLabels && opacity > 0.2);
    return (
      <g
        key={edge.id}
        className={`rg-edge${active ? " rg-edge-active" : ""}${dashed ? " rg-edge-dashed" : ""}${pathEdges.has(edge.id) ? " rg-edge-path" : ""}`}
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
          onPointerDown={(event) => event.stopPropagation()}
          onClick={() => handleEdgeClick(edge)}
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
    const dimmed = nodeDimmed(node.userId);
    const emphasised = selected || hoverNodeId === node.userId || pathNodes.has(node.userId) || (neighborIds?.has(node.userId) ?? false);
    const showLabel = settings.labelMode === "all"
      || (settings.labelMode === "smart" && (smartLabels.has(node.userId) || emphasised || (egoSet?.has(node.userId) ?? false)))
      || (settings.labelMode === "none" && (selected || hoverNodeId === node.userId));
    const pinned = settings.layout !== "circle" && pinnedRef.current.has(node.userId) && !simPositions;
    return (
      <g
        key={node.userId}
        data-node-id={node.userId}
        className={`rg-node${dimmed ? " is-dimmed" : ""}${selected ? " is-selected" : ""}${linking?.from === node.userId ? " is-linking" : ""}`}
        transform={`translate(${pos.x.toFixed(2)},${pos.y.toFixed(2)})`}
        style={{ "--i": Math.min(index, 24) } as React.CSSProperties}
        onPointerDown={(event) => handleNodePointerDown(event, node.userId)}
        onPointerEnter={() => setHoverNodeId(node.userId)}
        onPointerLeave={() => setHoverNodeId(null)}
        onClick={() => handleNodeClick(node.userId)}
        onDoubleClick={(event) => { event.stopPropagation(); handleNodeDoubleClick(node.userId); }}
        role="button"
        aria-label={`${name}，${node.degree} 条关系`}
      >
        {selected && <circle className="rg-node-select-ring" r={radius + 6} fill="none" />}
        {pathNodes.has(node.userId) && !selected && <circle className="rg-node-path-ring" r={radius + 5} fill="none" />}
        <g className="rg-node-core">
          <circle
            r={radius}
            fill={nodeFill(node)}
            stroke="#ffffff"
            strokeWidth={2}
          />
          <text textAnchor="middle" dominantBaseline="central" className="rg-node-initial" pointerEvents="none">
            {name.slice(0, 1)}
          </text>
        </g>
        {pinned && <circle className="rg-node-pin" cx={radius * 0.72} cy={-radius * 0.72} r={3.2} />}
        {showLabel && (
          <text y={radius + 15} textAnchor="middle" className={`rg-node-label${emphasised ? " is-strong" : ""}`} pointerEvents="none">
            {name}
          </text>
        )}
      </g>
    );
  };

  const exportImage = async (format: "svg" | "png") => {
    const { svg, width, height } = buildExportSvg(
      filtered.nodes,
      filtered.edges,
      positions,
      nodeFill,
      `人物关系图谱 · ${filtered.nodes.length} 人 · ${filtered.edges.length} 条关系`,
    );
    const stamp = new Date().toISOString().slice(0, 10);
    if (format === "svg") {
      triggerDownload(`relation-graph-${stamp}.svg`, new Blob([svg], { type: "image/svg+xml" }));
      return;
    }
    const image = new Image();
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("图片生成失败"));
      image.src = url;
    });
    const scale = 2;
    const canvas = document.createElement("canvas");
    canvas.width = width * scale;
    canvas.height = height * scale;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.scale(scale, scale);
    context.drawImage(image, 0, 0);
    URL.revokeObjectURL(url);
    canvas.toBlob((blob) => { if (blob) triggerDownload(`relation-graph-${stamp}.png`, blob); }, "image/png");
  };

  // ── 小地图 ──
  const minimap = useMemo(() => {
    const points = [...positions.values()];
    if (points.length === 0) return null;
    const minX = Math.min(...points.map((p) => p.x)) - 30;
    const minY = Math.min(...points.map((p) => p.y)) - 30;
    const maxX = Math.max(...points.map((p) => p.x)) + 30;
    const maxY = Math.max(...points.map((p) => p.y)) + 30;
    const scale = Math.min(MINIMAP_W / (maxX - minX), MINIMAP_H / (maxY - minY));
    const offsetX = (MINIMAP_W - (maxX - minX) * scale) / 2;
    const offsetY = (MINIMAP_H - (maxY - minY) * scale) / 2;
    return {
      project: (p: LayoutPosition) => ({ x: offsetX + (p.x - minX) * scale, y: offsetY + (p.y - minY) * scale }),
      unproject: (x: number, y: number) => ({ x: (x - offsetX) / scale + minX, y: (y - offsetY) / scale + minY }),
      scale,
    };
    // positions 每次渲染都是新 Map；按布局/模拟/拖拽变化重算即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, simPositions, dragPos]);

  const minimapPan = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!minimap || (event.type === "pointermove" && event.buttons !== 1)) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const world = minimap.unproject(event.clientX - rect.left, event.clientY - rect.top);
    setSmoothTransform(false);
    setTransform((prev) => ({ ...prev, x: size.w / 2 - world.x * prev.k, y: size.h / 2 - world.y * prev.k }));
  };

  const selectedNode = selectedNodeId ? (nodeById.get(selectedNodeId) ?? null) : null;
  const selectedStats = useMemo(() => {
    if (!selectedNodeId) return null;
    const list = filtered.edges.filter((edge) => edge.subjectUserId === selectedNodeId || edge.objectUserId === selectedNodeId);
    const neighbors = new Set(list.map((edge) => (edge.subjectUserId === selectedNodeId ? edge.objectUserId : edge.subjectUserId)));
    return { edges: list.length, neighbors: neighbors.size, out: list.filter((edge) => edge.subjectUserId === selectedNodeId).length };
  }, [filtered.edges, selectedNodeId]);

  const nameOf = (userId: string) => nodeDisplayName(nodeById.get(userId), userId);
  const memberOptions = (keyword: string, exclude?: string | null) => {
    const term = keyword.trim().toLowerCase();
    return filtered.nodes
      .filter((node) => node.userId !== exclude)
      .filter((node) => !term || nodeDisplayName(node, node.userId).toLowerCase().includes(term) || node.userId.includes(term))
      .sort((a, b) => b.degree - a.degree)
      .slice(0, 12)
      .map((node) => ({
        value: node.userId,
        label: <span className="rg-option"><span className="rg-option-dot" style={{ background: nodeFill(node) }} />{nodeDisplayName(node, node.userId)}<span className="rg-option-meta">{node.degree} 条</span></span>,
      }));
  };

  // 悬停提示卡：定位在节点/边中点的屏幕坐标上方。
  const hoverCard = (() => {
    if (dragRef.current || linking || panRef.current) return null;
    if (hoverNodeId) {
      const node = nodeById.get(hoverNodeId);
      const pos = positions.get(hoverNodeId);
      if (!node || !pos) return null;
      const r = nodeRadius(node.degree) * transform.k;
      return {
        x: pos.x * transform.k + transform.x,
        y: pos.y * transform.k + transform.y - r - 10,
        content: <>
          <strong>{nameOf(node.userId)}</strong>
          <span>{ROLE_LABEL[node.role] ?? "成员"} · {node.linked ? `${node.degree} 条关系` : "暂无关系"}</span>
        </>,
      };
    }
    if (hoverEdgeId) {
      const edge = filtered.edges.find((item) => item.id === hoverEdgeId);
      const from = edge && positions.get(edge.subjectUserId);
      const to = edge && positions.get(edge.objectUserId);
      if (!edge || !from || !to) return null;
      return {
        x: ((from.x + to.x) / 2) * transform.k + transform.x,
        y: ((from.y + to.y) / 2) * transform.k + transform.y - 14,
        content: <>
          <strong>{relationSentence(nameOf(edge.subjectUserId), edge.type, nameOf(edge.objectUserId))}</strong>
          <span>置信 {Math.round(edge.confidence * 100)}% · {relativeTime(edge.lastSeenAt)}{edge.note ? ` · ${edge.note}` : ""}</span>
        </>,
      };
    }
    return null;
  })();

  if (graph.nodes.length === 0 || (filtered.nodes.length === 0 && hiddenTypes.size === 0)) {
    return (
      <Empty
        description={graph.edges.length === 0 ? "暂无关系记忆" : "当前过滤条件下暂无关系"}
        style={{ padding: "48px 0" }}
      />
    );
  }

  const settingsContent = (
    <div className="rg-settings">
      <div className="rg-settings-row"><span>布局</span>
        <Segmented size="small" value={settings.layout} onChange={(value) => { resetLayoutRef.current = true; pinnedRef.current.clear(); setSettings({ layout: value as LayoutMode }); }}
          options={[{ value: "force", label: "力导向" }, { value: "cluster", label: "按圈子" }, { value: "circle", label: "环形" }]} />
      </div>
      <div className="rg-settings-row"><span>节点颜色</span>
        <Segmented size="small" value={settings.colorMode} onChange={(value) => setSettings({ colorMode: value as ColorMode })}
          options={[{ value: "role", label: "按角色" }, { value: "community", label: "按圈子" }]} />
      </div>
      <div className="rg-settings-row"><span>成员名</span>
        <Segmented size="small" value={settings.labelMode} onChange={(value) => setSettings({ labelMode: value as LabelMode })}
          options={[{ value: "smart", label: "智能" }, { value: "all", label: "全部" }, { value: "none", label: "隐藏" }]} />
      </div>
      <label className="rg-settings-row"><span>常显关系类型</span><Switch size="small" checked={settings.edgeLabels} onChange={(value) => setSettings({ edgeLabels: value })} /></label>
      <label className="rg-settings-row"><span>淡化 60 天未见的关系</span><Switch size="small" checked={settings.fadeStale} onChange={(value) => setSettings({ fadeStale: value })} /></label>
      <label className="rg-settings-row"><span>显示无关系成员</span><Switch size="small" checked={showIsolated} onChange={setShowIsolated} /></label>
      <label className="rg-settings-row"><span>小地图</span><Switch size="small" checked={settings.minimap} onChange={(value) => setSettings({ minimap: value })} /></label>
    </div>
  );

  const helpContent = (
    <ul className="rg-help">
      <li>点成员：选中并显示信息卡；再点空白取消</li>
      <li>点关系线：打开关系详情</li>
      {!readOnly && <li>拖动成员：牵动相邻成员并固定位置；双击解除固定</li>}
      {!readOnly && onCreateBetween && <li>按住 Shift 从一人拖到另一人：新建关系</li>}
      <li>滚轮缩放 · 拖动空白平移 · 小地图可点击跳转</li>
      <li>快捷键：F 适配全图，+ / − 缩放{readOnly ? "" : "，R 重新布局"}，Esc 逐级退出</li>
    </ul>
  );

  return (
    <>
      {maximized && <div className="rg-maximize-backdrop" onClick={() => setMaximized(false)} />}
      <div
        className={maximized ? "rg-wrap rg-maximized" : "rg-wrap"}
        ref={containerRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
        aria-label="人物关系图谱，可用 F 适配、加减号缩放"
      >
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
          className={`rg-svg${linking ? " is-linking" : ""}`}
          width={size.w || 900}
          height={size.h || 620}
          onPointerDown={handleBackgroundPointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
          onDoubleClick={doFit}
        >
          <g
            className={smoothTransform ? "rg-viewport rg-viewport-anim" : "rg-viewport"}
            style={{
              transform: `translate(${transform.x.toFixed(2)}px, ${transform.y.toFixed(2)}px) scale(${transform.k.toFixed(4)})`,
            }}
          >
            {groupedEdges.map(renderEdge)}
            {linking && positions.get(linking.from) && (
              <line
                className="rg-link-draft"
                x1={positions.get(linking.from)!.x}
                y1={positions.get(linking.from)!.y}
                x2={linking.to.x}
                y2={linking.to.y}
              />
            )}
            {filtered.nodes.map((node, index) => renderNode(node, index))}
          </g>
        </svg>
        {size.w === 0 && <Spin className="rg-loading" />}
        {filtered.nodes.length === 0 && <div className="rg-empty-overlay">图例里的类型都被关掉了</div>}

        <div className="rg-toolbar">
          <AutoComplete
            className="rg-search"
            size="small"
            value={searchText}
            options={memberOptions(searchText)}
            onChange={setSearchText}
            onSelect={(value: string) => {
              setSearchText("");
              setSelectedNodeId(value);
              setSelectedEdgeId(null);
              centerOn(value);
            }}
            placeholder="定位成员"
            aria-label="在图谱中定位成员"
          />
          <Space size={2} className="rg-toolbar-buttons">
            <Popover content={settingsContent} trigger="click" placement="bottomRight" title="显示设置">
              <Tooltip title="显示设置"><Button size="small" type="text" icon={<SettingOutlined />} aria-label="显示设置" /></Tooltip>
            </Popover>
            {!readOnly && settings.layout !== "circle" && (
              <Tooltip title="重新自动布局（清除手动固定，R）">
                <Button size="small" type="text" icon={<ReloadOutlined />} aria-label="重新布局" onClick={relayout} />
              </Tooltip>
            )}
            <Tooltip title="适配全图（F）">
              <Button size="small" type="text" icon={<AimOutlined />} aria-label="适配全图" onClick={doFit} />
            </Tooltip>
            <Tooltip title="缩小（−）">
              <Button size="small" type="text" icon={<ZoomOutOutlined />} aria-label="缩小" onClick={() => zoomBy(1 / 1.25)} />
            </Tooltip>
            <Tooltip title="点击恢复 100%">
              <button type="button" className="rg-zoom-label" onClick={() => applyTransformAnimated((t) => wheelZoom(t, 1 / t.k, size.w / 2, size.h / 2))}>
                {Math.round(transform.k * 100)}%
              </button>
            </Tooltip>
            <Tooltip title="放大（+）">
              <Button size="small" type="text" icon={<ZoomInOutlined />} aria-label="放大" onClick={() => zoomBy(1.25)} />
            </Tooltip>
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [{ key: "png", label: "导出 PNG 图片" }, { key: "svg", label: "导出 SVG 矢量图" }],
                onClick: ({ key }) => { void exportImage(key as "png" | "svg"); },
              }}
            >
              <Tooltip title="导出图片"><Button size="small" type="text" icon={<DownloadOutlined />} aria-label="导出图片" /></Tooltip>
            </Dropdown>
            <Popover content={helpContent} trigger="click" placement="bottomRight" title="操作说明">
              <Tooltip title="操作说明"><Button size="small" type="text" icon={<QuestionCircleOutlined />} aria-label="操作说明" /></Tooltip>
            </Popover>
            <Tooltip title={maximized ? "还原（Esc）" : "最大化查看"}>
              <Button
                size="small"
                type="text"
                icon={maximized ? <CompressOutlined /> : <ExpandOutlined />}
                aria-label={maximized ? "还原" : "最大化"}
                onClick={() => setMaximized((value) => !value)}
              />
            </Tooltip>
          </Space>
        </div>

        {selectedNode && selectedStats && (
          <div className="rg-card" role="dialog" aria-label={`${nameOf(selectedNode.userId)} 的信息卡`}>
            <div className="rg-card-head">
              <span className="rg-card-avatar" style={{ background: nodeFill(selectedNode) }}>{nameOf(selectedNode.userId).slice(0, 1)}</span>
              <div className="rg-card-title">
                <strong>{nameOf(selectedNode.userId)}</strong>
                <Text type="secondary" className="rg-card-sub">{ROLE_LABEL[selectedNode.role] ?? "成员"} · {selectedNode.userId}</Text>
              </div>
              <button type="button" className="rg-card-close" aria-label="关闭信息卡" onClick={() => { setSelectedNodeId(null); setPathTarget(null); }}>×</button>
            </div>
            <div className="rg-card-stats">
              <span><strong>{selectedStats.edges}</strong>关系</span>
              <span><strong>{selectedStats.neighbors}</strong>认识</span>
              <span><strong>{selectedStats.out}</strong>主动</span>
              {settings.colorMode === "community" && communities.has(selectedNode.userId) && (
                <span><strong style={{ color: communityColor(communities.get(selectedNode.userId)) }}>{(communities.get(selectedNode.userId) ?? 0) + 1}</strong>圈子</span>
              )}
            </div>
            <div className="rg-card-row">
              <span className="rg-card-label">邻域</span>
              <Segmented size="small" value={egoHops} onChange={(value) => setEgoHops(Number(value))}
                options={[{ value: 0, label: "全图" }, { value: 1, label: "1 跳" }, { value: 2, label: "2 跳" }]} />
            </div>
            <div className="rg-card-row">
              <span className="rg-card-label">路径</span>
              <AutoComplete
                size="small"
                className="rg-card-path"
                value={pathText}
                options={memberOptions(pathText, selectedNode.userId)}
                onChange={setPathText}
                onSelect={(value: string) => { setPathText(""); setPathTarget(value); }}
                placeholder="到某人的最短关系链"
              />
            </div>
            {pathTarget && (
              <div className="rg-card-path-result" aria-live="polite">
                {path ? (
                  <>
                    <Text type="secondary" className="rg-card-sub">{path.length - 1} 步关系链</Text>
                    <div className="rg-path-chain">
                      {path.map((id, index) => (
                        <span key={id} className="rg-path-step">
                          {index > 0 && <span className="rg-path-arrow">›</span>}
                          <button type="button" onClick={() => centerOn(id)}>{nameOf(id)}</button>
                        </span>
                      ))}
                    </div>
                  </>
                ) : <Text type="secondary" className="rg-card-sub">{nameOf(selectedNode.userId)} 和 {nameOf(pathTarget)} 之间没有关系链</Text>}
                <button type="button" className="rg-link-btn" onClick={() => setPathTarget(null)}>清除路径</button>
              </div>
            )}
            <div className="rg-card-actions">
              {onOpenMember && <Button size="small" onClick={() => onOpenMember(selectedNode.userId)}>关系档案</Button>}
              <Button size="small" onClick={() => centerOn(selectedNode.userId)}>居中</Button>
              {onCreateBetween && !readOnly && (
                <Button size="small" type="primary" onClick={() => onCreateBetween(selectedNode.userId, pathTarget ?? undefined)}>新增关系</Button>
              )}
            </div>
          </div>
        )}

        {hoverCard && size.w > 0 && (
          <div className="rg-tip" style={{ left: Math.min(Math.max(hoverCard.x, 90), size.w - 90), top: Math.max(hoverCard.y, 8) }} role="tooltip">
            {hoverCard.content}
          </div>
        )}

        <div className="rg-legend" aria-label="关系类型图例（点击开关）">
          {legendTypes.map(([type, count]) => {
            const color = relationTypeColor(type);
            const hidden = hiddenTypes.has(type);
            return (
              <Tooltip key={type} title={hidden ? "点击显示" : "点击隐藏，双击只看这一类"}>
                <button
                  type="button"
                  className={`rg-legend-item${hidden ? " is-off" : ""}`}
                  aria-pressed={!hidden}
                  onClick={() => setHiddenTypes((current) => {
                    const next = new Set(current);
                    if (next.has(type)) next.delete(type);
                    else next.add(type);
                    return next;
                  })}
                  onDoubleClick={() => setHiddenTypes(new Set(legendTypes.map(([item]) => item).filter((item) => item !== type)))}
                >
                  <span className={`rg-legend-dot${isDashedRelationType(type) ? " is-dashed" : ""}`} style={{ background: color }} />
                  {type}
                  <span className="rg-legend-count">{count}</span>
                </button>
              </Tooltip>
            );
          })}
          {hiddenTypes.size > 0 && (
            <button type="button" className="rg-legend-reset" onClick={() => setHiddenTypes(new Set())}>全部显示</button>
          )}
        </div>

        <div className="rg-status" aria-live="polite" hidden={!!selectedNode}>
          {filtered.nodes.length} 人 · {filtered.edges.length} 条关系
          {egoHops > 0 && selectedNode ? ` · ${nameOf(selectedNode.userId)} 的 ${egoHops} 跳邻域 ${egoSet?.size ?? 0} 人` : ""}
          {settings.colorMode === "community" ? ` · ${new Set(communities.values()).size} 个圈子` : ""}
        </div>

        {settings.minimap && minimap && size.w > 0 && (
          <svg
            className="rg-minimap"
            width={MINIMAP_W}
            height={MINIMAP_H}
            onPointerDown={(event) => { event.stopPropagation(); event.currentTarget.setPointerCapture(event.pointerId); minimapPan(event); }}
            onPointerMove={minimapPan}
            aria-label="小地图，点击跳转"
          >
            {groupedEdges.length < 1500 && groupedEdges.map(({ edge }) => {
              const a = positions.get(edge.subjectUserId);
              const b = positions.get(edge.objectUserId);
              if (!a || !b) return null;
              const pa = minimap.project(a);
              const pb = minimap.project(b);
              return <line key={edge.id} x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y} className="rg-minimap-edge" />;
            })}
            {filtered.nodes.map((node) => {
              const pos = positions.get(node.userId);
              if (!pos) return null;
              const p = minimap.project(pos);
              return <circle key={node.userId} cx={p.x} cy={p.y} r={node.userId === selectedNodeId ? 3.6 : 2.2} fill={nodeFill(node)} />;
            })}
            {(() => {
              const topLeft = minimap.project({ x: -transform.x / transform.k, y: -transform.y / transform.k });
              return (
                <rect
                  className="rg-minimap-view"
                  x={topLeft.x}
                  y={topLeft.y}
                  width={(size.w / transform.k) * minimap.scale}
                  height={(size.h / transform.k) * minimap.scale}
                />
              );
            })()}
          </svg>
        )}
      </div>
    </>
  );
}
