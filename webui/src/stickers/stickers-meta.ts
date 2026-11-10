// 表情包页的纯函数：筛选、标签解析、策略校验与文案。不依赖 React，方便单测。
import type { LabelJobSummary, LabelMode, Sticker, StickerLabel, StickerSettings } from "./types";

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
  stolen: "Bot 偷的",
};

export type SourceFilter = "any" | Sticker["source"];
export type AiFilter = "any" | "labeled" | "unlabeled" | "unsafe";

export const AI_FILTER_LABELS: Record<AiFilter, string> = {
  any: "AI 标注：全部",
  labeled: "AI 标注过",
  unlabeled: "还没 AI 标注",
  unsafe: "AI 觉得不合适",
};

export const STEAL_LABELS: Record<StickerSettings["stealMode"], string> = {
  off: "不偷",
  review: "偷来先审核",
  auto: "偷来直接用",
};

export const LABEL_MODE_LABELS: Record<LabelMode, string> = {
  fill: "只补空的",
  merge: "补空 + 合并标签",
  replace: "全部换成 AI 的",
};

export const LABEL_MODE_HINTS: Record<LabelMode, string> = {
  fill: "名字是文件名/编号时才改名；没有标签、没有说明的才补",
  merge: "同上，另外把 AI 的标签并进已有标签",
  replace: "名字、标签、说明都换成 AI 起的（每张都能撤销）",
};

export const VIA_LABELS: Record<string, string> = {
  steal: "群里偷的",
  collect: "群友让收藏的",
  private: "私聊里存的",
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

export function isAiLabeled(sticker: Sticker): boolean {
  return Boolean(sticker.ai && !sticker.ai.undone);
}

export function filterStickers(
  stickers: Sticker[],
  filter: StickerFilter,
  keyword: string,
  tag: string | null,
  source: SourceFilter = "any",
  ai: AiFilter = "any",
): Sticker[] {
  const needle = keyword.trim().toLowerCase();
  return stickers.filter((sticker) => {
    if (filter !== "all" && stickerState(sticker) !== filter) return false;
    if (tag && !sticker.tags.includes(tag)) return false;
    if (source !== "any" && sticker.source !== source) return false;
    if (ai === "labeled" && !isAiLabeled(sticker)) return false;
    if (ai === "unlabeled" && isAiLabeled(sticker)) return false;
    if (ai === "unsafe" && !(sticker.ai && !sticker.ai.safe)) return false;
    if (!needle) return true;
    // 图上的字（AI 认出来的）也能搜到
    return [sticker.name, sticker.description, sticker.id, sticker.ai?.text ?? "", sticker.origin?.senderName ?? "", ...sticker.tags].some((field) => field.toLowerCase().includes(needle));
  });
}

/** 0.83 → “把握 83%”；没有值返回空串。 */
export function confidenceText(value: number | null | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "";
  return `把握 ${Math.round(Math.min(Math.max(value, 0), 1) * 100)}%`;
}

/** 偷图来源写成一句话：“群里偷的 · 某群 · 小明（10001）发的”。 */
export function describeOrigin(sticker: Sticker, groupNames: Record<string, string>): string {
  const origin = sticker.origin;
  if (!origin) return "";
  const parts = [VIA_LABELS[origin.via ?? ""] ?? "收进来的"];
  if (origin.groupId) parts.push(groupNames[origin.groupId] ?? `群 ${origin.groupId}`);
  if (origin.senderName || origin.senderId) {
    const who = origin.senderName && origin.senderId ? `${origin.senderName}（${origin.senderId}）` : origin.senderName ?? origin.senderId;
    parts.push(`${who} 发的`);
  }
  if (origin.qqSticker) parts.push("原本就是 QQ 表情包");
  return parts.join(" · ");
}

/** 批量任务进度百分比（0–100）。 */
export function jobPercent(job: Pick<LabelJobSummary, "done" | "total">): number {
  if (job.total <= 0) return 100;
  return Math.min(100, Math.round((job.done / job.total) * 100));
}

export function describeJob(job: LabelJobSummary): string {
  const head = job.status === "running" ? `正在标注 ${job.done}/${job.total}` : job.status === "cancelled" ? `已取消（标了 ${job.done}/${job.total}）` : job.status === "failed" ? "任务出错" : `标完 ${job.total} 张`;
  const tail = [job.changed ? `改了 ${job.changed} 张` : "", job.failed ? `${job.failed} 张失败` : "", job.held ? `${job.held} 张转待审核` : ""].filter(Boolean).join("，");
  return tail ? `${head}：${tail}` : head;
}

/** 上传框里：把 AI 建议填进还空着的字段（用户已经写了的不覆盖）。 */
export function applySuggestion(item: { name: string; tags: string }, label: StickerLabel, placeholder: boolean): { name: string; tags: string } {
  if (!label.ok) return item;
  return {
    name: !cleanName(item.name) || placeholder ? label.name : item.name,
    tags: parseTags(item.tags).length ? item.tags : label.tags.join("，"),
  };
}

/** 文件名猜出来的名字算不算“没起名”（和后端 looks_like_placeholder 大致同口径）。 */
export function looksLikePlaceholder(name: string): boolean {
  const text = name.replace(/\s+/g, "");
  if (!text) return true;
  return /^(img|image|pic|photo|screenshot|mmexport|qq图片|微信图片|截图|下载|未命名|tmp|temp|dsc)?[_\-0-9a-f]*$/i.test(text) && /\d/.test(text);
}

export function describeSteal(settings: StickerSettings): string {
  if (!settings.enabled || settings.stealMode === "off") return "Bot 不会偷图";
  const what = settings.stealPhotos ? "表情包和照片" : "只偷 QQ 表情包";
  return `${STEAL_LABELS[settings.stealMode]}，${what}，每群每天最多 ${settings.groupDailySteals} 张`;
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

export const STICKER_NUMERIC_KEYS = [
  "groupCooldownSeconds",
  "groupDailyLimit",
  "maxPerMessage",
  "menuSize",
  "memberDailyCollect",
  "privateDailyLimit",
  "privateCooldownSeconds",
  "groupDailySteals",
  "stealCooldownSeconds",
  "privateDailySteals",
] as const;
export type StickerNumericKey = (typeof STICKER_NUMERIC_KEYS)[number];

export const STICKER_SETTING_RANGES: Record<StickerNumericKey, { label: string; min: number; max: number; unit?: string }> = {
  groupCooldownSeconds: { label: "同一个群两次表情包至少间隔", min: 0, max: 86400, unit: "秒" },
  groupDailyLimit: { label: "每个群每天最多发", min: 0, max: 10000, unit: "张" },
  maxPerMessage: { label: "一次发言最多带", min: 1, max: 3, unit: "张" },
  menuSize: { label: "每轮给模型看的菜单", min: 3, max: 40, unit: "项" },
  memberDailyCollect: { label: "每个成员每天最多收藏", min: 0, max: 200, unit: "张" },
  privateDailyLimit: { label: "私聊里每人每天最多发", min: 0, max: 1000, unit: "张" },
  privateCooldownSeconds: { label: "私聊里两次表情包至少间隔", min: 0, max: 86400, unit: "秒" },
  groupDailySteals: { label: "每个群每天最多偷", min: 0, max: 200, unit: "张" },
  stealCooldownSeconds: { label: "同一个群两次偷图至少间隔", min: 0, max: 86400, unit: "秒" },
  privateDailySteals: { label: "私聊里每人每天最多存", min: 0, max: 200, unit: "张" },
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
  if (settings.aiLabelStyle.length > 300) errors.aiLabelStyle = "命名要求最多 300 字";
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
