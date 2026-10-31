import { api } from "../api";
import type { FeatureState } from "../types";
import type {
  BatchResult,
  GroupDetail,
  GroupOverview,
  GroupRow,
  GroupsSummary,
  MemberOverride,
  MemberRow,
  UserProfile,
  UserRow,
  UsersSummary,
} from "./types";

export interface Paged<T> {
  rows: T[];
  total: number;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const patch = (override: boolean | null): RequestInit => ({ method: "PATCH", body: JSON.stringify({ override }) });

async function paged<T>(path: string, signal?: AbortSignal): Promise<Paged<T>> {
  const response = await api<T[]>(path, { signal });
  return { rows: response.data, total: response.meta.total ?? 0 };
}

export const directoryApi = {
  groups: (query: string, signal?: AbortSignal) => paged<GroupRow>(`/groups?${query}`, signal),
  groupsSummary: (signal?: AbortSignal) => api<GroupsSummary>("/groups/summary", { signal }).then((r) => r.data),
  group: (groupId: string, signal?: AbortSignal) => api<GroupDetail>(`/groups/${groupId}`, { signal }).then((r) => r.data),
  groupOverview: (groupId: string, signal?: AbortSignal) =>
    api<GroupOverview>(`/groups/${groupId}/overview`, { signal }).then((r) => r.data),
  groupOverrides: (groupId: string, signal?: AbortSignal) =>
    api<MemberOverride[]>(`/groups/${groupId}/overrides`, { signal }).then((r) => r.data),
  members: (groupId: string, query: string, signal?: AbortSignal) => paged<MemberRow>(`/groups/${groupId}/members?${query}`, signal),
  setGroupFeature: (groupId: string, feature: string, override: boolean | null) =>
    api<FeatureState>(`/groups/${groupId}/features/${feature}`, patch(override)).then((r) => r.data),
  resetGroupFeatures: (groupId: string, includeAgent = false) =>
    api<{ removed: string[]; features: FeatureState[] }>(`/groups/${groupId}/features/reset`, json({ includeAgent })).then((r) => r.data),
  batchGroups: (ids: string[], feature: string, override: boolean | null) =>
    api<BatchResult>("/groups/batch/features", json({ ids, feature, override })).then((r) => r.data),
  memberFeatures: (groupId: string, userId: string, signal?: AbortSignal) =>
    api<FeatureState[]>(`/groups/${groupId}/members/${userId}/features`, { signal }).then((r) => r.data),
  setMemberFeature: (groupId: string, userId: string, feature: string, override: boolean | null) =>
    api<FeatureState>(`/groups/${groupId}/members/${userId}/features/${feature}`, patch(override)).then((r) => r.data),
  resetMemberFeatures: (groupId: string, userId: string) =>
    api<{ removed: number; features: FeatureState[] }>(`/groups/${groupId}/members/${userId}/features`, { method: "DELETE" }).then((r) => r.data),
  batchMembers: (groupId: string, ids: string[], feature: string, override: boolean | null) =>
    api<BatchResult>(`/groups/${groupId}/members/batch/features`, json({ ids, feature, override })).then((r) => r.data),
  users: (query: string, signal?: AbortSignal) => paged<UserRow>(`/users?${query}`, signal),
  usersSummary: (signal?: AbortSignal) => api<UsersSummary>("/users/summary", { signal }).then((r) => r.data),
  user: (userId: string, signal?: AbortSignal) => api<UserProfile>(`/users/${userId}`, { signal }).then((r) => r.data),
  userFeatures: (userId: string, signal?: AbortSignal) =>
    api<FeatureState[]>(`/users/${userId}/features`, { signal }).then((r) => r.data),
  setUserFeature: (userId: string, feature: string, override: boolean | null) =>
    api<FeatureState>(`/users/${userId}/features/${feature}`, patch(override)).then((r) => r.data),
  batchUsers: (ids: string[], feature: string, override: boolean | null) =>
    api<BatchResult>("/users/batch/features", json({ ids, feature, override })).then((r) => r.data),
};
