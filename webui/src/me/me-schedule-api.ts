// 个人账号“我的定时任务”接口（后端 me_schedule_routes.py，前缀 /me/schedules）。
// 本人只认会话里的 QQ：私聊提醒是自己的；群任务只能看/改自己设的，群主/管理员还能管本群别人设的。
import { api } from "../api";
import type { PrivateReminder, PrivateReminderDetail, ScheduleHistoryEntry, ScheduleMode, ScheduleRule, ScheduleTask } from "../schedules/types";

export interface MyGroupTask extends ScheduleTask {
  ref: string;
  mine: boolean;
  youManage?: boolean;
}

export interface MyGroupTaskDetail extends MyGroupTask {
  schedule: ScheduleRule;
  upcoming: string[];
  history: ScheduleHistoryEntry[];
}

export type MyScheduleItem = (PrivateReminder & { channel: "private" }) | (MyGroupTask & { channel?: "group" });
export type MyScheduleDetail = PrivateReminderDetail | MyGroupTaskDetail;

export interface MyScheduleGroup {
  groupId: string;
  groupName: string;
  role: string;
  scheduleOpen: boolean;
  canCreate: boolean;
}

export interface MySchedulesData {
  items: MyScheduleItem[];
  counts: Record<string, number>;
  groupReady: boolean;
  groups: MyScheduleGroup[];
  policy: {
    crossCreate: boolean;
    privateAgentMode: boolean;
    allowAgentMode: boolean;
    privateMinInterval: number;
    groupMinInterval: number;
  };
}

export interface MyScheduleInput {
  target: "private" | "group";
  groupId?: string;
  content: string;
  title: string;
  mode: ScheduleMode;
  schedule: ScheduleRule;
  maxRuns: number | null;
  mentionMe: boolean;
  /** 群任务：到点 @全体成员（要你是这个群的群主/管理员） */
  mentionAll?: boolean;
  /** 群任务：到点还要 @ 的群成员 QQ 号 */
  mentionUserIds?: string[];
}

export interface MySchedulePreview {
  valid: boolean;
  error?: string;
  hint?: string | null;
  description?: string;
  upcoming?: string[];
}

export type MyScheduleAction = "pause" | "resume" | "cancel";

const BASE = "/me/schedules";

function json(method: string, body?: unknown): RequestInit {
  return body === undefined ? { method } : { method, body: JSON.stringify(body) };
}

export function isPrivateItem(item: { ref: string }): boolean {
  return item.ref.startsWith("p");
}

export const myScheduleApi = {
  list: (params: { channel: string; scope: string; includeFinished: boolean }, signal?: AbortSignal) => {
    const search = new URLSearchParams({ channel: params.channel, scope: params.scope, include_finished: String(params.includeFinished) });
    return api<MySchedulesData>(`${BASE}?${search.toString()}`, { signal }).then((r) => r.data);
  },
  detail: (ref: string, signal?: AbortSignal) => api<MyScheduleDetail>(`${BASE}/${encodeURIComponent(ref)}`, { signal }).then((r) => r.data),
  preview: (input: { target: string; schedule: ScheduleRule; maxRuns: number | null; mode: ScheduleMode }) =>
    api<MySchedulePreview>(`${BASE}/preview`, json("POST", input)).then((r) => r.data),
  create: (input: MyScheduleInput) =>
    api<{ created: boolean; item: MyScheduleDetail }>(BASE, json("POST", input)).then((r) => r.data),
  update: (ref: string, input: Partial<MyScheduleInput> & { revision?: number }) =>
    api<MyScheduleDetail>(`${BASE}/${encodeURIComponent(ref)}`, json("PATCH", input)).then((r) => r.data),
  action: (ref: string, action: MyScheduleAction) =>
    api<{ changed: boolean }>(`${BASE}/${encodeURIComponent(ref)}/${action}`, json("POST")).then((r) => r.data),
};
