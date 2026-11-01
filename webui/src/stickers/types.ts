// 「表情包」页的接口类型，与后端 webui/stickers.py 的 camelCase 输出一致。

export type StickerFrequency = "rare" | "normal" | "often";
export type StickerStatus = "active" | "pending";
export type StickerSource = "manual" | "upload" | "member";

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
  counts: { total: number; active: number; pending: number; disabled: number };
  stickers: Sticker[];
  tags: { tag: string; count: number }[];
  groupNames: Record<string, string>;
}

export interface StickerPatch {
  name?: string;
  tags?: string[];
  description?: string;
  enabled?: boolean;
  status?: StickerStatus;
  groups?: string[];
}

export type StickerBulkAction = "approve" | "enable" | "disable" | "delete";

export interface StickerPromptPreview {
  open: boolean;
  reason: string | null;
  reasonText: string | null;
  available: number;
  leftToday: number;
  cooldownSeconds: number;
  text: string;
}
