// 好感玩法的接口类型：和后端 webui/affinity_routes.py 的返回形状一一对应。
// 策略（AffinityPolicy）保持 snake_case，与后端 pydantic 模型和 data/affinity/policy.json 一致。

export interface PerkBonus { key: string; label: string; value: number; unit: "%" | "次" }
export interface PerkUnlock { key: string; label: string; description: string }

export interface PerksPublic {
  level: string;
  tone: string;
  behavior: string;
  bonuses: PerkBonus[];
  gift_daily: number;
  shop_discount_pct: number;
  unlocks: PerkUnlock[];
  levelup_items: Record<string, number>;
}

export interface NextUnlocks {
  level: string;
  to_next: number | null;
  unlocks: PerkUnlock[];
  better: Array<{ key: string; label: string; delta: number; unit: "%" | "次" }>;
  gift_daily: number;
  shop_discount_pct: number;
  levelup_items: Record<string, number>;
}

export interface QuestItem { key: string; title: string; progress: number; target: number; done: boolean; claimed: boolean }
export interface QuestsView {
  enabled: boolean;
  day: string;
  items: QuestItem[];
  reward_affinity: number;
  all_done_item: string;
  all_done_item_name: string;
  bonus_claimed: boolean;
  all_done: boolean;
}

export interface ShopRow {
  key: string;
  name: string;
  description: string;
  price: number;
  final_price: number;
  min_level: string;
  unlocked: boolean;
  have: number;
  max_hold: number;
}

export interface GiftMenuRow { key: string; name: string; emoji: string; price: number; finalPrice: number; affinity: number }

export interface PerksState {
  enabled: boolean;
  level: string;
  levelIndex: number;
  current: PerksPublic;
  next: NextUnlocks | null;
  ladder: Array<PerksPublic & { floor: number; name: string }>;
  wallet: { total: number; groups: Array<{ group_id: number; group_name: string; points: number }> };
  bag: { items: Array<{ key: string; name: string; count: number; description: string }>; shield_until: string; lucky_today: boolean };
  quests: QuestsView;
  gifts: {
    favorite: string | null;
    sentToday: number;
    dailyLimit: number;
    dailyAffinityCap: number;
    favoriteMultiplier: number;
    menu: GiftMenuRow[];
    totals: Record<string, number>;
    log: Array<{ key: string; name: string; count: number; cost: number; favorite: boolean; affinity: number; group_id: number | null; at: string }>;
  };
  shop: ShopRow[];
  nickname: string;
  nicknameActive: boolean;
  nicknameUnlockLevel: string | null;
  interactions: Array<{ word: string; key: string; unlocked: boolean; needLevel: string | null; count: number }>;
  levelHistory: Array<{ level: string; at: string; up: boolean }>;
  itemLog: Array<{ key: string; action: string; at: string; count?: number; cost?: number; note?: string }>;
  /** 只有管理台返回 */
  points?: number;
  lastPrivate?: string | null;
}

export interface QuestSettle { claimed: string[]; affinity: number; bonus_item: string | null }
export interface ActionResponse<R = Record<string, unknown>> { result: R; quests: QuestSettle; state: PerksState }

// ── 策略 ──────────────────────────────────────────────────────────────

export interface LevelPerksPolicy {
  level: string;
  tone: string;
  behavior: string;
  private_turns_pct: number;
  private_web_bonus: number;
  private_sticker_bonus: number;
  web_search_bonus: number;
  sandbox_bonus: number;
  sticker_collect_bonus: number;
  persona_edit_bonus: number;
  memory_bonus: number;
  checkin_points_pct: number;
  gift_daily: number;
  shop_discount_pct: number;
  unlocks: string[];
  levelup_items: Record<string, number>;
}

export interface GiftPolicy { key: string; name: string; emoji: string; price: number; affinity: number; enabled: boolean }
export interface ShopPolicy { key: string; name: string; price: number; description: string; min_level: string; max_hold: number; enabled: boolean }

export interface AffinityPolicy {
  enabled: boolean;
  levels: LevelPerksPolicy[];
  gifts: GiftPolicy[];
  gift_affinity_daily_cap: number;
  favorite_multiplier: number;
  shop: ShopPolicy[];
  quests: { enabled: boolean; per_day: number; reward_affinity: number; all_done_item: string };
  decay: { enabled: boolean; idle_days: number; per_day: number; keep_level: boolean };
  care: { idle_days: number; give_up_days: number };
  interact_affinity_daily_cap: number;
  announce_levelup: boolean;
}

export interface PolicyMeta {
  levels: Array<{ floor: number; name: string }>;
  unlocks: PerkUnlock[];
  items: Array<{ key: string; description: string }>;
  bonusFields: Array<{ key: keyof LevelPerksPolicy; label: string }>;
  quests: Array<{ key: string; title: string; target: number }>;
}

export interface PolicyPayload { policy: AffinityPolicy; meta: PolicyMeta; defaults?: AffinityPolicy }

export interface AffinityStats {
  available: boolean;
  enabled: boolean;
  users: number;
  withAffinity: number;
  distribution: Array<{ level: string; count: number }>;
  sources30d: Array<{ source: string; label: string; total: number }>;
  daily: Array<{ day: string; gained: number; users: number }>;
  top: Array<{ userId: string; nickname: string | null; level: string; points: number }>;
  gifts: Array<{ key: string; name: string; count: number }>;
  items: Array<{ key: string; name: string; count: number }>;
  nicknames: number;
  questsDoneToday: number;
  activeShields: number;
  favoriteToday: string | null;
}
