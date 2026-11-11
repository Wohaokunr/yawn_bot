// 好感玩法的纯逻辑：权益文案、任务进度、策略校验与比较。不依赖 React，便于单测。
import type { AffinityPolicy, BondBadge, BondSettle, LevelPerksPolicy, PerkBonus, PerksPublic, QuestsView, WeeklyView } from "./types";

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
  { key: "private_task_bonus", label: "后台小助手", max: 50, suffix: "件/天" },
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
    if ((level.title ?? "").length > 12) return `「${level.level}」的关系称号太长（最多 12 字）`;
    if ((level.speech_style ?? "").length > 120) return `「${level.level}」的说话方式太长（最多 120 字）`;
    if (!Number.isInteger(level.initiative) || level.initiative < 0 || level.initiative > 3) return `「${level.level}」的主动程度要在 0~3 之间`;
  }
  const keys = new Set<string>();
  for (const gift of policy.gifts) {
    if (!/^[a-z0-9_]+$/.test(gift.key)) return `礼物标识「${gift.key}」只能用小写字母、数字和下划线`;
    if (keys.has(gift.key)) return `礼物标识「${gift.key}」重复了`;
    keys.add(gift.key);
    if (!gift.name.trim()) return "有礼物没写名字";
    if (gift.affinity < 1 || gift.affinity > 100) return `「${gift.name}」的好感要在 1~100 之间`;
  }
  for (const gift of policy.gifts) {
    if (gift.min_level && !policy.levels.some((level) => level.level === gift.min_level)) return `「${gift.name}」的解锁等级「${gift.min_level}」不存在`;
  }
  for (const day of Object.keys(policy.streak?.milestones ?? {})) {
    const n = Number(day);
    if (!Number.isInteger(n) || n < 2 || n > 1000) return `连续来往的里程碑「${day}」要是 2~1000 的整数天`;
  }
  for (const day of Object.keys(policy.anniversary?.items ?? {})) {
    const n = Number(day);
    if (!Number.isInteger(n) || n < 1 || n > 3650) return `纪念日「${day}」要是 1~3650 的整数天`;
  }
  if (policy.moments && (policy.moments.daily_up > 20 || policy.moments.daily_down > 20)) return "心动瞬间每天最多 20 点";
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
  mood_candy: "心情糖",
  mystery_box: "神秘礼盒",
};

// ── 羁绊 ─────────────────────────────────────────────────────────────

/** 结算里的羁绊奖励 → 一句提示（和后端 affinity_bond.settle_reply 同口径）。 */
export function bondSettleText(settled: BondSettle | undefined | null): string | null {
  if (!settled) return null;
  const parts: string[] = [];
  if (settled.streak) parts.push(`连续来往 ${settled.streak.days} 天${settled.streak_affinity ? "，好感涨了" : ""}`);
  if (settled.weekly?.length) parts.push(`完成周任务：${settled.weekly.join("、")}`);
  if (settled.weekly_bonus_item) parts.push(`这周任务全做完，送你一张${settled.weekly_bonus_item}`);
  if (settled.anniversary) parts.push(`认识满 ${settled.anniversary.days} 天，送你${settled.anniversary.item}`);
  if (settled.badges?.length) parts.push(`获得徽章：${settled.badges.join("、")}`);
  return parts.length ? parts.join("；") : null;
}

export function badgeCounts(badges: BondBadge[]): { earned: number; total: number } {
  return { earned: badges.filter((badge) => badge.earned).length, total: badges.length };
}

/** 徽章排序：已得到的按时间倒序在前，没得到的按完成度高的在前。 */
export function sortBadges(badges: BondBadge[]): BondBadge[] {
  const ratio = (badge: BondBadge) => badge.progress / Math.max(badge.target, 1);
  return [...badges].sort((a, b) => {
    if (a.earned !== b.earned) return a.earned ? -1 : 1;
    if (a.earned) return String(b.earned_at ?? "").localeCompare(String(a.earned_at ?? ""));
    return ratio(b) - ratio(a);
  });
}

export function weeklyPercent(view: WeeklyView): number {
  return questPercent({ ...view, day: view.week });
}

export const TREND_TONE: Record<string, "success" | "default" | "warning"> = { 升温: "success", 平稳: "default", 降温: "warning" };

/** 最近 14 天每天涨了多少 → 柱子高度百分比（全 0 时都是 0）。 */
export function dailyBars(daily: Array<{ day: string; gain: number }>): Array<{ day: string; gain: number; height: number }> {
  const max = Math.max(0, ...daily.map((row) => row.gain));
  return daily.map((row) => ({ ...row, height: max > 0 ? Math.round((Math.max(row.gain, 0) / max) * 100) : 0 }));
}

/** 里程碑表（{"3": 1, "7": 3}）→ 按天数排好的行。 */
export function milestoneRows<T>(table: Record<string, T>): Array<{ day: number; value: T }> {
  return Object.entries(table).map(([day, value]) => ({ day: Number(day), value })).sort((a, b) => a.day - b.day);
}
