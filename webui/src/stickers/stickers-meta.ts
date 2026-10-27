// 表情包页的纯函数：筛选、标签解析、策略校验与文案。不依赖 React，方便单测。
import type { Sticker, StickerSettings } from "./types";

export type StickerFilter = "all" | "active" | "pending" | "disabled";

export const FILTER_LABELS: Record<StickerFilter, string> = {
  all: "全部",
  active: "可用",
  pending: "待审核",
  disabled: "已停用",
};

export const SOURCE_LABELS: Record<Sticker["source"], string> = {
  manual: "手工放入",
  upload: "后台上传",
  member: "群友收藏",
};

export const FREQUENCY_HINTS: Record<StickerSettings["frequency"], string> = {
  rare: "只有气氛特别对时才发，大多数回合不发",
  normal: "像普通群友一样偶尔发，接梗、吐槽、无语时甩一张",
  often: "爱发表情包，情绪到了就甩，可以只回一张图",
};

export const COLLECT_LABELS: Record<StickerSettings["collectMode"], string> = {
  off: "不让群友收藏",
  review: "进待审核",
  auto: "直接可用",
};

export const NAME_LIMIT = 16;
export const TAG_LIMIT = 8;
export const TAG_CHARS = 10;

export function stickerState(sticker: Sticker): Exclude<StickerFilter, "all"> {
  if (sticker.status === "pending") return "pending";
  return sticker.enabled ? "active" : "disabled";
}

export function filterStickers(stickers: Sticker[], filter: StickerFilter, keyword: string, tag: string | null): Sticker[] {
  const needle = keyword.trim().toLowerCase();
  return stickers.filter((sticker) => {
    if (filter !== "all" && stickerState(sticker) !== filter) return false;
    if (tag && !sticker.tags.includes(tag)) return false;
    if (!needle) return true;
    return [sticker.name, sticker.description, sticker.id, ...sticker.tags].some((field) => field.toLowerCase().includes(needle));
  });
}

/** “尴尬, 好笑 无语” → ["尴尬", "好笑", "无语"]；去重、截断，和后端 clean_tags 同口径。 */
export function parseTags(text: string): string[] {
  const output: string[] = [];
  for (const raw of text.split(/[\s,，、/]+/)) {
    const tag = raw.replace(/[[\]【】<>:：|｜]/g, "").trim().slice(0, TAG_CHARS);
    if (tag && !output.some((item) => item.toLowerCase() === tag.toLowerCase())) output.push(tag);
    if (output.length >= TAG_LIMIT) break;
  }
  return output;
}

export function cleanName(text: string): string {
  return text.replace(/[[\]【】<>:：|｜\r\n\t]+/g, " ").replace(/\s+/g, " ").trim().slice(0, NAME_LIMIT).trim();
}

/** 上传时从文件名猜一个名字：去掉扩展名和常见的“微信图片_2026…”之类前缀。 */
export function nameFromFile(fileName: string): string {
  const base = fileName.replace(/\.[^.]+$/, "").replace(/^(微信图片|QQ图片|IMG|image|截图)[_\- ]?/i, "").replace(/[_-]?\d{6,}$/, "");
  return cleanName(base.replace(/[_-]+/g, " "));
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export const STICKER_NUMERIC_KEYS = ["groupCooldownSeconds", "groupDailyLimit", "maxPerMessage", "menuSize", "memberDailyCollect"] as const;
export type StickerNumericKey = (typeof STICKER_NUMERIC_KEYS)[number];

export const STICKER_SETTING_RANGES: Record<StickerNumericKey, { label: string; min: number; max: number; unit?: string }> = {
  groupCooldownSeconds: { label: "同一个群两次表情包至少间隔", min: 0, max: 86400, unit: "秒" },
  groupDailyLimit: { label: "每个群每天最多发", min: 0, max: 10000, unit: "张" },
  maxPerMessage: { label: "一次发言最多带", min: 1, max: 3, unit: "张" },
  menuSize: { label: "每轮给模型看的菜单", min: 3, max: 40, unit: "项" },
  memberDailyCollect: { label: "每个成员每天最多收藏", min: 0, max: 200, unit: "张" },
};

export function validateStickerSettings(settings: StickerSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const key of STICKER_NUMERIC_KEYS) {
    const range = STICKER_SETTING_RANGES[key];
    const value = settings[key];
    if (!Number.isInteger(value) || value < range.min || value > range.max) {
      errors[key] = `${range.label}需要在 ${range.min}–${range.max} 之间`;
    }
  }
  if (settings.groupMode === "allowlist" && settings.allowedGroups.length === 0) {
    errors.allowedGroups = "白名单模式至少要填一个群";
  }
  return errors;
}

export function stickerSettingsEqual(left: StickerSettings, right: StickerSettings): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function describeStickerAccess(settings: StickerSettings): string {
  if (!settings.enabled) return "已关闭：Agent 不会发表情包";
  const scope = settings.groupMode === "allowlist" ? `只在 ${settings.allowedGroups.length} 个白名单群` : "所有群";
  const cooldown = settings.groupCooldownSeconds > 0 ? `，同群至少隔 ${settings.groupCooldownSeconds} 秒` : "";
  return `${scope}可用${cooldown}，每群每天最多 ${settings.groupDailyLimit} 张`;
}
