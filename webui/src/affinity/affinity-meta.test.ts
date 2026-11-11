import { describe, expect, it } from "vitest";
import { bondSettleText, dailyBars, discountLabel, milestoneRows, perkSummary, questPercent, sortBadges, toggleUnlock, unclaimed, validatePolicy } from "./affinity-meta";
import type { AffinityPolicy, BondBadge, LevelPerksPolicy, QuestsView } from "./types";

const level = (name: string): LevelPerksPolicy => ({
  level: name, tone: "", behavior: "", title: "", speech_style: "", initiative: 0, private_task_bonus: 0, private_turns_pct: 0, private_web_bonus: 0, private_sticker_bonus: 0, web_search_bonus: 0,
  sandbox_bonus: 0, sticker_collect_bonus: 0, persona_edit_bonus: 0, memory_bonus: 0, checkin_points_pct: 0, gift_daily: 3,
  shop_discount_pct: 0, unlocks: [], levelup_items: {},
});

const policy = (): AffinityPolicy => ({
  enabled: true,
  levels: ["陌生", "面熟", "熟人", "朋友", "好朋友", "挚友"].map(level),
  gifts: [{ key: "candy", name: "糖果", emoji: "🍬", price: 10, affinity: 1, enabled: true, min_level: "陌生" }],
  gift_affinity_daily_cap: 10,
  favorite_multiplier: 2,
  shop: [],
  quests: { enabled: true, per_day: 3, reward_affinity: 2, all_done_item: "lucky_card" },
  decay: { enabled: true, idle_days: 14, per_day: 1, keep_level: true },
  care: { idle_days: 4, give_up_days: 45 },
  interact_affinity_daily_cap: 3,
  announce_levelup: true,
  version: 3,
  moments: { enabled: true, daily_up: 3, daily_down: 2, allow_down: true, keep_level: true, cooldown_minutes: 10 },
  streak: { enabled: true, milestones: { "3": 1, "7": 3 }, milestone_items: { "7": "lucky_card" } },
  weekly: { enabled: true, per_week: 3, reward_affinity: 5, all_done_item: "shield" },
  fortune: { enabled: true, reward_affinity: 1 },
  anniversary: { enabled: true, items: { "30": "repair_card" } },
  badges_enabled: true,
  bot_mood_enabled: true,
  prompt_bond_enabled: true,
});

const badge = (key: string, earned: boolean, progress: number, target: number, at: string | null = null): BondBadge => ({
  key, name: key, emoji: "🏅", description: "", earned, earned_at: at, progress, target, reward_item: "",
});

describe("affinity-meta", () => {
  it("formats discounts as 折", () => {
    expect(discountLabel(0)).toBe("原价");
    expect(discountLabel(10)).toBe("9 折");
    expect(discountLabel(5)).toBe("95 折");
    expect(discountLabel(15)).toBe("85 折");
  });

  it("summarises a level's perks", () => {
    const text = perkSummary({ level: "熟人", tone: "", behavior: "", bonuses: [{ key: "private_turns_pct", label: "私聊轮数", value: 25, unit: "%" }], gift_daily: 5, shop_discount_pct: 5, unlocks: [{ key: "nickname", label: "专属称呼", description: "" }], levelup_items: {} });
    expect(text).toBe("私聊轮数 +25%；送礼 5 次/天；商店 95 折；解锁 专属称呼");
  });

  it("measures quest progress and unclaimed rewards", () => {
    const view: QuestsView = { enabled: true, day: "2026-11-09", items: [
      { key: "checkin", title: "签到一次", progress: 1, target: 1, done: true, claimed: false },
      { key: "private_chat", title: "私聊 3 轮", progress: 1, target: 3, done: false, claimed: false },
    ], reward_affinity: 2, all_done_item: "", all_done_item_name: "", bonus_claimed: false, all_done: false };
    expect(questPercent(view)).toBe(67);
    expect(unclaimed(view)).toBe(1);
  });

  it("validates policy edits", () => {
    expect(validatePolicy(policy())).toBeNull();
    const bad = policy();
    bad.levels[2] = { ...bad.levels[2], shop_discount_pct: 95 };
    expect(validatePolicy(bad)).toContain("熟人");
    const dupe = policy();
    dupe.gifts.push({ ...dupe.gifts[0] });
    expect(validatePolicy(dupe)).toContain("重复");
    const care = policy();
    care.care = { idle_days: 10, give_up_days: 5 };
    expect(validatePolicy(care)).toContain("久别");
  });

  it("toggles unlocks without duplicates", () => {
    const on = toggleUnlock(level("朋友"), "care", true);
    expect(toggleUnlock(on, "care", true).unlocks).toEqual(["care"]);
    expect(toggleUnlock(on, "care", false).unlocks).toEqual([]);
  });

  it("validates bond fields: title length, initiative range, gift level, milestones", () => {
    const p = policy();
    p.levels[2] = { ...p.levels[2], title: "一二三四五六七八九十一二三" };
    expect(validatePolicy(p)).toMatch(/称号太长/);
    const q = policy();
    q.levels[1] = { ...q.levels[1], initiative: 4 };
    expect(validatePolicy(q)).toMatch(/主动程度/);
    const r = policy();
    r.gifts[0] = { ...r.gifts[0], min_level: "神仙" };
    expect(validatePolicy(r)).toMatch(/不存在/);
    const t = policy();
    t.streak = { ...t.streak, milestones: { "1": 2 } };
    expect(validatePolicy(t)).toMatch(/里程碑/);
    expect(validatePolicy(policy())).toBeNull();
  });

  it("summarises bond settlements like the backend", () => {
    expect(bondSettleText(null)).toBeNull();
    expect(bondSettleText({})).toBeNull();
    expect(bondSettleText({ streak: { days: 7 }, streak_affinity: 3, badges: ["一周之约"] })).toBe("连续来往 7 天，好感涨了；获得徽章：一周之约");
  });

  it("sorts badges: earned (newest first) then by progress", () => {
    const rows = sortBadges([badge("a", false, 1, 10), badge("b", true, 1, 1, "2026-11-01"), badge("c", false, 9, 10), badge("d", true, 1, 1, "2026-11-05")]);
    expect(rows.map((row) => row.key)).toEqual(["d", "b", "c", "a"]);
  });

  it("scales daily bars and sorts milestones", () => {
    expect(dailyBars([{ day: "a", gain: 0 }, { day: "b", gain: 4 }, { day: "c", gain: 2 }]).map((row) => row.height)).toEqual([0, 100, 50]);
    expect(dailyBars([{ day: "a", gain: 0 }])[0].height).toBe(0);
    expect(milestoneRows({ "30": 8, "3": 1, "7": 3 }).map((row) => row.day)).toEqual([3, 7, 30]);
  });
});
