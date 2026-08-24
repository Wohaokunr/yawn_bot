import type { AgentRelationGraphNode } from "./types";

export const RELATION_TYPE_GRAPH_COLORS: Record<string, string> = {
  好友: "#4caf7d",
  死党: "#2fa2a0",
  情侣: "#f2608d",
  伴侣: "#d94f9e",
  亲属: "#8f63d2",
  师徒: "#e8873a",
  同事: "#4b7fd1",
  同学: "#55a3d9",
  搭子: "#b98cd6",
  对立: "#e0524f",
  mentions: "#b3a4ad",
};

const RELATION_FALLBACK_COLOR = "#9c8f96";

export function relationTypeColor(type: string): string {
  return RELATION_TYPE_GRAPH_COLORS[type] ?? RELATION_FALLBACK_COLOR;
}

export function isDashedRelationType(type: string): boolean {
  return type === "mentions";
}

export function nodeDisplayName(
  node: AgentRelationGraphNode | undefined,
  userId: string,
): string {
  if (!node) return userId;
  return (node.groupNickname || node.nickname || "").trim() || userId;
}
