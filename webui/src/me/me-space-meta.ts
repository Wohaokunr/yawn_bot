// 「我的空间」2.0 的纯逻辑：日期文案、心情、筛选、分组、校验。和页面分开，方便单测。
import type { ChatMessageRow, NoteItem, TodoItem } from "./me-space-api";

/** 按北京时间的小时给一句问候 */
export function greeting(hour: number): string {
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

/** 待办截止：今天到期 / 明天到期 / 还有 N 天 / 逾期 N 天 */
export function dueLabel(dueInDays: number | null): string | null {
  if (dueInDays === null) return null;
  if (dueInDays < 0) return `逾期 ${-dueInDays} 天`;
  if (dueInDays === 0) return "今天到期";
  if (dueInDays === 1) return "明天到期";
  return `还有 ${dueInDays} 天`;
}

/** 倒数日：就是今天 / 还有 N 天 / 已过去 N 天 */
export function countdownLabel(daysLeft: number): string {
  if (daysLeft === 0) return "就是今天";
  return daysLeft > 0 ? `还有 ${daysLeft} 天` : `已过去 ${-daysLeft} 天`;
}

export const MOOD_OPTIONS: Array<{ score: number; emoji: string; label: string }> = [
  { score: 1, emoji: "😣", label: "很差" },
  { score: 2, emoji: "😕", label: "不太好" },
  { score: 3, emoji: "😐", label: "一般" },
  { score: 4, emoji: "🙂", label: "不错" },
  { score: 5, emoji: "😄", label: "很好" },
];

export function moodEmoji(score: number | null | undefined): string {
  return MOOD_OPTIONS.find((item) => item.score === Math.round(score ?? 0))?.emoji ?? "·";
}

export function trendLabel(trend: string | null | undefined): string {
  return trend === "up" ? "在变好" : trend === "down" ? "在变差" : trend === "flat" ? "比较平稳" : "记录还不够";
}

/** 心情按天聚合（同一天多条取平均），给最近 N 天的小柱图用；没记的天 score 为 null */
export function moodByDay(items: Array<{ at: string; score: number }>, today: string, days: number): Array<{ day: string; score: number | null }> {
  const sums = new Map<string, { total: number; count: number }>();
  for (const item of items) {
    const day = item.at.slice(0, 10);
    const row = sums.get(day) ?? { total: 0, count: 0 };
    row.total += item.score;
    row.count += 1;
    sums.set(day, row);
  }
  const base = new Date(`${today}T00:00:00Z`);
  const result: Array<{ day: string; score: number | null }> = [];
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const day = new Date(base.getTime() - offset * 86_400_000).toISOString().slice(0, 10);
    const row = sums.get(day);
    result.push({ day, score: row ? Math.round((row.total / row.count) * 10) / 10 : null });
  }
  return result;
}

export const PRIORITY_META: Record<TodoItem["priority"], { label: string; color: string }> = {
  high: { label: "重要", color: "red" },
  normal: { label: "普通", color: "default" },
  low: { label: "不急", color: "blue" },
};

export type TodoFilter = "open" | "today" | "overdue" | "done" | "all";

export interface TodoDraftEdit { content: string; priority: TodoItem["priority"]; due: string }

/** 只有填完整的日期（或清空）才算有效；输到一半的日期框是空串以外的残缺值或被浏览器报为空。 */
export const isCompleteDate = (value: string): boolean => value === "" || /^\d{4}-\d{2}-\d{2}$/.test(value);

/** 编辑框的草稿与原条目对比，只返回改动的字段；内容为空或日期不完整时返回 null（不能保存）。 */
export function todoChanges(item: TodoItem, draft: TodoDraftEdit): { content?: string; priority?: TodoItem["priority"]; due?: string } | null {
  const content = draft.content.trim();
  if (!content || !isCompleteDate(draft.due)) return null;
  const changes: { content?: string; priority?: TodoItem["priority"]; due?: string } = {};
  if (content !== item.content) changes.content = content;
  if (draft.priority !== item.priority) changes.priority = draft.priority;
  if (draft.due !== (item.due ?? "")) changes.due = draft.due;
  return changes;
}

export function filterTodos(items: TodoItem[], filter: TodoFilter, keyword = ""): TodoItem[] {
  const needle = keyword.trim().toLowerCase();
  return items.filter((item) => {
    if (needle && !item.content.toLowerCase().includes(needle)) return false;
    if (filter === "open") return !item.done;
    if (filter === "today") return !item.done && item.dueInDays !== null && item.dueInDays <= 0;
    if (filter === "overdue") return item.overdue;
    if (filter === "done") return item.done;
    return true;
  });
}

export function filterNotes(items: NoteItem[], keyword: string, tag: string | null): NoteItem[] {
  const needle = keyword.trim().toLowerCase();
  return items.filter((item) => (!tag || item.tag === tag) && (!needle || item.content.toLowerCase().includes(needle) || (item.tag ?? "").toLowerCase().includes(needle)));
}

/** HH:MM（00:00–23:59） */
export function isClock(raw: string): boolean {
  return /^([01]?\d|2[0-3])[:：][0-5]\d$/.test(raw.trim());
}

/** 生日只记月日：MM-DD */
export function isBirthday(raw: string): boolean {
  const match = /^(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])$/.exec(raw.trim());
  return match !== null;
}

/** 免打扰时段的文案：“23:00 到次日 08:00” */
export function quietLabel(start: string, end: string): string {
  if (!start || !end) return "没有设置";
  return start < end ? `${start} 到 ${end}` : `${start} 到次日 ${end}`;
}

/** 消息按天分组，给聊天记录加日期分隔 */
export function groupByDay(rows: ChatMessageRow[]): Array<{ day: string; rows: ChatMessageRow[] }> {
  const groups: Array<{ day: string; rows: ChatMessageRow[] }> = [];
  for (const row of rows) {
    const day = (row.at ?? "").slice(0, 10) || "未知日期";
    const last = groups.at(-1);
    if (last && last.day === day) last.rows.push(row);
    else groups.push({ day, rows: [row] });
  }
  return groups;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

export const TASK_STATE_COLOR: Record<string, string> = {
  queued: "default",
  running: "processing",
  blocked: "warning",
  done: "success",
  failed: "error",
  cancelled: "default",
};

export const OPEN_TASK_STATES = new Set(["queued", "running", "blocked"]);

export const DEATH_CAUSE_LABEL: Record<string, string> = {
  WOLF_KILL: "被狼刀",
  WITCH_POISON: "被毒",
  VOTED: "被投出",
  HUNTER_SHOT: "被猎人带走",
  SELF_DETONATION: "自爆",
  KNIGHT_KILL: "被骑士决斗",
  KNIGHT_DEATH: "决斗失败",
};

export const FACTION_LABEL: Record<string, string> = { wolf: "狼人阵营", good: "好人阵营" };

export const REPLY_LENGTH_OPTIONS = [
  { value: "short", label: "尽量简短" },
  { value: "normal", label: "正常" },
  { value: "long", label: "可以多说" },
] as const;

export const EMOJI_OPTIONS = [
  { value: "none", label: "不用表情" },
  { value: "some", label: "偶尔用" },
  { value: "lots", label: "多用点" },
] as const;

/** 打开某个“我的空间”子页时 Tabs 的 key 校验：不认识的回到默认 */
export function pickTab<T extends string>(raw: string | null, allowed: readonly T[], fallback: T): T {
  return raw && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}
