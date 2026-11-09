// 好感玩法接口：管理台 /affinity/*、/users/{id}/affinity/*，个人页 /me/affinity/*。
import { api } from "../api";
import type { ActionResponse, AffinityPolicy, AffinityStats, PerksState, PolicyPayload } from "./types";

const enc = encodeURIComponent;
const post = (body?: unknown): RequestInit => (body === undefined ? { method: "POST" } : { method: "POST", body: JSON.stringify(body) });

export const affinityAdminApi = {
  policy: () => api<PolicyPayload>("/affinity/policy").then((r) => r.data),
  savePolicy: (policy: AffinityPolicy) => api<PolicyPayload>("/affinity/policy", { method: "PUT", body: JSON.stringify({ policy }) }).then((r) => r.data),
  resetPolicy: () => api<PolicyPayload>("/affinity/policy/reset", post()).then((r) => r.data),
  stats: () => api<AffinityStats>("/affinity/stats").then((r) => r.data),
  runDecay: () => api<{ affected: number; total: number }>("/affinity/decay/run", post()).then((r) => r.data),
  userPerks: (userId: string) => api<PerksState>(`/users/${enc(userId)}/affinity/perks`).then((r) => r.data),
  grantItem: (userId: string, item: string, count: number, note = "") =>
    api<PerksState>(`/users/${enc(userId)}/affinity/items`, post({ item, count, note })).then((r) => r.data),
  clearNickname: (userId: string) => api<PerksState>(`/users/${enc(userId)}/affinity/nickname`, { method: "DELETE" }).then((r) => r.data),
};

export const affinityMeApi = {
  perks: () => api<PerksState>("/me/affinity/perks").then((r) => r.data),
  gift: (gift: string, count: number, groupId?: string | null) =>
    api<ActionResponse>("/me/affinity/gift", post({ gift, count, groupId: groupId ?? null })).then((r) => r.data),
  buy: (item: string, count: number) => api<ActionResponse>("/me/affinity/buy", post({ item, count })).then((r) => r.data),
  use: (item: string, groupId?: string | null) => api<ActionResponse>("/me/affinity/use", post({ item, groupId: groupId ?? null })).then((r) => r.data),
  interact: (action: string) => api<ActionResponse>("/me/affinity/interact", post({ action })).then((r) => r.data),
  nickname: (nickname: string) => api<ActionResponse>("/me/affinity/nickname", { method: "PUT", body: JSON.stringify({ nickname }) }).then((r) => r.data),
  claim: () => api<ActionResponse>("/me/affinity/quests/claim", post()).then((r) => r.data),
};
