// 「我的空间」3.0 的纯逻辑：随手记解析、待办分组与排序、月历格子、热力档位、
// 心情统计、周对比、关键词高亮、快捷键。和页面分开，方便单测。
import type { MoodItem, NoteItem, TodoItem, WerewolfGameRow, GameStats } from "./me-space-api";
import type { InsightWindow } from "./me-hub-api";

const DAY_MS = 86_400_000;

/** 北京时间的今天（YYYY-MM-DD） */
export function todayIso(now: number = Date.now()): string {
  return new Date(now + 8 * 3_600_000).toISOString().slice(0, 10);
}

export function addDays(iso: string, days: number): string {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
}

export function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / DAY_MS);
}

/** 0 = 周一 … 6 = 周日 */
export function weekdayOf(iso: string): number {
  return (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7;
}

export const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"] as const;

// ── 随手记：一句话里带上日期和优先级 ──────────────────────────────────

export interface QuickTodo { content: string; priority: TodoItem["priority"]; due: string | null; hints: string[] }

const WEEKDAY_WORD: Record<string, number> = { 一: 0, 二: 1, 三: 2, 四: 3, 五: 4, 六: 5, 日: 6, 天: 6 };

function validDate(year: number, month: number, day: number): string | null {
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return date.toISOString().slice(0, 10);
}

/**
 * “明天 交报告 !重要” → { content: "交报告", due: 明天, priority: high }。
 * 认得：今天/明天/后天/大后天、N天后、周X/下周X、M月D日、MM-DD、YYYY-MM-DD；!重要 !高 !! / !不急 !低。
 * 只吃句首或句尾、用空格隔开的词，免得把“明天见面礼”里的“明天”吃掉。
 */
export function parseQuickTodo(raw: string, today: string): QuickTodo {
  let priority: TodoItem["priority"] = "normal";
  let due: string | null = null;
  const hints: string[] = [];
  const words = raw.trim().split(/\s+/).filter(Boolean);
  const keep: string[] = [];
  words.forEach((word, index) => {
    const edge = index === 0 || index === words.length - 1;
    if (/^[!！](重要|高|紧急|[!！])$/.test(word)) { priority = "high"; hints.push("重要"); return; }
    if (/^[!！](不急|低)$/.test(word)) { priority = "low"; hints.push("不急"); return; }
    if (!edge || due !== null) { keep.push(word); return; }
    const relative: Record<string, number> = { 今天: 0, 今晚: 0, 明天: 1, 明早: 1, 后天: 2, 大后天: 3 };
    let found: string | null = null;
    if (word in relative) found = addDays(today, relative[word]);
    const later = /^(\d{1,3})天后$/.exec(word);
    if (later) found = addDays(today, Number(later[1]));
    const week = /^(下)?(?:周|星期)([一二三四五六日天])$/.exec(word);
    if (week) {
      const target = WEEKDAY_WORD[week[2]];
      let delta = target - weekdayOf(today);
      if (week[1]) delta += 7;
      else if (delta < 0) delta += 7;
      found = addDays(today, delta);
    }
    const cn = /^(\d{1,2})月(\d{1,2})[日号]?$/.exec(word);
    const md = /^(\d{1,2})[-/](\d{1,2})$/.exec(word);
    const monthDay = cn ?? md;
    if (monthDay) {
      const year = Number(today.slice(0, 4));
      const thisYear = validDate(year, Number(monthDay[1]), Number(monthDay[2]));
      found = thisYear && thisYear < today ? validDate(year + 1, Number(monthDay[1]), Number(monthDay[2])) : thisYear;
    }
    const full = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(word);
    if (full) found = validDate(Number(full[1]), Number(full[2]), Number(full[3]));
    if (found) { due = found; hints.push(found === today ? "今天" : found.slice(5)); return; }
    keep.push(word);
  });
  const content = keep.join(" ");
  // 整句都被吃掉时原样保留，不产生空待办
  if (!content) return { content: raw.trim(), priority: "normal", due: null, hints: [] };
  return { content, priority, due, hints };
}

// ── 待办：分组、排序、导出 ─────────────────────────────────────────────

export type TodoBucket = "overdue" | "today" | "tomorrow" | "week" | "later" | "someday" | "done";

export const BUCKET_LABEL: Record<TodoBucket, string> = {
  overdue: "逾期",
  today: "今天",
  tomorrow: "明天",
  week: "这七天",
  later: "以后",
  someday: "没设日期",
  done: "已完成",
};

export function bucketOf(item: TodoItem): TodoBucket {
  if (item.done) return "done";
  if (item.dueInDays === null) return "someday";
  if (item.dueInDays < 0) return "overdue";
  if (item.dueInDays === 0) return "today";
  if (item.dueInDays === 1) return "tomorrow";
  return item.dueInDays <= 7 ? "week" : "later";
}

export function groupTodos(items: TodoItem[]): Array<{ bucket: TodoBucket; label: string; items: TodoItem[] }> {
  const order: TodoBucket[] = ["overdue", "today", "tomorrow", "week", "later", "someday", "done"];
  return order
    .map((bucket) => ({ bucket, label: BUCKET_LABEL[bucket], items: items.filter((item) => bucketOf(item) === bucket) }))
    .filter((group) => group.items.length > 0);
}

export type TodoSort = "smart" | "due" | "priority" | "newest";

const PRIORITY_RANK: Record<TodoItem["priority"], number> = { high: 0, normal: 1, low: 2 };

export function sortTodos(items: TodoItem[], mode: TodoSort): TodoItem[] {
  if (mode === "smart") return items;
  const copy = [...items];
  const due = (item: TodoItem): string => item.due ?? "9999-99-99";
  if (mode === "due") copy.sort((a, b) => Number(a.done) - Number(b.done) || due(a).localeCompare(due(b)) || a.id - b.id);
  if (mode === "priority") copy.sort((a, b) => Number(a.done) - Number(b.done) || PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] || due(a).localeCompare(due(b)));
  if (mode === "newest") copy.sort((a, b) => b.id - a.id);
  return copy;
}

export function todosToMarkdown(items: TodoItem[]): string {
  return items.map((item) => `- [${item.done ? "x" : " "}] ${item.content}${item.priority === "high" ? " ❗" : ""}${item.due ? ` （${item.due}）` : ""}`).join("\n");
}

export function notesToMarkdown(items: NoteItem[]): string {
  return items.map((item) => `${item.tag ? `**#${item.tag}** ` : ""}${item.content}${item.at ? `\n\n_${item.at}_` : ""}`).join("\n\n---\n\n");
}

/** 完成进度：已完成 / (已完成 + 未完成) */
export function completionPercent(done: number, pending: number): number {
  const total = done + pending;
  return total ? Math.round((done / total) * 100) : 0;
}

export type NoteSort = "pinned" | "newest" | "oldest" | "edited";

export function sortNotes(items: NoteItem[], mode: NoteSort): NoteItem[] {
  if (mode === "pinned") return items;
  const copy = [...items];
  if (mode === "newest") copy.sort((a, b) => b.id - a.id);
  if (mode === "oldest") copy.sort((a, b) => a.id - b.id);
  if (mode === "edited") copy.sort((a, b) => (b.editedAt ?? b.at ?? "").localeCompare(a.editedAt ?? a.at ?? ""));
  return copy;
}

// ── 月历 ───────────────────────────────────────────────────────────────

/** 把一个月的天排成周一开头的格子，前后用 null 补齐整周 */
export function calendarCells<T>(firstWeekday: number, days: T[]): Array<T | null> {
  const cells: Array<T | null> = [...Array.from({ length: firstWeekday }, () => null), ...days];
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

export function shiftMonth(month: string, delta: number): string {
  const [year, value] = month.split("-").map(Number);
  const date = new Date(Date.UTC(year, value - 1 + delta, 1));
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function monthLabel(month: string): string {
  const [year, value] = month.split("-");
  return `${year} 年 ${Number(value)} 月`;
}

// ── 热力、对比、图表 ───────────────────────────────────────────────────

/** 0（没有）到 4（最多）的热力档位 */
export function heatLevel(value: number, max: number): 0 | 1 | 2 | 3 | 4 {
  if (value <= 0 || max <= 0) return 0;
  const ratio = value / max;
  if (ratio > 0.75) return 4;
  if (ratio > 0.5) return 3;
  if (ratio > 0.25) return 2;
  return 1;
}

/** 最近 N 天排成按周的列（每列周一到周日），给签到热力图用；不在范围内的格子是 null */
export function heatColumns(today: string, days: number, values: Record<string, number>): Array<Array<{ day: string; value: number } | null>> {
  const start = addDays(today, -(days - 1));
  const columns: Array<Array<{ day: string; value: number } | null>> = [];
  let column: Array<{ day: string; value: number } | null> = Array.from({ length: weekdayOf(start) }, () => null);
  for (let offset = 0; offset < days; offset += 1) {
    const day = addDays(start, offset);
    column.push({ day, value: values[day] ?? 0 });
    if (column.length === 7) { columns.push(column); column = []; }
  }
  if (column.length) {
    while (column.length < 7) column.push(null);
    columns.push(column);
  }
  return columns;
}

export interface Delta { diff: number; direction: "up" | "down" | "flat"; label: string }

export function compare(current: number, previous: number | null | undefined): Delta {
  if (previous === null || previous === undefined) return { diff: 0, direction: "flat", label: "—" };
  const diff = Math.round((current - previous) * 10) / 10;
  if (diff === 0) return { diff, direction: "flat", label: "和上周一样" };
  return { diff, direction: diff > 0 ? "up" : "down", label: `比上周${diff > 0 ? "多" : "少"} ${Math.abs(diff)}` };
}

export function insightHighlights(week: InsightWindow, last: InsightWindow | null, busiestHour: number | null, activeStreak: number): string[] {
  const lines: string[] = [];
  if (week.activeDays >= 6) lines.push(`这周 ${week.activeDays} 天都来过，几乎全勤`);
  else if (week.activeDays > 0) lines.push(`这周来了 ${week.activeDays} 天`);
  if (last && week.mine > last.mine && last.mine > 0) lines.push(`聊天比上周多了 ${Math.round(((week.mine - last.mine) / last.mine) * 100)}%`);
  if (week.todosDone > 0) lines.push(`这周完成了 ${week.todosDone} 件待办`);
  if (week.mood !== null && last?.mood !== null && last?.mood !== undefined) {
    if (week.mood - last.mood >= 0.5) lines.push("心情比上周好");
    else if (last.mood - week.mood >= 0.5) lines.push("心情比上周低一些，照顾好自己");
  }
  if (busiestHour !== null) lines.push(`你最常在 ${hourLabel(busiestHour)} 找 Bot`);
  if (activeStreak >= 3) lines.push(`最长连续活跃 ${activeStreak} 天`);
  return lines;
}

export function hourLabel(hour: number): string {
  const part = hour < 5 ? "凌晨" : hour < 9 ? "早上" : hour < 12 ? "上午" : hour < 14 ? "中午" : hour < 18 ? "下午" : hour < 23 ? "晚上" : "深夜";
  return `${part} ${hour} 点`;
}

/** 把一组数画成 SVG 折线的 points（宽 width、高 height，留 2px 边） */
export function sparkPoints(values: number[], width: number, height: number): string {
  if (values.length === 0) return "";
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? (width - 4) / (values.length - 1) : 0;
  return values.map((value, index) => `${(2 + index * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`).join(" ");
}

// ── 心情 ───────────────────────────────────────────────────────────────

export function moodDistribution(items: MoodItem[]): Array<{ score: number; count: number; percent: number }> {
  const total = items.length || 1;
  return [5, 4, 3, 2, 1].map((score) => {
    const count = items.filter((item) => item.score === score).length;
    return { score, count, percent: Math.round((count / total) * 100) };
  });
}

/** 周一到周日各自的平均心情；没记过的是 null */
export function moodByWeekday(items: MoodItem[]): Array<number | null> {
  const sums = Array.from({ length: 7 }, () => ({ total: 0, count: 0 }));
  for (const item of items) {
    const slot = sums[weekdayOf(item.at.slice(0, 10))];
    slot.total += item.score;
    slot.count += 1;
  }
  return sums.map((slot) => (slot.count ? Math.round((slot.total / slot.count) * 10) / 10 : null));
}

/** 到今天（或昨天）为止连续记了几天 */
export function moodStreak(items: MoodItem[], today: string): number {
  const days = new Set(items.map((item) => item.at.slice(0, 10)));
  let cursor = days.has(today) ? today : addDays(today, -1);
  let streak = 0;
  while (days.has(cursor)) {
    streak += 1;
    cursor = addDays(cursor, -1);
  }
  return streak;
}

// ── 文本 ───────────────────────────────────────────────────────────────

/** 把文本按关键词切开，命中的段 hit=true（不区分大小写） */
export function highlightParts(text: string, needle: string): Array<{ text: string; hit: boolean }> {
  const key = needle.trim();
  if (!key) return [{ text, hit: false }];
  const parts: Array<{ text: string; hit: boolean }> = [];
  const lower = text.toLowerCase();
  const target = key.toLowerCase();
  let from = 0;
  for (;;) {
    const at = lower.indexOf(target, from);
    if (at < 0) break;
    if (at > from) parts.push({ text: text.slice(from, at), hit: false });
    parts.push({ text: text.slice(at, at + key.length), hit: true });
    from = at + key.length;
  }
  if (from < text.length) parts.push({ text: text.slice(from), hit: false });
  return parts;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.round((seconds % 3600) / 60)} 分`;
}

/** 两个 “YYYY-MM-DD HH:MM[:SS]” / ISO 时间之间的秒数；缺一个就是 null */
export function secondsBetween(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const a = Date.parse(start.replace(" ", "T"));
  const b = Date.parse(end.replace(" ", "T"));
  return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 1000) : null;
}

/** 随机生成一个好记又够强的密码：四段小写 + 数字 + 符号 */
export function suggestPassword(random: (max: number) => number = (max) => crypto.getRandomValues(new Uint32Array(1))[0] % max): string {
  const consonants = "bcdfghjkmnpqrstvwxyz";
  const vowels = "aeiou";
  const syllable = (): string => consonants[random(consonants.length)] + vowels[random(vowels.length)];
  const word = (): string => syllable() + syllable();
  const symbols = "-_.+";
  return `${word()}${symbols[random(symbols.length)]}${word()}${symbols[random(symbols.length)]}${10 + random(90)}`;
}

// ── 狼人杀 ─────────────────────────────────────────────────────────────

/** 最近几局的走势：W 胜 / L 负 / - 没打完，最旧的在前 */
export function recentForm(rows: WerewolfGameRow[], count = 10): Array<"W" | "L" | "-"> {
  return rows.slice(0, count).map((row): "W" | "L" | "-" => (row.won === null || !row.endedAt ? "-" : row.won ? "W" : "L")).reverse();
}

export function werewolfTitle(stats: GameStats): string {
  const finished = stats.finished ?? 0;
  const rate = stats.winRate ?? 0;
  if (finished < 3) return "新手村民";
  const wolf = stats.byFaction?.wolf;
  const good = stats.byFaction?.good;
  if ((stats.bestStreak ?? 0) >= 5) return "连胜机器";
  if (wolf && wolf.games >= 3 && wolf.wins / wolf.games >= 0.7) return "影帝级狼人";
  if (good && good.games >= 3 && good.wins / good.games >= 0.7) return "村里的明灯";
  if (finished && (stats.survived ?? 0) / finished >= 0.6) return "苟到最后";
  if (rate >= 0.55) return "稳定上分";
  if (rate < 0.35) return "越挫越勇";
  return "老练玩家";
}

export type GameFilter = { faction: "all" | "wolf" | "good"; result: "all" | "win" | "lose" | "open"; board: string };

export function filterGames(rows: WerewolfGameRow[], filter: GameFilter): WerewolfGameRow[] {
  return rows.filter((row) => {
    if (filter.faction !== "all" && row.faction !== filter.faction) return false;
    if (filter.board && row.board !== filter.board) return false;
    const open = row.won === null || !row.endedAt;
    if (filter.result === "open") return open;
    if (filter.result === "win") return !open && row.won === true;
    if (filter.result === "lose") return !open && row.won === false;
    return true;
  });
}

// ── 新手引导 ───────────────────────────────────────────────────────────

export interface OnboardingStep { key: string; title: string; hint: string; to: string; done: boolean }

export function onboardingSteps(input: { todos: number; notes: number; moods: number; checkedToday: number; callMe: boolean; hasFacts: boolean; levelIndex: number }): OnboardingStep[] {
  return [
    { key: "todo", title: "记下第一条待办", hint: "按 N 随时随手记", to: "/me/assistant", done: input.todos > 0 },
    { key: "checkin", title: "今天签个到", hint: "网页上就能一键签完所有群", to: "/me/affinity", done: input.checkedToday > 0 },
    { key: "mood", title: "记一次心情", hint: "主页右边点个表情就行", to: "/me/assistant?tab=moods", done: input.moods > 0 },
    { key: "call", title: "告诉 Bot 怎么称呼你", hint: "私聊设置里填一下", to: "/me/chat", done: input.callMe },
    { key: "memory", title: "看看 Bot 记得你什么", hint: "不想让它记的随时让它忘掉", to: "/me/memory", done: input.hasFacts },
    { key: "level", title: "和 Bot 混个面熟", hint: "签到、聊天、送礼都会涨好感", to: "/me/affinity?tab=perks", done: input.levelIndex >= 1 },
  ];
}

// ── 快捷键 ─────────────────────────────────────────────────────────────

/** 先按 G 再按这些键跳转 */
export const GO_KEYS: Record<string, { to: string; label: string }> = {
  h: { to: "/me", label: "主页" },
  a: { to: "/me/assistant", label: "小本本" },
  c: { to: "/me/calendar", label: "日历" },
  s: { to: "/me/schedules", label: "定时任务" },
  t: { to: "/me/tasks", label: "后台小助手" },
  m: { to: "/me/memory", label: "Bot 记得我" },
  l: { to: "/me/chats", label: "聊天记录" },
  g: { to: "/me/groups", label: "我在的群" },
  f: { to: "/me/affinity", label: "好感与签到" },
  i: { to: "/me/insights", label: "数据洞察" },
  b: { to: "/me/achievements", label: "成就" },
  u: { to: "/me/account", label: "账号" },
};

export const SHORTCUTS: Array<{ keys: string; what: string }> = [
  { keys: "Ctrl K", what: "搜索页面" },
  { keys: "/", what: "搜我自己的东西" },
  { keys: "N", what: "随手记（待办 / 笔记 / 心情 / 倒数日）" },
  { keys: "G 然后 字母", what: "跳到某一页" },
  { keys: "?", what: "打开这张快捷键表" },
];

/** 正在输入框里打字时不触发快捷键 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
