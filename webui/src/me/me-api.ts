// QQ 个人账号自助接口（后端 self_service_routes.py，前缀 /me）。
// “本人”只认会话里的 QQ，接口里没有可以指定别人的参数。
import { api } from "../api";
import type { FeatureState, PersonaCard, PersonaPreset } from "../types";
import type { PersonaHistoryItem, PersonaMode } from "../private-agent/private-api";

export interface MyAccount {
  userId: string;
  hasPassword: boolean;
  disabled: boolean;
  createdAt: string | null;
  passwordChangedAt: string | null;
  lastLoginAt: string | null;
  setupPending: boolean;
  passwordSource: string | null;
}

export interface MyProfile {
  userId: string;
  nickname: string | null;
  account: MyAccount | null;
  managedGroupCount: number;
  groupCount: number;
  persona: { sourceLabel: string; summary: string; name: string | null; locked: boolean } | null;
  sessionExpiresAt: number;
}

export interface MyGroupRow {
  groupId: string;
  groupName: string | null;
  role: "owner" | "admin" | string;
  memberCount: number;
  lastActiveAt: string | null;
  enabledFeatures: number;
  featureCount: number;
  agentEnabled: boolean;
}

export interface MyGroupDetail {
  groupId: string;
  groupName: string | null;
  memberCount: number;
  lastActiveAt: string | null;
  role: string;
  /** Bot 在线并实时确认过管理员身份；false 时只能看不能改 */
  verified: boolean;
  features: FeatureState[];
  featureHints: Record<string, string>;
}

export interface MyPersonaState {
  userId: string;
  mode: PersonaMode;
  card: PersonaCard;
  effectiveCard: PersonaCard;
  source: string;
  sourceLabel: string;
  templateId: string | null;
  templateName: string | null;
  summary: string;
  compiled: string;
  promptTokens: number;
  promptTokenLimit: number;
  locked: boolean;
  revision: number;
  updatedAt: string | null;
  selfEditsToday: number;
  selfEditDaily: number;
  callThem: string | null;
  callThemSource: "chat_prefs" | "persona" | null;
  history: Array<Omit<PersonaHistoryItem, "actor">>;
  canEdit: boolean;
  blockedReason: string | null;
  editsLeftToday: number;
  options: {
    presets: PersonaPreset[];
    library: Array<{ id: string; name: string; summary: string }>;
    /** 本人能改的字段：snake_case → 中文名 */
    fields: Record<string, string>;
  };
}

export interface PersonaWriteResult {
  revision: number;
  changed: string[];
  summary: string;
  state: MyPersonaState;
}

function json(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

export const meApi = {
  profile: () => api<MyProfile>("/me").then((r) => r.data),
  groups: () => api<MyGroupRow[]>("/me/groups").then((r) => r.data),
  group: (groupId: string) => api<MyGroupDetail>(`/me/groups/${encodeURIComponent(groupId)}`).then((r) => r.data),
  setFeature: (groupId: string, feature: string, override: boolean | null) =>
    api<FeatureState>(`/me/groups/${encodeURIComponent(groupId)}/features/${encodeURIComponent(feature)}`, json("PATCH", { override })).then((r) => r.data),
  persona: () => api<MyPersonaState>("/me/persona").then((r) => r.data),
  patchPersona: (fields: Record<string, unknown>) => api<PersonaWriteResult>("/me/persona", json("PATCH", { fields })).then((r) => r.data),
  usePreset: (id: string) => api<PersonaWriteResult>("/me/persona/preset", json("POST", { id })).then((r) => r.data),
  useTemplate: (id: string) => api<PersonaWriteResult>("/me/persona/template", json("POST", { id })).then((r) => r.data),
  resetPersona: () => api<PersonaWriteResult>("/me/persona/reset", json("POST")).then((r) => r.data),
  undoPersona: () => api<PersonaWriteResult>("/me/persona/undo", json("POST")).then((r) => r.data),
  changePassword: (currentPassword: string, newPassword: string) =>
    api<{ account: MyAccount; csrfToken: string; expiresAt: number }>("/me/password", json("POST", { currentPassword, newPassword })).then((r) => r.data),
  logoutAll: () => api<{ authenticated: false }>("/me/logout-all", json("POST")).then((r) => r.data),
};
