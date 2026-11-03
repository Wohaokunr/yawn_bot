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
  groupManage?: GroupManageFlags;
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
  groupManage?: GroupManageFlags;
}

/** 群管代办总开关（后端 GROUP_MANAGE_ENABLED / GROUP_MANAGE_WEBUI） */
export interface GroupManageFlags {
  enabled: boolean;
  webui: boolean;
}

export type ManageRisk = "low" | "mid" | "high";

export interface ManageActionMeta {
  key: string;
  label: string;
  risk: ManageRisk;
  target: boolean;
  botOwner: boolean;
  confirm: boolean;
  adminAllowed: boolean;
  disabled: boolean;
  /** 此刻为什么不能用；null 表示能用 */
  blockedReason: string | null;
}

export interface ManagePolicy {
  enabled: boolean;
  privateEnabled: boolean;
  webuiEnabled: boolean;
  notifyGroup: boolean;
  allowRelay?: boolean;
  maxMuteMinutes: number;
  maxMuteCeiling: number;
  disabledActions: string[];
  adminActions: string[];
  confirmActions: string[];
  updatedAt: string | null;
  updatedBy: string | null;
}

export interface ManageStatus {
  available: boolean;
  problem: string | null;
  yourRole: string | null;
  botRole: string | null;
  isOwner: boolean;
  allowedActions: string[];
  actions: ManageActionMeta[];
  policy: ManagePolicy;
  usedToday: number;
  dailyLimit: number;
  sent: Array<{ messageId: string; kind: string; at: string; preview: string }>;
}

export interface ManageMember {
  userId: string;
  name: string;
  nickname: string;
  card: string;
  role: string;
  title: string | null;
  mutedUntil: string | null;
}

export interface ManageNotice {
  noticeId: string;
  senderId: string;
  publishTime: string | null;
  content: string;
}

export interface ManageAuditRow {
  id: number;
  at: string;
  actorId: string;
  actorRole: string | null;
  source: "private" | "webui" | string;
  action: string;
  label: string;
  result: "success" | "failed" | "denied" | string;
  targetId: string | null;
  targetName: string | null;
  detail: string | null;
  error: string | null;
  reason: string | null;
}

export interface ManageActionBody {
  action: string;
  member?: string;
  minutes?: number;
  enable?: boolean;
  content?: string;
  text?: string;
  pinned?: boolean;
  confirmRequired?: boolean;
  reject?: boolean;
  noticeId?: string;
  messageId?: string;
  reason?: string;
  confirm?: boolean;
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
  manage: (groupId: string) => api<ManageStatus>(`/me/groups/${encodeURIComponent(groupId)}/manage`).then((r) => r.data),
  manageMembers: (groupId: string, query = "", refresh = false) =>
    api<ManageMember[]>(`/me/groups/${encodeURIComponent(groupId)}/manage/members?limit=500&query=${encodeURIComponent(query)}${refresh ? "&refresh=true" : ""}`)
      .then((r) => ({ rows: r.data, total: Number(r.meta.total ?? r.data.length), muted: Number(r.meta.muted ?? 0) })),
  manageNotices: (groupId: string) => api<ManageNotice[]>(`/me/groups/${encodeURIComponent(groupId)}/manage/notices`).then((r) => r.data),
  manageAudit: (groupId: string, params: { result?: string; mine?: boolean } = {}) =>
    api<ManageAuditRow[]>(`/me/groups/${encodeURIComponent(groupId)}/manage/audit?limit=200${params.result ? `&result=${params.result}` : ""}${params.mine ? "&mine=true" : ""}`).then((r) => r.data),
  managePreview: (groupId: string, body: ManageActionBody) =>
    api<{ summary: string; risk: ManageRisk; label: string }>(`/me/groups/${encodeURIComponent(groupId)}/manage/preview`, json("POST", body)).then((r) => r.data),
  manageAction: (groupId: string, body: ManageActionBody) =>
    api<{ summary: string; result: Record<string, unknown> }>(`/me/groups/${encodeURIComponent(groupId)}/manage/actions`, json("POST", body)).then((r) => r.data),
  managePolicy: (groupId: string, changes: Partial<ManagePolicy>) =>
    api<ManagePolicy>(`/me/groups/${encodeURIComponent(groupId)}/manage/policy`, json("PUT", changes)).then((r) => r.data),
  logoutAll: () => api<{ authenticated: false }>("/me/logout-all", json("POST")).then((r) => r.data),
};
