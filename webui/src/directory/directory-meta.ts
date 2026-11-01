// 群组 / 用户页的纯逻辑：URL 状态解析、文案映射、时间与覆盖推导。不依赖 React，便于单测。
import type { FeatureState } from "../types";
import type { FeatureSource, MemberOverride } from "./types";

// ── 选项表 ────────────────────────────────────────────────────

export const FEATURE_LABELS: Record<string, string> = {
  checkin: "签到",
  info: "个人信息",
  ai_chat: "Yawn对话",
  werewolf: "狼人杀",
  rpg: "跑团",
  reminder: "定时提醒",
  fanqie: "番茄小说",
  group_agent: "群聊 Agent",
};

export function featureLabel(key: string, fallback?: string): string {
  return FEATURE_LABELS[key] ?? fallback ?? key;
}

export const ROLE_META: Record<string, { label: string; color?: string; order: number }> = {
  owner: { label: "群主", color: "gold", order: 0 },
  admin: { label: "管理员", color: "blue", order: 1 },
  member: { label: "成员", order: 2 },
};

export function roleMeta(role: string | null | undefined): { label: string; color?: string; order: number } {
  return ROLE_META[role ?? "member"] ?? { label: role || "成员", order: 3 };
}

export const SOURCE_LABELS: Record<FeatureSource, string> = {
  default: "默认",
  group: "群设置",
  user: "成员覆盖",
  global_user: "私聊覆盖",
  agent_config: "Agent 总开关",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source as FeatureSource] ?? source;
}

export type ActivityWindow = "" | "24h" | "7d" | "30d" | "dormant";
export const ACTIVITY_OPTIONS: { value: ActivityWindow; label: string }[] = [
  { value: "", label: "不限活跃" },
  { value: "24h", label: "24 小时内活跃" },
  { value: "7d", label: "7 天内活跃" },
  { value: "30d", label: "30 天内活跃" },
  { value: "dormant", label: "30 天以上没动静" },
];

export const GROUP_SORT_OPTIONS = [
  { value: "active", label: "最近活跃" },
  { value: "members", label: "成员最多" },
  { value: "checkins", label: "今日签到最多" },
  { value: "first_seen", label: "最近加入" },
  { value: "name", label: "群名" },
] as const;
export type GroupSort = (typeof GROUP_SORT_OPTIONS)[number]["value"];

export const GROUP_OVERRIDE_OPTIONS = [
  { value: "", label: "不限覆盖" },
  { value: "any", label: "有任意覆盖" },
  { value: "group", label: "群功能有覆盖" },
  { value: "member", label: "有成员级覆盖" },
] as const;
export type GroupOverrideFilter = (typeof GROUP_OVERRIDE_OPTIONS)[number]["value"];

export const MEMBER_SORT_OPTIONS = [
  { value: "recent", label: "最近出现" },
  { value: "streak", label: "连签最长" },
  { value: "affinity", label: "群好感最高" },
  { value: "joined", label: "最近入群" },
  { value: "name", label: "昵称" },
] as const;
export type MemberSort = (typeof MEMBER_SORT_OPTIONS)[number]["value"];

export const USER_SORT_OPTIONS = [
  { value: "recent", label: "最近互动" },
  { value: "affinity", label: "好感最高" },
  { value: "groups", label: "所在群最多" },
  { value: "first_seen", label: "最近认识" },
] as const;
export type UserSort = (typeof USER_SORT_OPTIONS)[number]["value"];

export const USER_OVERRIDE_OPTIONS = [
  { value: "", label: "不限覆盖" },
  { value: "any", label: "有任意覆盖" },
  { value: "private", label: "有私聊覆盖" },
  { value: "member", label: "有群内覆盖" },
] as const;
export type UserOverrideFilter = (typeof USER_OVERRIDE_OPTIONS)[number]["value"];

function pick<T extends string>(value: string | null, allowed: readonly { value: T }[], fallback: T): T {
  return allowed.some((option) => option.value === value) ? (value as T) : fallback;
}

function positiveInt(value: string | null): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

// ── URL 状态 ──────────────────────────────────────────────────

export interface GroupFilters {
  q: string;
  agent: "" | "on" | "off";
  overrides: GroupOverrideFilter;
  active: ActivityWindow;
  sort: GroupSort;
  page: number;
}

export function parseGroupFilters(params: URLSearchParams): GroupFilters {
  const agent = params.get("groups.agent");
  return {
    q: (params.get("groups.q") ?? "").slice(0, 120),
    agent: agent === "on" || agent === "off" ? agent : "",
    overrides: pick(params.get("groups.overrides"), GROUP_OVERRIDE_OPTIONS, ""),
    active: pick(params.get("groups.active"), ACTIVITY_OPTIONS, ""),
    sort: pick(params.get("groups.sort"), GROUP_SORT_OPTIONS, "active"),
    page: positiveInt(params.get("groups.page")),
  };
}

export function groupFiltersToParams(filters: Partial<GroupFilters>): Record<string, string | null> {
  return {
    "groups.q": filters.q ?? null,
    "groups.agent": filters.agent || null,
    "groups.overrides": filters.overrides || null,
    "groups.active": filters.active || null,
    "groups.sort": filters.sort && filters.sort !== "active" ? filters.sort : null,
    "groups.page": filters.page && filters.page > 1 ? String(filters.page) : null,
  };
}

export function groupListQuery(filters: GroupFilters, pageSize = 20): string {
  const query = new URLSearchParams({ page: String(filters.page), pageSize: String(pageSize), sort: filters.sort });
  if (filters.q) query.set("search", filters.q);
  if (filters.agent) query.set("agent", filters.agent);
  if (filters.overrides) query.set("overrides", filters.overrides);
  if (filters.active) query.set("active", filters.active);
  return query.toString();
}

export function activeFilterCount(filters: { q?: string; [key: string]: unknown }, keys: string[]): number {
  return keys.filter((key) => {
    const value = filters[key];
    return typeof value === "boolean" ? value : Boolean(value);
  }).length;
}

export interface MemberFilters {
  q: string;
  role: "" | "owner" | "admin" | "member";
  activity: ActivityWindow;
  overrides: boolean;
  sort: MemberSort;
  page: number;
}

export function parseMemberFilters(params: URLSearchParams): MemberFilters {
  const role = params.get("members.role");
  return {
    q: (params.get("members.q") ?? "").slice(0, 120),
    role: role === "owner" || role === "admin" || role === "member" ? role : "",
    activity: pick(params.get("members.active"), ACTIVITY_OPTIONS, ""),
    overrides: params.get("members.overrides") === "1",
    sort: pick(params.get("members.sort"), MEMBER_SORT_OPTIONS, "recent"),
    page: positiveInt(params.get("members.page")),
  };
}

export function memberFiltersToParams(filters: Partial<MemberFilters>): Record<string, string | null> {
  return {
    "members.q": filters.q ?? null,
    "members.role": filters.role || null,
    "members.active": filters.activity || null,
    "members.overrides": filters.overrides ? "1" : null,
    "members.sort": filters.sort && filters.sort !== "recent" ? filters.sort : null,
    "members.page": filters.page && filters.page > 1 ? String(filters.page) : null,
  };
}

export function memberListQuery(filters: MemberFilters, pageSize = 20): string {
  const query = new URLSearchParams({ page: String(filters.page), pageSize: String(pageSize), sort: filters.sort });
  if (filters.q) query.set("search", filters.q);
  if (filters.role) query.set("role", filters.role);
  if (filters.activity) query.set("activity", filters.activity);
  if (filters.overrides) query.set("overrides", "true");
  return query.toString();
}

export interface UserFilters {
  q: string;
  overrides: UserOverrideFilter;
  activity: ActivityWindow;
  sort: UserSort;
  page: number;
}

export function parseUserFilters(params: URLSearchParams): UserFilters {
  return {
    q: (params.get("users.q") ?? "").slice(0, 120),
    overrides: pick(params.get("users.overrides"), USER_OVERRIDE_OPTIONS, ""),
    activity: pick(params.get("users.active"), ACTIVITY_OPTIONS, ""),
    sort: pick(params.get("users.sort"), USER_SORT_OPTIONS, "recent"),
    page: positiveInt(params.get("users.page")),
  };
}

export function userFiltersToParams(filters: Partial<UserFilters>): Record<string, string | null> {
  return {
    "users.q": filters.q ?? null,
    "users.overrides": filters.overrides || null,
    "users.active": filters.activity || null,
    "users.sort": filters.sort && filters.sort !== "recent" ? filters.sort : null,
    "users.page": filters.page && filters.page > 1 ? String(filters.page) : null,
  };
}

export function userListQuery(filters: UserFilters, pageSize = 20): string {
  const query = new URLSearchParams({ page: String(filters.page), pageSize: String(pageSize), sort: filters.sort });
  if (filters.q) query.set("search", filters.q);
  if (filters.overrides) query.set("overrides", filters.overrides);
  if (filters.activity) query.set("activity", filters.activity);
  return query.toString();
}

export const GROUP_TABS = ["overview", "features", "members", "overrides"] as const;
export type GroupTab = (typeof GROUP_TABS)[number];

export function parseGroupTab(value: string | null): GroupTab {
  return (GROUP_TABS as readonly string[]).includes(value ?? "") ? (value as GroupTab) : "overview";
}

export const PROFILE_TABS = ["group", "groups", "private", "affinity", "agent"] as const;
export type ProfileTab = (typeof PROFILE_TABS)[number];

// 在群里打开档案默认看“本群”，在用户页打开默认看“所在群”。
export function parseProfileTab(value: string | null, hasGroup: boolean): ProfileTab {
  const fallback: ProfileTab = hasGroup ? "group" : "groups";
  if (!(PROFILE_TABS as readonly string[]).includes(value ?? "")) return fallback;
  if (value === "group" && !hasGroup) return "groups";
  return value as ProfileTab;
}

export function updateParams(current: URLSearchParams, patch: Record<string, string | null | undefined>): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const [key, value] of Object.entries(patch)) {
    if (value == null || value === "") next.delete(key);
    else next.set(key, value);
  }
  return next;
}

// ── 时间 ──────────────────────────────────────────────────────

export function relativeTime(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return "从未";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "—";
  const diff = Math.max(0, now - time);
  const minute = 60_000;
  if (diff < minute) return "刚刚";
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  const days = Math.floor(diff / (24 * 60 * minute));
  if (days < 30) return `${days} 天前`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

export type ActivityTone = "hot" | "warm" | "quiet" | "cold";

// 按最近活跃时间给个冷热度，用于列表左侧的小圆点。
export function activityTone(value: string | null | undefined, now: number = Date.now()): ActivityTone {
  if (!value) return "cold";
  const diff = now - new Date(value).getTime();
  const hour = 3_600_000;
  if (diff < hour) return "hot";
  if (diff < 24 * hour) return "warm";
  if (diff < 30 * 24 * hour) return "quiet";
  return "cold";
}

export const ACTIVITY_TONE_LABEL: Record<ActivityTone, string> = {
  hot: "1 小时内活跃",
  warm: "今天活跃过",
  quiet: "近 30 天活跃过",
  cold: "30 天以上没动静",
};

// ── 名称 ──────────────────────────────────────────────────────

export function groupDisplayName(group: { groupName?: string | null; groupId: string }): string {
  return group.groupName?.trim() || `未命名群 ${group.groupId}`;
}

export function memberDisplayName(member: { groupNickname?: string | null; nickname?: string | null; userId: string }): string {
  return member.groupNickname?.trim() || member.nickname?.trim() || `QQ ${member.userId}`;
}

// ── 功能覆盖 ──────────────────────────────────────────────────

export type FeatureMode = "inherit" | "on" | "off";

export function featureMode(override: boolean | null): FeatureMode {
  return override === null ? "inherit" : override ? "on" : "off";
}

export function modeToOverride(mode: FeatureMode): boolean | null {
  return mode === "inherit" ? null : mode === "on";
}

/**
 * “继承”最终会落到开还是关。
 * - 群功能：继承 = 默认开启。
 * - 成员功能：继承 = 跟随群设置（群里关了就是关）。
 * - 私聊功能：继承 = 默认开启。
 */
export function inheritedValue(featureKey: string, parent?: Pick<FeatureState, "key" | "effective">[]): boolean {
  if (!parent) return true;
  return parent.find((row) => row.key === featureKey)?.effective ?? true;
}

export function overrideCount(rows: Pick<FeatureState, "override">[]): number {
  return rows.filter((row) => row.override !== null).length;
}

export function redundantOverrides(rows: MemberOverride[]): MemberOverride[] {
  return rows.filter((row) => row.redundant);
}

// 覆盖一览按成员聚合，方便“这个人被单独设了哪些”一眼看完。
export function groupOverridesByMember(rows: MemberOverride[]): { userId: string; name: string; role: string; items: MemberOverride[] }[] {
  const map = new Map<string, { userId: string; name: string; role: string; items: MemberOverride[] }>();
  for (const row of rows) {
    const entry = map.get(row.userId) ?? { userId: row.userId, name: row.name, role: row.role, items: [] };
    entry.items.push(row);
    map.set(row.userId, entry);
  }
  return [...map.values()].sort((a, b) => b.items.length - a.items.length || a.name.localeCompare(b.name, "zh-CN"));
}

export function batchSummary(count: number, feature: string, override: boolean | null): string {
  const action = override === null ? "恢复继承" : override ? "显式开启" : "显式关闭";
  return `把 ${count} 项的「${featureLabel(feature)}」设为${action}`;
}

export const BATCH_LIMIT = 100;

export function percent(part: number, total: number): number {
  return total > 0 ? Math.round((part / total) * 100) : 0;
}
