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
}

export interface StickerAiMeta {
  model: string | null;
  confidence: number | null;
  kind: string | null;
  safe: boolean;
  text: string;
  labeledAt: string | null;
  undone: boolean;
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
}

export interface StickerUsage {
  day: string | null;
  totals: Record<string, number>;
  groups: { groupId: string; sent: number; groupName?: string | null }[];
  groupLimit: number;
}

export interface StickerOverview {
  settings: StickerSettings;
  defaults: StickerSettings;
  frequencyLabels: Record<StickerFrequency, string>;
  usage: StickerUsage;
  counts: { total: number; active: number; pending: number; disabled: number; stolen: number; aiLabeled: number; unlabeled: number };
  stickers: Sticker[];
  tags: { tag: string; count: number }[];
  groupNames: Record<string, string>;
  sourceLabels: Record<string, string>;
  vision: VisionStatus;
  labelJobs: LabelJobSummary[];
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
}

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
}

export type StickerBulkAction = "approve" | "enable" | "disable" | "delete" | "undo_label";

export interface StickerPromptPreview {
  open: boolean;
  reason: string | null;
  reasonText: string | null;
  available: number;
  leftToday: number;
  cooldownSeconds: number;
  text: string;
}
