// 好感玩法的纯逻辑：权益文案、任务进度、策略校验与比较。不依赖 React，便于单测。
import type { AffinityPolicy, LevelPerksPolicy, PerkBonus, PerksPublic, QuestsView } from "./types";

export function bonusText(bonus: PerkBonus): string {
  return `${bonus.label} +${bonus.value}${bonus.unit === "%" ? "%" : ""}`;
}

/** 一级权益的一行概述：“私聊每天能聊的轮数 +25%；送礼 5 次/天；商店 95 折；解锁 摸头、抱抱” */
export function perkSummary(perks: PerksPublic): string {
  const parts = perks.bonuses.slice(0, 3).map(bonusText);
  parts.push(`送礼 ${perks.gift_daily} 次/天`);
  if (perks.shop_discount_pct > 0) parts.push(`商店 ${discountLabel(perks.shop_discount_pct)}`);
  if (perks.unlocks.length) parts.push(`解锁 ${perks.unlocks.map((u) => u.label).join("、")}`);
  return parts.join("；");
}

/** 折扣百分比 → 中文折数：5 → “95 折”，10 → “9 折”，0 → “原价” */
export function discountLabel(pct: number): string {
  if (pct <= 0) return "原价";
  const rate = (100 - pct) / 10;
  return `${Number.isInteger(rate) ? rate : rate.toFixed(1).replace(/\.0$/, "")} 折`.replace(/^(\d)\.(\d) 折$/, "$1$2 折");
}

export function questDone(view: QuestsView): number {
  return view.items.filter((item) => item.done).length;
}

export function questPercent(view: QuestsView): number {
  if (!view.items.length) return 0;
  const total = view.items.reduce((sum, item) => sum + Math.min(item.progress / Math.max(item.target, 1), 1), 0);
  return Math.round((total / view.items.length) * 100);
}

export function unclaimed(view: QuestsView): number {
  return view.items.filter((item) => item.done && !item.claimed).length;
}

export const LEVEL_NUMBER_FIELDS: Array<{ key: keyof LevelPerksPolicy; label: string; max: number; suffix: string }> = [
  { key: "private_turns_pct", label: "私聊轮数", max: 500, suffix: "%" },
  { key: "private_web_bonus", label: "私聊联网", max: 200, suffix: "次" },
  { key: "private_sticker_bonus", label: "私聊表情包", max: 200, suffix: "条" },
  { key: "web_search_bonus", label: "群搜索", max: 500, suffix: "次" },
  { key: "sandbox_bonus", label: "沙箱/虚拟电脑", max: 500, suffix: "次" },
  { key: "sticker_collect_bonus", label: "收藏表情", max: 200, suffix: "张" },
  { key: "persona_edit_bonus", label: "改人设", max: 100, suffix: "次" },
  { key: "memory_bonus", label: "记忆每类", max: 30, suffix: "样" },
  { key: "checkin_points_pct", label: "签到积分", max: 300, suffix: "%" },
  { key: "gift_daily", label: "每天送礼", max: 100, suffix: "次" },
  { key: "shop_discount_pct", label: "商店折扣", max: 90, suffix: "%" },
];

/** 前端预校验（后端还会再校验一次）；返回第一条问题，没问题返回 null。 */
export function validatePolicy(policy: AffinityPolicy): string | null {
  if (policy.levels.length !== 6) return "等级数量不对，请重置后再改";
  for (const level of policy.levels) {
    for (const field of LEVEL_NUMBER_FIELDS) {
      const value = Number(level[field.key]);
      if (!Number.isFinite(value) || value < 0 || value > field.max) return `「${level.level}」的${field.label}要在 0~${field.max} 之间`;
    }
    if (level.tone.length > 80) return `「${level.level}」的相处提示太长（最多 80 字）`;
    if (level.behavior.length > 120) return `「${level.level}」的分寸说明太长（最多 120 字）`;
  }
  const keys = new Set<string>();
  for (const gift of policy.gifts) {
    if (!/^[a-z0-9_]+$/.test(gift.key)) return `礼物标识「${gift.key}」只能用小写字母、数字和下划线`;
    if (keys.has(gift.key)) return `礼物标识「${gift.key}」重复了`;
    keys.add(gift.key);
    if (!gift.name.trim()) return "有礼物没写名字";
    if (gift.affinity < 1 || gift.affinity > 100) return `「${gift.name}」的好感要在 1~100 之间`;
  }
  if (policy.decay.idle_days < 3) return "衰减至少要 3 天没来往才开始";
  if (policy.care.give_up_days <= policy.care.idle_days) return "久别问候的“放弃天数”要大于“久别天数”";
  return null;
}

export function policyEqual(a: AffinityPolicy | null, b: AffinityPolicy | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function toggleUnlock(level: LevelPerksPolicy, key: string, on: boolean): LevelPerksPolicy {
  const unlocks = on ? Array.from(new Set([...level.unlocks, key])) : level.unlocks.filter((item) => item !== key);
  return { ...level, unlocks };
}

export function itemsText(items: Record<string, number>, names: Record<string, string>): string {
  const parts = Object.entries(items).filter(([, n]) => n > 0).map(([key, n]) => `${names[key] ?? key}×${n}`);
  return parts.length ? parts.join("、") : "—";
}

export const ITEM_NAMES: Record<string, string> = {
  repair_card: "补签卡",
  shield: "好感护盾",
  lucky_card: "签到暴击卡",
  quest_refresh: "任务刷新券",
};
