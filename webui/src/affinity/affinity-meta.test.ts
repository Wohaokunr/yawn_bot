import { describe, expect, it } from "vitest";
import { discountLabel, perkSummary, questPercent, toggleUnlock, unclaimed, validatePolicy } from "./affinity-meta";
import type { AffinityPolicy, LevelPerksPolicy, QuestsView } from "./types";

const level = (name: string): LevelPerksPolicy => ({
  level: name, tone: "", behavior: "", private_turns_pct: 0, private_web_bonus: 0, private_sticker_bonus: 0, web_search_bonus: 0,
  sandbox_bonus: 0, sticker_collect_bonus: 0, persona_edit_bonus: 0, memory_bonus: 0, checkin_points_pct: 0, gift_daily: 3,
  shop_discount_pct: 0, unlocks: [], levelup_items: {},
});

const policy = (): AffinityPolicy => ({
  enabled: true,
  levels: ["陌生", "面熟", "熟人", "朋友", "好朋友", "挚友"].map(level),
  gifts: [{ key: "candy", name: "糖果", emoji: "🍬", price: 10, affinity: 1, enabled: true }],
  gift_affinity_daily_cap: 10,
  favorite_multiplier: 2,
  shop: [],
  quests: { enabled: true, per_day: 3, reward_affinity: 2, all_done_item: "lucky_card" },
  decay: { enabled: true, idle_days: 14, per_day: 1, keep_level: true },
  care: { idle_days: 4, give_up_days: 45 },
  interact_affinity_daily_cap: 3,
  announce_levelup: true,
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
});
