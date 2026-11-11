// 「表情包」页的接口类型，与后端 webui/stickers.py 的 camelCase 输出一致。

export type StickerFrequency = "rare" | "normal" | "often";
export type StickerStatus = "active" | "pending";
export type StickerSource = "manual" | "upload" | "member" | "stolen";
export type StealMode = "off" | "review" | "auto";
export type LabelMode = "fill" | "merge" | "replace";
export type LabelField = "name" | "tags" | "description";

export interface StickerSettings {
  enabled: boolean;
  groupMode: "all" | "allowlist";
  allowedGroups: string[];
  blockedGroups: string[];
  frequency: StickerFrequency;
  groupCooldownSeconds: number;
  groupDailyLimit: number;
  maxPerMessage: number;
  menuSize: number;
  allowProactive: boolean;
  collectMode: "off" | "review" | "auto";
  collectAdminOnly: boolean;
  memberDailyCollect: number;
  maxBytes: number;
  /** 私聊里也发表情包（按人计数）。 */
  privateEnabled: boolean;
  privateDailyLimit: number;
  privateCooldownSeconds: number;
  /** AI 标注：识图模型起名、打标签、写什么时候发、认图上的字。 */
  aiLabelEnabled: boolean;
  aiLabelOnUpload: boolean;
  aiLabelOnCollect: boolean;
  aiLabelStyle: string;
  aiLabelReviewUnsafe: boolean;
  /** Bot 偷图（steal_sticker / 私聊 stickers）。 */
  stealMode: StealMode;
  stealPhotos: boolean;
  groupDailySteals: number;
  stealCooldownSeconds: number;
  privateStealEnabled: boolean;
  privateDailySteals: number;
  stealProtectedUsers: string[];
  /** 3.0：发送节奏 */
  groupHourlyLimit: number;
  minMessagesBetween: number;
  stickerCooldownMinutes: number;
  quietEnabled: boolean;
  quietStart: string;
  quietEnd: string;
  /** 3.0：菜单怎么挑 */
  menuStrategy: MenuStrategy;
  newBoostDays: number;
  menuHintChars: number;
  menuShowTags: boolean;
  /** 3.0：偷图 / 收藏进阶 */
  stealScope: "all" | "origin";
  collectScope: "all" | "origin";
  stealAnimated: boolean;
  stealMinSide: number;
  stealSenderDaily: number;
  stealBlockedGroups: string[];
  reviewBelowConfidence: number;
  memberSelfProtect: boolean;
  memberManageOwn: boolean;
  /** 3.0：AI 标注进阶 */
  aiLabelRetry: boolean;
  aiLabelConcurrency: number;
  aiLabelAliases: boolean;
  trashDays: number;
  groupOverrides: Record<string, GroupOverride>;
}

export type MenuStrategy = "fresh" | "popular" | "explore" | "balanced";

/** 某个群单独的设置；null = 跟全局一样。 */
export interface GroupOverride {
  enabled: boolean | null;
  frequency: StickerFrequency | null;
  groupCooldownSeconds: number | null;
  groupDailyLimit: number | null;
  groupHourlyLimit: number | null;
  maxPerMessage: number | null;
  menuSize: number | null;
  allowProactive: boolean | null;
  stealMode: StealMode | null;
  collectMode: "off" | "review" | "auto" | null;
  note: string;
}

export interface StickerAiMeta {
  model: string | null;
  confidence: number | null;
  kind: string | null;
  safe: boolean;
  text: string;
  labeledAt: string | null;
  undone: boolean;
  mood?: string | null;
  imported?: boolean;
}

export interface StickerOrigin {
  via: "steal" | "collect" | "private" | string | null;
  groupId: string | null;
  senderId: string | null;
  senderName: string | null;
  messageId: string | null;
  qqSticker: boolean;
}

export interface Sticker {
  id: string;
  name: string;
  tags: string[];
  description: string;
  enabled: boolean;
  status: StickerStatus;
  groups: string[];
  source: StickerSource;
  addedBy: string | null;
  addedGroup: string | null;
  size: number;
  mime: string;
  createdAt: string | null;
  updatedAt: string | null;
  uses: number;
  lastUsed: string | null;
  fileMissing: boolean;
  ai: StickerAiMeta | null;
  origin: StickerOrigin | null;
  canUndoLabel: boolean;
  placeholderName: boolean;
  /** 3.0 */
  aliases: string[];
  pinned: boolean;
  /** 0 不主动用 / 1 正常 / 2 多用 / 3 最爱 */
  weight: number;
  packs: string[];
  note: string;
  width: number;
  height: number;
  frames: number;
  animated: boolean;
  dimensions: string;
  shape: string;
  dailyCap: number;
  sentToday: number;
  groupUses: { groupId: string | null; uses: number }[];
  deletedAt: string | null;
}

export interface StickerPack {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  groups: string[];
  createdAt: string | null;
  count: number;
  uses: number;
  cover: string[];
}

export interface StickerUsage {
  day: string | null;
  totals: Record<string, number>;
  groups: { groupId: string; sent: number; groupName?: string | null }[];
  groupLimit: number;
  drops?: Record<string, number>;
  quietNow?: boolean;
}

export interface StickerOverview {
  settings: StickerSettings;
  defaults: StickerSettings;
  frequencyLabels: Record<StickerFrequency, string>;
  usage: StickerUsage;
  counts: {
    total: number; active: number; pending: number; disabled: number; stolen: number; aiLabeled: number; unlabeled: number;
    pinned: number; animated: number; trash: number; blocked: number; packs: number;
  };
  stickers: Sticker[];
  tags: { tag: string; count: number }[];
  groupNames: Record<string, string>;
  sourceLabels: Record<string, string>;
  vision: VisionStatus;
  labelJobs: LabelJobSummary[];
  packs: StickerPack[];
  menuStrategyLabels: Record<MenuStrategy, string>;
  weightLabels: Record<string, string>;
  moods: string[];
  index: { corrupt: boolean; recoveredFromBackup: boolean; hasBackup: boolean };
  labelScopes: Record<string, string>;
}

export interface VisionStatus {
  ready: boolean;
  model: string;
  provider: string;
  stats: { requests: number; ok: number; failed: number; cached: number; cacheSize: number; lastError: { at?: string; error?: string } | null };
}

export interface StickerLabel {
  ok: boolean;
  name: string;
  tags: string[];
  description: string;
  text: string;
  kind: string;
  kindLabel: string;
  safe: boolean;
  confidence: number;
  model: string;
  reason: string;
  reasonText: string | null;
  cached: boolean;
  durationMs: number;
  warnings: string[];
  aliases?: string[];
  mood?: string;
  retried?: boolean;
}

export interface LabelJobSummary {
  id: string;
  status: "running" | "done" | "cancelled" | "failed";
  mode: LabelMode;
  modeLabel: string;
  fields: LabelField[];
  total: number;
  done: number;
  changed: number;
  unchanged: number;
  failed: number;
  held: number;
  createdAt: number;
  finishedAt: number | null;
  elapsedMs: number;
  scope?: LabelScope;
  scopeLabel?: string;
  etaMs?: number | null;
  retried?: number;
}

export type LabelScope = "selected" | "unlabeled" | "all" | "placeholder" | "low_confidence" | "retry_failed";

export interface LabelJobRow {
  id: string;
  name?: string;
  ok: boolean;
  reason?: string;
  reasonText?: string;
  changed?: string[];
  before?: { name: string; tags: string[]; description: string };
  after?: { name: string; tags: string[]; description: string };
  text?: string;
  safe?: boolean;
  confidence?: number;
  held?: boolean;
}

export interface LabelJobDetail extends LabelJobSummary {
  results: LabelJobRow[];
}

export interface UploadResult {
  sticker: Sticker;
  created: boolean;
  namedBy?: "given" | "ai" | "fallback" | "existing";
  heldForReview?: boolean;
  label?: StickerLabel | null;
}

export interface StickerPatch {
  name?: string;
  tags?: string[];
  description?: string;
  enabled?: boolean;
  status?: StickerStatus;
  groups?: string[];
  aliases?: string[];
  pinned?: boolean;
  weight?: number;
  packs?: string[];
  note?: string;
  dailyCap?: number;
}

export type StickerBulkAction =
  | "approve" | "enable" | "disable" | "delete" | "undo_label" | "hold" | "pin" | "unpin"
  | "add_tags" | "remove_tags" | "set_groups" | "add_pack" | "remove_pack" | "set_weight" | "purge" | "block";

export interface StickerBulkExtra {
  tags?: string[];
  groups?: string[];
  pack?: string;
  weight?: number;
}

export interface StickerPromptPreview {
  open: boolean;
  reason: string | null;
  reasonText: string | null;
  available: number;
  leftToday: number;
  cooldownSeconds: number;
  text: string;
  leftHour?: number | null;
  chars?: number;
  scene?: string;
  scenes?: Record<string, string>;
  menu?: MenuPlanRow[];
  overridden?: string[];
  effective?: Record<string, string | number>;
}

export interface MenuPlanRow {
  id: string;
  name: string;
  hint: string;
  /** pinned / new / favorite / 或菜单策略名 */
  why: string;
}

export interface StickerResolve {
  match: "id" | "name" | "alias" | "tag" | "none";
  matchText: string;
  candidates: { id: string; name: string }[];
  count: number;
  gateOpen: boolean;
  reasonText: string | null;
}

export interface HistoryDay {
  day: string;
  sent: number;
  dropped: number;
  collected: number;
  stolen: number;
  labeled: number;
  private_sent: number;
  [key: string]: number | string;
}

export interface Trend {
  recent: number;
  previous: number;
  changePercent: number | null;
}

export interface StickerMiss {
  name: string;
  count: number;
  last: string | null;
  groups: string[];
  suggestions: { id: string; name: string; why?: string }[];
}

export interface StickerInsights {
  days: number;
  history: HistoryDay[];
  trend: Record<"sent" | "stolen" | "collected" | "dropped", Trend>;
  totals: { uses: number; usable: number; everywhere: number; neverUsed: number; animated: number; bytes: number; concentration: number };
  top: { id: string; name: string; uses: number; lastUsed: string | null; sentToday: number }[];
  neverUsed: { id: string; name: string; createdAt: string | null }[];
  groups: { groupId: string | null; private: boolean; uses: number; today: number; favorite: string; groupName?: string | null }[];
  moods: { mood: string; count: number }[];
  moodGaps: string[];
  moodUnknown: number;
  sources: { source: string; label: string; count: number }[];
  tags: { tag: string; count: number; uses: number }[];
  drops: { reason: string; reasonText: string; count: number }[];
  misses: StickerMiss[];
  missCount: number;
  recent: { at: string | null; groupId: string | null; stickerId: string; name: string }[];
  growth: ({ week: string; total: number } & Record<string, number | string>)[];
  contributors: { userId: string; count: number }[];
}

export type HealthSeverity = "error" | "warn" | "info";

export interface HealthIssue {
  kind: string;
  severity: HealthSeverity;
  title: string;
  advice: string;
  count: number;
  ids: string[];
  fixable: boolean;
  files?: string[];
  groups?: string[][];
}

export interface StickerHealth {
  score: number;
  checkedAt: string;
  total: number;
  issues: HealthIssue[];
  index: { corrupt: boolean; recoveredFromBackup: boolean; hasBackup: boolean };
  trash: { count: number; bytes: number };
  bytes: number;
}

export interface StickerTrash {
  stickers: Sticker[];
  trashDays: number;
  autoPurged: number;
  blocked: string[];
}

export interface ArchiveInfo {
  count: number;
  existing: number;
  new: number;
  packs: string[];
  exportedAt: string | null;
  includePrivate: boolean;
}

export interface ImportResult {
  added: number;
  updated: number;
  skipped: number;
  failed: { name: string; reason: string }[];
  failedCount: number;
  packsCreated: number;
}
