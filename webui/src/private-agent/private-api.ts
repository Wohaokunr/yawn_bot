// 「私聊 Agent」页的接口与类型：私聊提示词（全局 / 本人）、每用户人设、最近请求。
import { api } from "../api";
import type { PromptHistoryEntry, PromptMode, PromptOverride, PromptSample, PromptSnapshot } from "../agent-prompts/prompt-meta";
import type { PersonaCard, PersonaPreset } from "../types";

export type PrivateScope = "global" | "user";

export interface PrivatePipelineRef {
  pipeline: string;
  label: string;
  where: string;
  condition: string;
}

export interface PrivateSectionRow {
  id: string;
  title: string;
  group: string;
  groupLabel: string;
  category: string;
  layer: string;
  layerLabel: string;
  kind: "static" | "generated" | "header" | "data";
  description: string;
  when: string;
  safety: boolean;
  protocol: boolean;
  editable: boolean;
  modes: PromptMode[];
  defaultText: string | null;
  defaultTokens: number;
  sample: PromptSample | null;
  globalOverride: PromptOverride | null;
  userOverride: PromptOverride | null;
  userOverrideCount: number;
  effectiveScope: "global" | "user" | "draft" | null;
  effectiveMode: PromptMode | "default";
  effectiveText: string | null;
  effectiveTokens: number;
  pipelines: PrivatePipelineRef[];
}

export interface PrivatePipelineStep {
  sectionId: string;
  title: string;
  where: string;
  condition: string;
  mode: string;
  scope: string | null;
  safety: boolean;
  tokens: number;
}

export interface PrivatePipeline {
  key: string;
  label: string;
  description: string;
  trigger: string;
  tools: "main" | "worker" | "tasks_only" | "none";
  steps: PrivatePipelineStep[];
  toolSections: string[];
  tokens: { always: number; conditional: number; tools: number };
}

export interface PrivatePromptIssue {
  level: "danger" | "warning" | "info";
  code: string;
  sectionId: string;
  message: string;
  scope: string;
}

export interface PrivatePromptCatalog {
  revision: number;
  updatedAt: string | null;
  loadError: string | null;
  userId: string | null;
  groups: Array<{ key: string; label: string; hint: string }>;
  pipelines: PrivatePipeline[];
  sections: PrivateSectionRow[];
  issues: PrivatePromptIssue[];
  summary: {
    total: number;
    globalOverrides: number;
    userOverrides: number;
    usersWithOverrides: number;
    disabled: number;
    safetyChanged: number;
  };
}

export interface PrivateOverrideUser {
  userId: string;
  count: number;
  sections: string[];
  safety: number;
  updatedAt: string | null;
}

export interface PrivateSnapshot extends PromptSnapshot {
  userId?: string;
  pipeline?: string | null;
  persona?: { source: string; label: string; name: string | null; templateName: string | null } | null;
  flags?: { computer: boolean; tasks: boolean; allowSilence: boolean; history: number };
}

export interface CacheCall {
  at: number;
  round: number;
  user_id?: number;
  changed?: string;
  cached?: number;
  miss?: number;
  hit_rate?: number | null;
}

export interface CachePipelineStats {
  calls: number;
  reported: number;
  promptTokens: number;
  cachedTokens: number;
  missTokens: number;
  hitRate: number | null;
  recent: CacheCall[];
}

export interface CacheStats {
  pipelines: Record<string, CachePipelineStats>;
  cachedTokens: number;
  missTokens: number;
  hitRate: number | null;
  anchoredUsers: number;
}

export interface CaptureStatus {
  captureEnabled: boolean;
  paused: boolean;
  perUser: number;
  maxUsers: number;
}

export interface CaptureUser {
  userId: string;
  count: number;
  lastAt: string | null;
  lastPipeline: string | null;
}

export type PersonaMode = "follow" | "custom" | "library";

export interface PersonaPolicy {
  enabled: boolean;
  selfEdit: boolean;
  selfEditDaily: number;
  selfFields: string[];
  allowPresets: boolean;
  allowLibrary: boolean;
  defaultTemplateId: string | null;
  revision: number;
  updatedAt: string | null;
  updatedBy: string | null;
}

export interface PersonaOptions {
  presets: PersonaPreset[];
  library: Array<{ id: string; name: string; summary: string }>;
  fields: Record<string, string>;
  promptTokenLimit: number;
}

export interface PersonaUserRow {
  userId: string;
  mode: PersonaMode;
  source: string;
  sourceLabel: string;
  name: string;
  summary: string;
  templateName: string | null;
  missingTemplate: boolean;
  locked: boolean;
  revision: number;
  updatedAt: string | null;
  updatedBy: string | null;
  selfEditsToday: number;
}

export interface PersonaStats {
  custom: number;
  library: number;
  follow: number;
  locked: number;
  missingTemplate: number;
  selfEditsToday: number;
}

export interface PersonaHistoryItem {
  at: string;
  by: "self" | "admin" | string;
  actor: string | null;
  action: string;
  changed: string[];
  /** 这次修改之前的样子（撤销会回到这里）。 */
  before: { mode: PersonaMode | null; templateName: string | null; summary: string | null };
}

export interface PersonaUserState {
  userId: string;
  mode: PersonaMode;
  card: PersonaCard;
  effectiveCard: PersonaCard;
  source: string;
  sourceLabel: string;
  personal: boolean;
  templateId: string | null;
  templateName: string | null;
  missingTemplate: boolean;
  suspended: boolean;
  summary: string;
  compiled: string;
  promptTokens: number;
  promptTokenLimit: number;
  trimmed: boolean;
  locked: boolean;
  revision: number;
  updatedAt: string | null;
  updatedBy: string | null;
  selfEditsToday: number;
  selfEditDaily: number;
  callThem: string | null;
  callThemSource: "chat_prefs" | "persona" | null;
  history: PersonaHistoryItem[];
}

export interface PersonaPreviewResult {
  compiled: string;
  promptTokens: number;
  promptTokenLimit: number;
  trimmed: boolean;
  summary: string;
  unsafe: string | null;
}

const BASE = "/private-agent";

function scopeQuery(scope: PrivateScope, userId: string, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ scope, ...extra });
  if (scope === "user") params.set("userId", userId);
  return params.toString();
}

function json(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

async function data<T>(path: string, init?: RequestInit): Promise<T> {
  return (await api<T>(path, init)).data;
}

export const privateApi = {
  catalog: (userId: string, signal?: AbortSignal) =>
    data<PrivatePromptCatalog>(`${BASE}/prompts/catalog${userId ? `?userId=${encodeURIComponent(userId)}` : ""}`, { signal }),
  saveOverride: (
    sectionId: string,
    body: { scope: PrivateScope; userId: string; mode: PromptMode; text: string; note: string; confirmSafety: boolean; expectedRevision: number },
  ) => data<{ revision: number }>(`${BASE}/prompts/sections/${encodeURIComponent(sectionId)}`, json("PUT", {
    ...body,
    userId: body.scope === "user" ? Number(body.userId) : undefined,
  })),
  clearOverride: (sectionId: string, scope: PrivateScope, userId: string, expectedRevision: number) =>
    data<{ revision: number }>(`${BASE}/prompts/sections/${encodeURIComponent(sectionId)}?${scopeQuery(scope, userId, { expectedRevision: String(expectedRevision) })}`, json("DELETE")),
  history: (sectionId: string, scope: PrivateScope, userId: string) =>
    data<PromptHistoryEntry[]>(`${BASE}/prompts/sections/${encodeURIComponent(sectionId)}/history?${scopeQuery(scope, userId)}`),
  bulk: (body: { scope: PrivateScope; userId: string; changes: Array<{ sectionId: string; mode: string; text?: string; note?: string }>; confirmSafety: boolean; expectedRevision: number; replaceScope?: boolean }) =>
    data<{ revision: number; changed: number }>(`${BASE}/prompts/bulk`, json("POST", { ...body, userId: body.scope === "user" ? Number(body.userId) : undefined })),
  exportOverrides: (scope: PrivateScope, userId: string) =>
    data<{ kind: string; scope: string; overrides: Record<string, PromptOverride> }>(`${BASE}/prompts/export?${scopeQuery(scope, userId)}`),
  preview: (body: {
    userId: string;
    message: string;
    globalDrafts?: Record<string, { mode: string; text: string }>;
    userDrafts?: Record<string, { mode: string; text: string }>;
    personaCard?: PersonaCard | null;
    computer?: boolean;
    tasks?: boolean | null;
    includeHistory?: boolean;
  }) => data<PrivateSnapshot>(`${BASE}/prompts/preview`, json("POST", { ...body, userId: Number(body.userId) })),
  overrideUsers: (signal?: AbortSignal) => data<PrivateOverrideUser[]>(`${BASE}/prompts/users`, { signal }),
  promote: (userId: string, body: { sectionIds?: string[]; keepUser?: boolean; confirmSafety?: boolean; expectedRevision?: number }) =>
    data<{ revision: number; promoted: string[] }>(`${BASE}/prompts/users/${encodeURIComponent(userId)}/promote`, json("POST", body)),
  resetUser: (userId: string, expectedRevision: number) =>
    data<{ revision: number; changed: number }>(`${BASE}/prompts/users/${encodeURIComponent(userId)}?expectedRevision=${expectedRevision}`, json("DELETE")),

  cacheStats: (signal?: AbortSignal) => data<CacheStats>(`${BASE}/cache`, { signal }),
  captures: (signal?: AbortSignal) => data<CaptureStatus & { users: CaptureUser[] }>(`${BASE}/captures`, { signal }),
  userCaptures: (userId: string, signal?: AbortSignal) =>
    data<CaptureStatus & { userId: string; prompts: PrivateSnapshot[] }>(`${BASE}/captures/${encodeURIComponent(userId)}`, { signal }),
  clearCaptures: (userId?: string) =>
    data<{ cleared: number }>(`${BASE}/captures${userId ? `?userId=${encodeURIComponent(userId)}` : ""}`, json("DELETE")),
  setCapturePaused: (paused: boolean) => data<CaptureStatus>(`${BASE}/captures/settings`, json("PUT", { paused })),

  policy: (signal?: AbortSignal) => data<{ policy: PersonaPolicy; options: PersonaOptions }>(`${BASE}/persona/policy`, { signal }),
  savePolicy: (patch: Partial<Omit<PersonaPolicy, "revision" | "updatedAt" | "updatedBy">> & { expectedRevision: number }) =>
    data<{ policy: PersonaPolicy }>(`${BASE}/persona/policy`, json("PUT", patch)),
  personaUsers: (query: { q?: string; source?: string }, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (query.q) params.set("q", query.q);
    if (query.source) params.set("source", query.source);
    const suffix = params.toString();
    return data<{ users: PersonaUserRow[]; stats: PersonaStats; templateUsage: Record<string, number>; policy: PersonaPolicy; options: PersonaOptions }>(
      `${BASE}/persona/users${suffix ? `?${suffix}` : ""}`,
      { signal },
    );
  },
  personaUser: (userId: string, signal?: AbortSignal) => data<PersonaUserState>(`${BASE}/persona/users/${encodeURIComponent(userId)}`, { signal }),
  setCard: (userId: string, card: PersonaCard, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}`, json("PUT", { card, expectedRevision })),
  useTemplate: (userId: string, templateId: string, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/template`, json("POST", { templateId: Number(templateId), expectedRevision })),
  usePreset: (userId: string, presetId: string, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/preset`, json("POST", { presetId, expectedRevision })),
  reset: (userId: string, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/reset`, json("POST", { expectedRevision })),
  undo: (userId: string) => data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/undo`, json("POST")),
  lock: (userId: string, locked: boolean) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/lock`, json("PUT", { locked })),
  clear: (userId: string) => data<{ deleted: boolean }>(`${BASE}/persona/users/${encodeURIComponent(userId)}`, json("DELETE")),
  exportPersona: (userId: string) => data<Record<string, unknown>>(`${BASE}/persona/users/${encodeURIComponent(userId)}/export`),
  importPersona: (userId: string, payload: Record<string, unknown>, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/import`, json("POST", { ...payload, expectedRevision })),
  saveAsTemplate: (userId: string, name: string, switchUser: boolean) =>
    data<{ state: PersonaUserState; template: { id: string; name: string } }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/save-as-template`, json("POST", { name, switchUser })),
  copyGroup: (userId: string, groupId: string, expectedRevision: number) =>
    data<{ state: PersonaUserState }>(`${BASE}/persona/users/${encodeURIComponent(userId)}/copy-group`, json("POST", { groupId: Number(groupId), expectedRevision })),
  batch: (userIds: string[], action: "reset" | "lock" | "unlock" | "clear") =>
    data<{ done: string[]; failed: Array<{ userId: string; error: string }> }>(`${BASE}/persona/batch`, json("POST", { userIds: userIds.map(Number), action })),
  previewPersona: (card: PersonaCard) => data<PersonaPreviewResult>(`${BASE}/persona/preview`, json("POST", { card })),
};
