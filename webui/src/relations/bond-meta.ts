// Bot 视角关系的纯展示规则：阶段配色、好感/信任措辞、锁定字段名。与后端 bot_bond.py 同口径。
import type { AgentBondItem, AgentBondStage } from "../types";

export const BOND_STAGE_ORDER: AgentBondStage[] = ["close", "friend", "familiar", "acquaintance", "stranger", "strained"];

export const BOND_STAGE_META: Record<AgentBondStage, { label: string; color: string; hint: string }> = {
  close: { label: "亲近", color: "#e4558a", hint: "熟悉度高、好感明显、认识至少 5 天" },
  friend: { label: "朋友", color: "#f08a5d", hint: "经常来往且态度友好" },
  familiar: { label: "熟人", color: "#4f9dde", hint: "有稳定来往" },
  acquaintance: { label: "点头之交", color: "#8aa1b4", hint: "偶尔说过几句" },
  stranger: { label: "陌生", color: "#b8c0c8", hint: "几乎没有直接来往" },
  strained: { label: "有点僵", color: "#8c6bb1", hint: "近期互动里有明显不满" },
};

export const BOND_FIELD_LABELS: Record<string, string> = {
  label: "关系标签",
  memberCallsBot: "他怎么叫 Bot",
  botCallsMember: "Bot 怎么叫他",
  tone: "说话分寸",
  note: "背景",
  insideJokes: "梗",
  boundaries: "边界",
  affinity: "好感",
  trust: "信任",
};

// 前端字段名 ↔ 后端 locked_fields 名。
export const BOND_LOCK_KEYS: Record<string, string> = {
  label: "label",
  memberCallsBot: "member_calls_bot",
  botCallsMember: "bot_calls_member",
  tone: "tone",
  note: "note",
  insideJokes: "inside_jokes",
  boundaries: "boundaries",
  affinity: "affinity",
  trust: "trust",
};

export function stageMeta(stage: string): { label: string; color: string; hint: string } {
  return BOND_STAGE_META[stage as AgentBondStage] ?? BOND_STAGE_META.stranger;
}

export function affinityWord(value: number | null | undefined): string {
  const score = Number(value ?? 0);
  if (score >= 0.5) return "很亲近";
  if (score >= 0.15) return "友好";
  if (score <= -0.35) return "有过不愉快";
  if (score <= -0.1) return "略有不满";
  return "中性";
}

export function trustWord(value: number | null | undefined): string {
  const score = Number(value ?? 0.3);
  if (score >= 0.7) return "很信任";
  if (score >= 0.45) return "比较信任";
  if (score <= 0.15) return "戒备";
  return "一般";
}

export function isLocked(bond: Pick<AgentBondItem, "lockedFields">, field: string): boolean {
  return (bond.lockedFields ?? []).includes(BOND_LOCK_KEYS[field] ?? field);
}

/** 一句话描述关系，用在卡片副标题和导出里。 */
export function bondSentence(bond: AgentBondItem): string {
  const parts = [stageMeta(bond.stage).label];
  if (bond.label) parts.push(bond.label);
  if (bond.knownDays !== undefined && bond.knownDays !== null) parts.push(`认识 ${bond.knownDays} 天`);
  if (bond.activeDays) parts.push(`来往 ${bond.activeDays} 天`);
  return parts.join(" · ");
}

export function interactionTotal(bond: AgentBondItem): number {
  return (bond.memberToBotCount ?? 0) + (bond.botToMemberCount ?? 0);
}

/** 他主动找 Bot 的占比：越高越是“他来找我”，越低越是“我在搭理他”。 */
export function initiativeRatio(bond: AgentBondItem): number | null {
  const total = interactionTotal(bond);
  if (!total) return null;
  return (bond.memberToBotCount ?? 0) / total;
}
