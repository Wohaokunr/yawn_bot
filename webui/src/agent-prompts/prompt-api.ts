import { api } from "../api";
import type {
  PromptCatalog,
  PromptHistoryEntry,
  PromptMode,
  PromptOverridesExport,
  PromptScope,
  PromptSnapshot,
  TracePrompts,
} from "./prompt-meta";

export interface PromptStatus {
  promptVersion: string;
  revision: number;
  updatedAt: string | null;
  storeFile: string;
  loadError: string | null;
  scopes: Record<string, number>;
  capture: { enabled: boolean; perTrace: number; snapshotChars: number; tracesWithPrompts: number };
}

function scopeQuery(scope: PromptScope, groupId: string, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ scope, ...extra });
  if (scope === "group") params.set("groupId", groupId);
  return params.toString();
}

export async function fetchPromptCatalog(groupId: string, signal?: AbortSignal): Promise<PromptCatalog> {
  return (await api<PromptCatalog>(`/agent/prompts/catalog?groupId=${encodeURIComponent(groupId)}`, { signal })).data;
}

export async function fetchPromptStatus(signal?: AbortSignal): Promise<PromptStatus> {
  return (await api<PromptStatus>("/agent/prompts/status", { signal })).data;
}

export async function saveOverride(
  sectionId: string,
  body: { scope: PromptScope; groupId: string; mode: PromptMode; text: string; note: string; confirmSafety: boolean; expectedRevision: number },
): Promise<{ revision: number }> {
  const payload = {
    ...body,
    groupId: body.scope === "group" ? Number(body.groupId) : undefined,
  };
  return (await api<{ revision: number }>(`/agent/prompts/sections/${encodeURIComponent(sectionId)}`, { method: "PUT", body: JSON.stringify(payload) })).data;
}

export async function clearOverride(sectionId: string, scope: PromptScope, groupId: string, expectedRevision: number): Promise<{ revision: number }> {
  const query = scopeQuery(scope, groupId, { expectedRevision: String(expectedRevision) });
  return (await api<{ revision: number }>(`/agent/prompts/sections/${encodeURIComponent(sectionId)}?${query}`, { method: "DELETE" })).data;
}

export async function fetchHistory(sectionId: string, scope: PromptScope, groupId: string): Promise<PromptHistoryEntry[]> {
  return (await api<PromptHistoryEntry[]>(`/agent/prompts/sections/${encodeURIComponent(sectionId)}/history?${scopeQuery(scope, groupId)}`)).data;
}

export async function bulkApply(body: {
  scope: PromptScope;
  groupId: string;
  changes: Array<{ sectionId: string; mode: string; text?: string; note?: string }>;
  confirmSafety: boolean;
  expectedRevision: number;
  replaceScope?: boolean;
}): Promise<{ revision: number; changed: number }> {
  const payload = { ...body, groupId: body.scope === "group" ? Number(body.groupId) : undefined };
  return (await api<{ revision: number; changed: number }>("/agent/prompts/bulk", { method: "POST", body: JSON.stringify(payload) })).data;
}

export async function exportOverrides(scope: PromptScope, groupId: string): Promise<PromptOverridesExport> {
  return (await api<PromptOverridesExport>(`/agent/prompts/export?${scopeQuery(scope, groupId)}`)).data;
}

export async function fetchTracePrompts(groupId: string, traceId: string, signal?: AbortSignal): Promise<TracePrompts> {
  return (await api<TracePrompts>(`/agent/groups/${groupId}/execution-traces/${encodeURIComponent(traceId)}/prompts`, { signal })).data;
}

export async function fetchBackgroundPrompts(signal?: AbortSignal): Promise<{ captureEnabled: boolean; prompts: PromptSnapshot[] }> {
  return (await api<{ captureEnabled: boolean; prompts: PromptSnapshot[] }>("/agent/prompts/background", { signal })).data;
}
