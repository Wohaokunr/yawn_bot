// 表情包页的纯函数：筛选、标签解析、策略校验与文案。不依赖 React，方便单测。
import type { GroupOverride, HealthSeverity, HistoryDay, LabelJobSummary, LabelMode, MenuStrategy, Sticker, StickerLabel, StickerSettings, Trend } from "./types";

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
  extra: ExtraFilter = {},
): Sticker[] {
  const needle = keyword.trim().toLowerCase();
  return stickers.filter((sticker) => {
    if (extra.pack && !sticker.packs.includes(extra.pack)) return false;
    if (extra.mood && (sticker.ai?.undone || sticker.ai?.mood !== extra.mood)) return false;
    if (extra.flag && !FLAG_TESTS[extra.flag](sticker)) return false;
    if (filter !== "all" && stickerState(sticker) !== filter) return false;
    if (tag && !sticker.tags.includes(tag)) return false;
    if (source !== "any" && sticker.source !== source) return false;
    if (ai === "labeled" && !isAiLabeled(sticker)) return false;
    if (ai === "unlabeled" && isAiLabeled(sticker)) return false;
    if (ai === "unsafe" && !(sticker.ai && !sticker.ai.safe)) return false;
    if (!needle) return true;
    // 图上的字（AI 认出来的）也能搜到
    return [sticker.name, sticker.description, sticker.id, sticker.note, sticker.ai?.text ?? "", sticker.origin?.senderName ?? "", ...sticker.tags, ...sticker.aliases].some((field) => field.toLowerCase().includes(needle));
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

// ── 3.0：更多筛选、排序、显示 ──

export type FlagFilter = "pinned" | "animated" | "scoped" | "never" | "unpacked" | "missing" | "capped" | "silent";

export const FLAG_LABELS: Record<FlagFilter, string> = {
  pinned: "置顶的",
  animated: "动图",
  scoped: "限定了群的",
  never: "从没发过",
  unpacked: "不在任何合集",
  missing: "文件丢了",
  capped: "设了每日上限",
  silent: "不主动用",
};

const FLAG_TESTS: Record<FlagFilter, (sticker: Sticker) => boolean> = {
  pinned: (sticker) => sticker.pinned,
  animated: (sticker) => sticker.animated,
  scoped: (sticker) => sticker.groups.length > 0,
  never: (sticker) => sticker.uses === 0,
  unpacked: (sticker) => sticker.packs.length === 0,
  missing: (sticker) => sticker.fileMissing,
  capped: (sticker) => sticker.dailyCap > 0,
  silent: (sticker) => sticker.weight === 0,
};

export interface ExtraFilter {
  pack?: string | null;
  mood?: string | null;
  flag?: FlagFilter | null;
}

export type StickerSort = "default" | "newest" | "oldest" | "most" | "least" | "recent" | "name" | "size";

export const SORT_LABELS: Record<StickerSort, string> = {
  default: "默认（置顶在前）",
  newest: "最新入库",
  oldest: "最早入库",
  most: "用得最多",
  least: "用得最少",
  recent: "最近发过",
  name: "按名字",
  size: "文件最大",
};

/** 排序不改原数组；默认排序 = 置顶在前，其余保持库里的顺序。 */
export function sortStickers(stickers: Sticker[], sort: StickerSort): Sticker[] {
  const rows = [...stickers];
  const text = (value: string | null) => value ?? "";
  switch (sort) {
    case "newest":
      return rows.sort((a, b) => text(b.createdAt).localeCompare(text(a.createdAt)));
    case "oldest":
      return rows.sort((a, b) => text(a.createdAt).localeCompare(text(b.createdAt)));
    case "most":
      return rows.sort((a, b) => b.uses - a.uses);
    case "least":
      return rows.sort((a, b) => a.uses - b.uses);
    case "recent":
      return rows.sort((a, b) => text(b.lastUsed).localeCompare(text(a.lastUsed)));
    case "name":
      return rows.sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
    case "size":
      return rows.sort((a, b) => b.size - a.size);
    default:
      return rows.sort((a, b) => Number(b.pinned) - Number(a.pinned));
  }
}

export function parseEnum<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

export const WEIGHT_HINTS: Record<number, string> = {
  0: "不进菜单、按情绪也不挑它；只有模型点它的名字才发",
  1: "和别的图一样轮着来",
  2: "菜单里往前排，按情绪挑时更容易选中",
  3: "优先进菜单，按情绪挑时最容易选中",
};

export const ALIAS_LIMIT = 4;

/** 别名：去掉和名字一样的、重复的，最多 4 个（和后端 clean_aliases 同口径）。 */
export function cleanAliases(values: string[], name: string): string[] {
  const output: string[] = [];
  const taken = new Set([cleanName(name).replace(/\s+/g, "").toLowerCase()]);
  for (const raw of values) {
    const alias = cleanName(raw);
    const key = alias.replace(/\s+/g, "").toLowerCase();
    if (!alias || taken.has(key)) continue;
    taken.add(key);
    output.push(alias);
    if (output.length >= ALIAS_LIMIT) break;
  }
  return output;
}

export const MENU_STRATEGY_HINTS: Record<MenuStrategy, string> = {
  fresh: "每小时洗一次牌，最近发过的往后放；最均匀",
  popular: "发得多的排前面；群里的梗图会越用越多",
  explore: "没怎么发过的排前面；让冷门图有机会",
  balanced: "一张常用、一张冷门交替着排",
};

export const MENU_WHY_LABELS: Record<string, string> = {
  pinned: "置顶",
  new: "新入库",
  favorite: "最爱",
  fresh: "轮到",
  popular: "常用",
  explore: "冷门",
  balanced: "均衡",
};

export const SCOPE_LABELS = { all: "所有群都能用", origin: "只在来源群用" } as const;

export const OVERRIDE_FIELD_LABELS: Record<string, string> = {
  frequency: "性格",
  groupCooldownSeconds: "间隔",
  groupDailyLimit: "每日上限",
  groupHourlyLimit: "每小时上限",
  maxPerMessage: "每次张数",
  menuSize: "菜单大小",
  allowProactive: "主动发言",
  stealMode: "偷图",
  collectMode: "收藏",
};

export function emptyOverride(): GroupOverride {
  return {
    enabled: null,
    frequency: null,
    groupCooldownSeconds: null,
    groupDailyLimit: null,
    groupHourlyLimit: null,
    maxPerMessage: null,
    menuSize: null,
    allowProactive: null,
    stealMode: null,
    collectMode: null,
    note: "",
  };
}

/** 这个群改了哪几项（不含开关和备注）。 */
export function overrideChanges(override: GroupOverride): string[] {
  return Object.keys(OVERRIDE_FIELD_LABELS).filter((key) => override[key as keyof GroupOverride] !== null && override[key as keyof GroupOverride] !== undefined);
}

export function overrideIsEmpty(override: GroupOverride): boolean {
  return override.enabled === null && overrideChanges(override).length === 0;
}

export function describeOverride(override: GroupOverride): string {
  const parts: string[] = [];
  if (override.enabled === true) parts.push("单独开放");
  if (override.enabled === false) parts.push("单独关闭");
  const changed = overrideChanges(override).map((key) => OVERRIDE_FIELD_LABELS[key]);
  if (changed.length) parts.push(`改了${changed.join("、")}`);
  return parts.join("，") || "和全局一样";
}

const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;

export function isClock(value: string): boolean {
  return CLOCK.test(value);
}

export function describeQuiet(settings: Pick<StickerSettings, "quietEnabled" | "quietStart" | "quietEnd">): string {
  if (!settings.quietEnabled) return "没有安静时段";
  if (settings.quietStart === settings.quietEnd) return "全天不发表情包";
  const crosses = settings.quietStart > settings.quietEnd;
  return `${settings.quietStart} 到${crosses ? "第二天 " : " "}${settings.quietEnd} 不发表情包`;
}

/** “+25%”“-10%”“持平”；上一段是 0 时没法比。 */
export function trendText(trend: Trend | undefined): string {
  if (!trend || trend.changePercent === null) return trend && trend.recent > 0 ? "上一周期没有数据" : "";
  if (trend.changePercent === 0) return "和上一周期持平";
  return `比上一周期${trend.changePercent > 0 ? "多" : "少"} ${Math.abs(trend.changePercent)}%`;
}

/** 把每天的数字画成折线：返回 SVG polyline 的 points（0–100 × 0–height）。 */
export function sparkPoints(values: number[], height = 32): string {
  if (values.length === 0) return "";
  const top = Math.max(...values, 1);
  const step = values.length > 1 ? 100 / (values.length - 1) : 0;
  return values.map((value, index) => `${(index * step).toFixed(1)},${(height - (value / top) * (height - 2) - 1).toFixed(1)}`).join(" ");
}

export function historySeries(history: HistoryDay[], key: string): number[] {
  return history.map((row) => Number(row[key] ?? 0));
}

export function healthTone(score: number): "ok" | "warn" | "bad" {
  if (score >= 85) return "ok";
  return score >= 60 ? "warn" : "bad";
}

export const SEVERITY_LABELS: Record<HealthSeverity, string> = { error: "要处理", warn: "建议处理", info: "提示" };
export const SEVERITY_COLORS: Record<HealthSeverity, string> = { error: "red", warn: "gold", info: "blue" };

/** 回收站里的还剩几天自动清掉；trashDays=0 表示不自动清。 */
export function trashDaysLeft(deletedAt: string | null, trashDays: number, now: Date = new Date()): number | null {
  if (!deletedAt || trashDays <= 0) return null;
  const deleted = new Date(deletedAt.replace(" ", "T"));
  if (Number.isNaN(deleted.getTime())) return null;
  const left = trashDays - (now.getTime() - deleted.getTime()) / 86400000;
  return Math.max(0, Math.ceil(left));
}

export function formatEta(ms: number | null | undefined): string {
  if (typeof ms !== "number" || ms <= 0) return "";
  const seconds = Math.round(ms / 1000);
  return seconds >= 60 ? `大约还要 ${Math.ceil(seconds / 60)} 分钟` : `大约还要 ${Math.max(seconds, 1)} 秒`;
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
  "groupHourlyLimit",
  "minMessagesBetween",
  "stickerCooldownMinutes",
  "newBoostDays",
  "menuHintChars",
  "stealMinSide",
  "stealSenderDaily",
  "aiLabelConcurrency",
  "trashDays",
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
  groupHourlyLimit: { label: "每个群每小时最多发（0 = 不限）", min: 0, max: 1000, unit: "张" },
  minMessagesBetween: { label: "两张表情包之间至少先说（0 = 不限）", min: 0, max: 20, unit: "条文字" },
  stickerCooldownMinutes: { label: "同一张图在同一个群至少隔（0 = 不限）", min: 0, max: 1440, unit: "分钟" },
  newBoostDays: { label: "新入库的优先进菜单（0 = 不照顾）", min: 0, max: 30, unit: "天" },
  menuHintChars: { label: "菜单里每张的说明最多（0 = 只给名字）", min: 0, max: 30, unit: "字" },
  stealMinSide: { label: "短边小于这么多的不偷（0 = 不限）", min: 0, max: 512, unit: "像素" },
  stealSenderDaily: { label: "同一个人每天最多被偷（0 = 不限）", min: 0, max: 100, unit: "张" },
  aiLabelConcurrency: { label: "批量标注同时请求", min: 1, max: 4, unit: "张" },
  trashDays: { label: "删掉的在回收站留（0 = 直接删）", min: 0, max: 90, unit: "天" },
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
  if (!isClock(settings.quietStart)) errors.quietStart = "时间要写成 HH:MM";
  if (!isClock(settings.quietEnd)) errors.quietEnd = "时间要写成 HH:MM";
  if (!(settings.reviewBelowConfidence >= 0 && settings.reviewBelowConfidence <= 1)) errors.reviewBelowConfidence = "把握要在 0–1 之间";
  for (const [group, override] of Object.entries(settings.groupOverrides)) {
    if (!/^\d{4,12}$/.test(group)) errors.groupOverrides = `群号不对：${group}`;
    for (const key of ["groupCooldownSeconds", "groupDailyLimit", "groupHourlyLimit", "maxPerMessage", "menuSize"] as const) {
      const value = override[key];
      const range = STICKER_SETTING_RANGES[key];
      if (value !== null && value !== undefined && (!Number.isInteger(value) || value < range.min || value > range.max)) {
        errors.groupOverrides = `群 ${group} 的${OVERRIDE_FIELD_LABELS[key]}需要在 ${range.min}–${range.max} 之间`;
      }
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
  const hourly = settings.groupHourlyLimit > 0 ? `、每小时 ${settings.groupHourlyLimit} 张` : "";
  const quiet = settings.quietEnabled ? `；${describeQuiet(settings)}` : "";
  return `${scope}可用${cooldown}，每群每天最多 ${settings.groupDailyLimit} 张${hourly}${quiet}`;
}
