import type { AgentRelationItem } from "../types";
import type { RelationGraphState } from "./use-relation-graph";

export type RelationsView = "table" | "graph" | "members" | "matrix" | "insights";

export const RELATION_VIEWS: Array<{ key: RelationsView; label: string; hint: string }> = [
  { key: "table", label: "列表", hint: "分页表格，支持排序、筛选与批量操作" },
  { key: "graph", label: "图谱", hint: "力导向关系网，支持搜索定位、路径与邻域" },
  { key: "members", label: "成员", hint: "以人为中心的关系卡片" },
  { key: "matrix", label: "矩阵", hint: "核心成员两两关系热力图" },
  { key: "insights", label: "洞察", hint: "健康分、社群、关键人物与待复核队列" },
];

export interface RelationFilters {
  type: string;
  source: string;
  member: string;
  search: string;
  minConfidence: number | null;
  maxConfidence: number | null;
}

export const EMPTY_FILTERS: RelationFilters = {
  type: "",
  source: "",
  member: "",
  search: "",
  minConfidence: null,
  maxConfidence: null,
};

export interface RelationCreatePrefill {
  subjectUserId?: string;
  objectUserId?: string;
  type?: string;
  note?: string;
  confidence?: number;
}

// 各视图共享的面板能力；视图本身只关心展示，写操作统一回到面板外壳处理。
export interface RelationsContext {
  groupId: string;
  readOnly: boolean;
  /** readOnly 或数据正在刷新时为 true，写按钮应禁用。 */
  locked: boolean;
  graph: RelationGraphState;
  filters: RelationFilters;
  nameOf: (userId: string) => string;
  patchFilters: (patch: Partial<RelationFilters>) => void;
  openEdge: (edge: AgentRelationItem) => void;
  openMember: (userId: string) => void;
  openCreate: (prefill?: RelationCreatePrefill) => void;
  openEdit: (edge: AgentRelationItem) => void;
  removeEdges: (ids: string[]) => Promise<boolean>;
  createReverse: (edges: AgentRelationItem[]) => Promise<void>;
  focusInGraph: (userId: string) => void;
  setView: (view: RelationsView) => void;
}

export function activeFilterCount(filters: RelationFilters): number {
  return [
    filters.type,
    filters.source,
    filters.member,
    filters.search,
    filters.minConfidence !== null || filters.maxConfidence !== null ? "conf" : "",
  ].filter(Boolean).length;
}
