// 「我的空间」2.0 的接口（后端 me_space_routes.py / me_insight_routes.py，前缀 /me）。
// “本人”只认会话里的 QQ；这里没有任何可以指定别人的参数。
import { api } from "../api";

function json(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

const enc = encodeURIComponent;

// ── 今日概览 ──────────────────────────────────────────────────────────

export interface TodoItem {
  id: number;
  content: string;
  priority: "high" | "normal" | "low";
  priorityLabel: string;
  done: boolean;
  due: string | null;
  dueInDays: number | null;
  overdue: boolean;
  createdAt: string | null;
  doneAt: string | null;
}

export interface CountdownItem {
  id: number;
  title: string;
  date: string;
  daysLeft: number;
  status: "today" | "past" | "upcoming";
  notify: boolean;
}

export interface MoodItem {
  index: number;
  at: string;
  score: number;
  label: string;
  note: string;
}

export interface MoodSummary {
  days: number;
  count: number;
  average: number | null;
  averageLabel: string | null;
  trend: "up" | "down" | "flat" | null;
}

export interface Dashboard {
  now: string;
  todos: { pending: number; overdue: number; dueToday: number; top: TodoItem[] };
  reminders: Array<{ ref: string; content: string; due: string }>;
  countdowns: CountdownItem[];
  mood: { last: MoodItem | null; week: MoodSummary; loggedToday: boolean };
  affinity: { available: boolean; level: string; nextLevel: string | null; progress: number; today: number };
  checkin: {
    groups: number;
    checkedToday: number;
    bestStreak: number;
    pendingGroups: Array<{ groupId: string; groupName: string | null; streakDays: number }>;
  };
  chat: { enabled: boolean; loaded: boolean; paused: boolean; turnsToday: number; dailyTurns: number };
  tasks: { running: number; waitingYou: number; total: number };
  counts: Record<string, number>;
}

// ── 小本本 ────────────────────────────────────────────────────────────

export interface TodoList { items: TodoItem[]; pending: number; done: number; limit: number; added?: number; cleared?: number }
export interface NoteItem { id: number; content: string; tag: string | null; pinned: boolean; at: string | null; editedAt: string | null }
export interface NoteList { items: NoteItem[]; tags: string[]; limit: number; added?: number }
export interface CountdownList { items: CountdownItem[]; limit: number; added?: number }
export interface MoodList { items: MoodItem[]; week: MoodSummary; month: MoodSummary; scores: Record<string, string>; limit: number }

export interface TodoDraft { content: string; priority: TodoItem["priority"]; due?: string | null }
export interface TodoPatch { content?: string; priority?: TodoItem["priority"]; due?: string; done?: boolean }

// ── 私聊设置 / 主动提议 / 小助手 ──────────────────────────────────────

export interface ChatPrefs {
  replyLength: "short" | "normal" | "long";
  emoji: "none" | "some" | "lots";
  callMe: string;
  quietStart: string;
  quietEnd: string;
  morningGreeting: string;
  checkinNudge: string;
  birthday: string;
  proactive: boolean;
}

export interface ChatSettings {
  prefs: ChatPrefs;
  inQuietHours: boolean;
  agent: {
    enabled: boolean;
    loaded: boolean;
    autoReply: boolean;
    friendsOnly: boolean;
    paused: boolean;
    turnsToday: number;
    dailyTurns: number;
    proactiveAllowed: boolean;
  };
}

export interface OfferKind { kind: string; label: string; description: string; siteOn: boolean; mutedUntil: string | null }

export interface OfferState {
  siteEnabled: boolean;
  siteOpen: boolean;
  defaultOn: boolean;
  on: boolean;
  ownChoice: boolean;
  everyHours: number;
  defaultEveryHours: number;
  minEveryHours: number;
  pausedUntil: string | null;
  dailyMax: number;
  siteQuiet: string | null;
  kinds: OfferKind[];
  history: Array<{ id: string; at: string; status: string; statusLabel: string; about: string[]; outcome: string | null; outcomeLabel: string | null }>;
  stats: Record<string, number>;
}

export interface OfferPatch {
  enabled?: boolean;
  followDefault?: boolean;
  everyHours?: number;
  resetInterval?: boolean;
  snoozeHours?: number;
  muteKind?: string;
  muteDays?: number;
  unmuteKind?: string;
}

export interface HelperTask {
  id: string;
  title: string | null;
  goal: string | null;
  state: string;
  status: string;
  toolsets: string[];
  createdAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  progress: Array<{ at: string | null; note: string | null; step: number | null; total: number | null }>;
  summary: string | null;
  error: string | null;
  question: string | null;
  files: string[];
  reported: boolean;
  held: boolean;
}

export interface HelperState {
  tasksEnabled: boolean;
  taskLimits: { perUser: number; daily: number; timeoutSeconds: number };
  tasks: HelperTask[];
  tasksToday: number;
  computer: { workspace: string; runsToday: number; dailyLimit: number; inboxBytes: number } | null;
  projects: Array<{ id: string; title: string | null; status: string | null; members: Array<Record<string, unknown>> }>;
  cancelled?: boolean;
}

// ── Bot 记得我 ────────────────────────────────────────────────────────

export interface MemoryFact {
  key: string;
  label: string;
  content: string;
  source: string;
  sourceLabel: string;
  protected: boolean;
  updatedAt: string | null;
  expiresAt: string | null;
}

export interface MemoryState {
  available: boolean;
  enabled: boolean;
  shareEnabled: boolean;
  tombstoneDays: number;
  labels: Record<string, string>;
  listKeys: string[];
  facts: MemoryFact[];
  groupProfile: Array<{ key: string; label: string; content: string; groups: string[]; selfTold: boolean; asOf: string }>;
  settings: { groupsInPrivate: boolean; mutedGroups: string[]; shareKeys: string[]; shareGroups: string[] };
  shareableKeys: Array<{ key: string; label: string }>;
  groups: Array<{ groupId: string; name: string | null; agentReadable: boolean; inPrivate: boolean; seesShared: Array<{ label: string; content: string }> }>;
  tombstones: Array<{ what: string; keyword: string; scope: string; until: string | null; index: number }>;
  ledger: Array<{ at: string; what: string; key?: string; keys?: string[]; keyword?: string; group?: string; groups?: string[]; on?: boolean; by?: string }>;
  recap: { summary: string; openThreads: string[]; updatedAt: string | null } | null;
  result?: { removed: string[]; trimmed: string[]; protected: string[]; changed: boolean; blocked?: boolean };
}

export interface LinkPatch { groupsInPrivate?: boolean; shareKeys?: string[]; shareGroups?: string[]; mutedGroups?: string[] }

// ── 好感 / 群 / 聊天 / 战绩 ───────────────────────────────────────────

export interface AffinityEvent { id: number; group_id: number | null; source: string; source_label: string; delta: number; balance_after: number; note: string; day: string | null; created_at: string | null }

export interface AffinityState {
  userId: string;
  points: number;
  level: string;
  levelIndex: number;
  nextLevel: string | null;
  toNext: number | null;
  progress: number;
  today: number;
  available: boolean;
  levels: Array<{ floor: number; name: string }>;
  sources: Record<string, { label: string; dailyCap: number | null }>;
  breakdown30d: Record<string, number>;
  events: AffinityEvent[];
  groups: Array<{ groupId: string; groupName: string | null; groupAffinity: number; checkedToday: boolean; streakDays: number; totalDays: number; points: number; exp: number; lastCheckinDate: string | null }>;
  tones: Record<string, string>;
}

export interface JoinedGroup {
  groupId: string;
  groupName: string | null;
  role: string;
  managed: boolean;
  memberCount: number;
  lastActiveAt: string | null;
  firstSeenAt: string | null;
  agentEnabled: boolean;
  featuresOff: Array<{ key: string; label: string }>;
  checkin: { checkedToday: boolean; streakDays: number; streakBroken: boolean; totalDays: number; points: number; exp: number; groupAffinity: number; lastCheckinDate: string | null } | null;
}

export interface ChatRow {
  id: string;
  title: string | null;
  where: "private" | "group";
  groupId: string | null;
  groupName: string | null;
  messages: number;
  mine: number;
  createdAt: string | null;
  updatedAt: string | null;
  firstAt: string | null;
  lastAt: string | null;
  preview: string;
}

export interface ChatMessageRow { id: string; role: "user" | "assistant" | string; content: string; at: string | null }
export interface ChatHit { chatId: string; messageId: string; role: string; at: string | null; snippet: string; chatTitle: string | null; where: string }

export interface WerewolfGameRow {
  gameId: string;
  groupId: string;
  groupName: string | null;
  board: string | null;
  players: number;
  startedAt: string | null;
  endedAt: string | null;
  role: string;
  faction: string;
  seat: number;
  sheriff: boolean;
  won: boolean | null;
  deathRound: number | null;
  deathCause: string | null;
  rounds: number | null;
  winner: string | null;
}

export interface GameStats {
  available: boolean;
  games?: number;
  finished?: number;
  aborted?: number;
  wins?: number;
  winRate?: number | null;
  survived?: number;
  sheriff?: number;
  currentStreak?: number;
  bestStreak?: number;
  byFaction?: Record<string, { games: number; wins: number }>;
  byRole?: Array<{ role: string; games: number; wins: number }>;
  deathCauses?: Record<string, number>;
  recent?: WerewolfGameRow[];
}

export const EXPORT_URL = "/webui/api/v1/me/export";
export const chatExportUrl = (chatId: string): string => `/webui/api/v1/me/chats/${enc(chatId)}/export`;

export const spaceApi = {
  dashboard: () => api<Dashboard>("/me/dashboard").then((r) => r.data),

  todos: () => api<TodoList>("/me/assistant/todos").then((r) => r.data),
  addTodo: (draft: TodoDraft) => api<TodoList>("/me/assistant/todos", json("POST", { ...draft, due: draft.due || null })).then((r) => r.data),
  editTodo: (id: number, patch: TodoPatch) => api<TodoList>(`/me/assistant/todos/${id}`, json("PATCH", patch)).then((r) => r.data),
  deleteTodo: (id: number) => api<TodoList>(`/me/assistant/todos/${id}`, json("DELETE")).then((r) => r.data),
  clearDoneTodos: () => api<TodoList>("/me/assistant/todos/clear-done", json("POST")).then((r) => r.data),

  notes: () => api<NoteList>("/me/assistant/notes").then((r) => r.data),
  addNote: (content: string, tag?: string) => api<NoteList>("/me/assistant/notes", json("POST", { content, tag: tag || null })).then((r) => r.data),
  editNote: (id: number, patch: { content?: string; tag?: string; pinned?: boolean }) => api<NoteList>(`/me/assistant/notes/${id}`, json("PATCH", patch)).then((r) => r.data),
  deleteNote: (id: number) => api<NoteList>(`/me/assistant/notes/${id}`, json("DELETE")).then((r) => r.data),

  countdowns: () => api<CountdownList>("/me/assistant/countdowns").then((r) => r.data),
  addCountdown: (title: string, date: string, notify: boolean) => api<CountdownList>("/me/assistant/countdowns", json("POST", { title, date, notify })).then((r) => r.data),
  editCountdown: (id: number, patch: { title?: string; date?: string; notify?: boolean }) => api<CountdownList>(`/me/assistant/countdowns/${id}`, json("PATCH", patch)).then((r) => r.data),
  deleteCountdown: (id: number) => api<CountdownList>(`/me/assistant/countdowns/${id}`, json("DELETE")).then((r) => r.data),

  moods: () => api<MoodList>("/me/assistant/moods").then((r) => r.data),
  logMood: (score: number, note?: string) => api<MoodList>("/me/assistant/moods", json("POST", { score, note: note || null })).then((r) => r.data),
  deleteMood: (item: MoodItem) => api<MoodList>(`/me/assistant/moods/${item.index}?at=${enc(item.at)}`, json("DELETE")).then((r) => r.data),

  chat: () => api<ChatSettings>("/me/chat").then((r) => r.data),
  patchChatPrefs: (patch: Partial<ChatPrefs>) => api<ChatSettings>("/me/chat/prefs", json("PATCH", patch)).then((r) => r.data),
  pauseChat: (paused: boolean) => api<ChatSettings>("/me/chat/pause", json("POST", { paused })).then((r) => r.data),

  offers: () => api<OfferState>("/me/offers").then((r) => r.data),
  patchOffers: (patch: OfferPatch) => api<OfferState>("/me/offers", json("PATCH", patch)).then((r) => r.data),

  tasks: () => api<HelperState>("/me/tasks").then((r) => r.data),
  cancelTask: (id: string) => api<HelperState>(`/me/tasks/${enc(id)}`, json("DELETE")).then((r) => r.data),

  memory: () => api<MemoryState>("/me/memory").then((r) => r.data),
  addFact: (key: string, content: string) => api<MemoryState>("/me/memory/facts", json("POST", { key, content })).then((r) => r.data),
  forgetFact: (params: { key?: string; keyword?: string; block?: boolean; everywhere?: boolean }) =>
    api<MemoryState>(
      `/me/memory/facts?key=${enc(params.key ?? "")}&keyword=${enc(params.keyword ?? "")}${params.block ? "&block=true" : ""}${params.everywhere ? "&everywhere=true" : ""}`,
      json("DELETE"),
    ).then((r) => r.data),
  forgetAll: () => api<MemoryState>("/me/memory/all", json("DELETE")).then((r) => r.data),
  clearRecap: () => api<MemoryState>("/me/memory/recap", json("DELETE")).then((r) => r.data),
  dropTombstone: (index: number) => api<MemoryState>(`/me/memory/tombstones/${index}`, json("DELETE")).then((r) => r.data),
  putLink: (patch: LinkPatch) => api<MemoryState>("/me/memory/link", json("PUT", patch)).then((r) => r.data),

  affinity: () => api<AffinityState>("/me/affinity").then((r) => r.data),
  joinedGroups: () => api<JoinedGroup[]>("/me/joined-groups").then((r) => r.data),

  chats: () => api<ChatRow[]>("/me/chats").then((r) => r.data),
  chatMessages: (chatId: string, before?: string) =>
    api<ChatMessageRow[]>(`/me/chats/${enc(chatId)}/messages?limit=60${before ? `&before=${enc(before)}` : ""}`)
      .then((r) => ({ rows: r.data, hasMore: Boolean(r.meta.hasMore), before: (r.meta.before as string | null) ?? null })),
  searchChats: (q: string) => api<ChatHit[]>(`/me/chats/search?q=${enc(q)}`).then((r) => r.data),
  renameChat: (chatId: string, title: string) => api<{ id: string; title: string }>(`/me/chats/${enc(chatId)}`, json("PATCH", { title })).then((r) => r.data),
  deleteChat: (chatId: string) => api<{ deleted: string }>(`/me/chats/${enc(chatId)}`, json("DELETE")).then((r) => r.data),

  games: () => api<GameStats>("/me/games").then((r) => r.data),
};
