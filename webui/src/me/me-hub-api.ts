// 「我的空间」3.0 的接口（后端 me_hub_routes.py，前缀 /me）：提醒中心、全站搜索、月历、
// 网页签到、成就、数据洞察、待办批量、清空某一类、跟着账号走的网页偏好。
import { api } from "../api";
import type { TodoItem, TodoList } from "./me-space-api";

function json(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

const enc = encodeURIComponent;

export type NoticeTone = "danger" | "warning" | "success" | "info";
export interface Notice { id: string; kind: string; tone: NoticeTone; title: string; detail: string; to: string; count: number }
export interface Inbox { now: string; items: Notice[]; total: number; badges: Record<string, number> }

export interface SearchItem { id: string; title: string; sub: string; to: string }
export interface SearchGroup { kind: string; label: string; total: number; more?: boolean; items: SearchItem[] }
export interface SearchResult { q: string; total: number; groups: SearchGroup[] }

export interface CalendarDay {
  date: string;
  todos: Array<{ id: number; content: string; done: boolean; priority: TodoItem["priority"] }>;
  done: number;
  reminders: Array<{ ref: string; content: string; time: string; repeat: boolean }>;
  countdowns: Array<{ id: number; title: string }>;
  mood: number | null;
  moodCount: number;
  checkins: number;
  affinity: number;
}
export interface CalendarMonth {
  month: string;
  today: string;
  /** 这个月 1 号是周几：0 = 周一 */
  firstWeekday: number;
  days: CalendarDay[];
  totals: { todos: number; done: number; reminders: number; countdowns: number; moodDays: number; checkinDays: number; affinity: number };
}

export interface CheckinRow {
  groupId: string;
  groupName: string;
  status: "ok" | "already" | "disabled";
  streakDays: number;
  reward?: number;
  bonus?: number;
  lucky?: boolean;
  expGain?: number;
  affinityGain?: number;
  milestone?: number | null;
  levelUp?: string | null;
  rankToday?: number;
}
export interface CheckinOutcome {
  results: CheckinRow[];
  signed: number;
  already: number;
  disabled: number;
  reward: number;
  affinityGain: number;
  levelUp: string | null;
  quests: { claimed: string[]; affinity: number; bonus_item: string | null };
}
export interface CheckinHistory { today: string; since: string; days: Array<{ day: string; groups: number; reward: number }>; activeDays: number; anyStreak: number; reward: number }

export interface Achievement { key: string; title: string; description: string; icon: string; value: number; target: number; done: boolean; unlockedAt: string | null; progress: number }
export interface Achievements {
  total: number;
  unlocked: number;
  fresh: string[];
  groups: Array<{ label: string; unlocked: number; items: Achievement[] }>;
  next: Achievement[];
  metrics: Record<string, number>;
}

export interface InsightDay { day: string; mine: number; bot: number; todosAdded: number; todosDone: number; mood: number | null; checkins: number; affinity: number }
export interface InsightWindow { mine: number; todosDone: number; checkins: number; affinity: number; activeDays: number; mood: number | null }
export interface GroupRank { groupId: string; groupName: string | null; members: number; groupAffinity: number; affinityRank: number | null; points: number; pointsRank: number | null; streakDays: number; totalDays: number }
export interface Insights {
  days: number;
  today: string;
  series: InsightDay[];
  hours: number[];
  weekdays: number[];
  totals: InsightWindow;
  thisWeek: InsightWindow;
  lastWeek: InsightWindow | null;
  busiestHour: number | null;
  bestDay: { day: string; mine: number } | null;
  activeStreak: number;
  ranks: GroupRank[];
}

export interface UiPrefs {
  homeHidden: string[];
  homeOrder: string[];
  landing: string;
  pinnedChats: string[];
  starred: string[];
  onboardingDismissed: boolean;
  seenAchievements: string[];
  seenNotices: string[];
  homeBlocks: string[];
  landings: string[];
}
export type UiPrefsPatch = Partial<Pick<UiPrefs, "homeHidden" | "homeOrder" | "landing" | "pinnedChats" | "starred" | "onboardingDismissed" | "seenAchievements" | "seenNotices">>;

export type TodoBatchAction = "done" | "undone" | "delete" | "postpone" | "priority" | "due";
export type ClearSection = "todos" | "notes" | "countdowns" | "moods";

export const hubApi = {
  inbox: () => api<Inbox>("/me/inbox").then((r) => r.data),
  search: (q: string) => api<SearchResult>(`/me/search?q=${enc(q)}`).then((r) => r.data),
  calendar: (month?: string) => api<CalendarMonth>(`/me/calendar${month ? `?month=${enc(month)}` : ""}`).then((r) => r.data),
  checkin: (groupId?: string) => api<CheckinOutcome>("/me/checkin", json("POST", groupId ? { groupId } : {})).then((r) => r.data),
  checkinHistory: (days = 120) => api<CheckinHistory>(`/me/checkin/history?days=${days}`).then((r) => r.data),
  achievements: () => api<Achievements>("/me/achievements").then((r) => r.data),
  insights: (days = 30) => api<Insights>(`/me/insights?days=${days}`).then((r) => r.data),
  uiPrefs: () => api<UiPrefs>("/me/ui-prefs").then((r) => r.data),
  putUiPrefs: (patch: UiPrefsPatch) => api<UiPrefs>("/me/ui-prefs", json("PUT", patch)).then((r) => r.data),
  batchTodos: (ids: number[], action: TodoBatchAction, extra: { days?: number; priority?: TodoItem["priority"]; due?: string } = {}) =>
    api<TodoList & { affected: number }>("/me/assistant/todos/batch", json("POST", { ids, action, ...extra })).then((r) => r.data),
  bulkTodos: (lines: string[], priority: TodoItem["priority"] = "normal", due?: string) =>
    api<TodoList & { added: number; skipped: Array<{ line: string; reason: string }> }>("/me/assistant/todos/bulk", json("POST", { lines, priority, due: due || null })).then((r) => r.data),
  clearSection: (section: ClearSection, confirm: string) =>
    api<{ section: ClearSection; cleared: number; counts: Record<string, number> }>("/me/assistant/clear", json("POST", { section, confirm })).then((r) => r.data),
};

/** 签到结果一句话：给 message 用 */
export function checkinSummary(outcome: CheckinOutcome): string {
  if (outcome.signed === 0) {
    if (outcome.disabled && !outcome.already) return "这些群没开签到";
    return "今天都签过啦";
  }
  const parts = [`签了 ${outcome.signed} 个群`, `积分 +${outcome.reward}`];
  if (outcome.affinityGain) parts.push(`好感 +${outcome.affinityGain}`);
  if (outcome.levelUp) parts.push(`升到「${outcome.levelUp}」`);
  if (outcome.quests.claimed.length) parts.push(`完成任务：${outcome.quests.claimed.join("、")}`);
  return parts.join("，");
}
