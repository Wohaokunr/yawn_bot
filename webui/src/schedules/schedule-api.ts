// 定时任务页的接口封装。
import { api } from "../api";
import type {
  SchedulePreview,
  ScheduleRule,
  ScheduleRunItem,
  ScheduleSettings,
  ScheduleStatusData,
  ScheduleTask,
  ScheduleTaskDetail,
  ScheduleTaskInput,
  ScheduleTasksPage,
} from "./types";

const BASE = "/schedules";

function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export type TaskAction = "pause" | "resume" | "cancel" | "run";
export type BulkAction = "pause" | "resume" | "cancel" | "delete";

export interface TaskFilters {
  group?: string;
  status?: string;
  mode?: string;
  creator?: string;
  q?: string;
  page?: number;
  pageSize?: number;
}

export const scheduleApi = {
  status: (signal?: AbortSignal) => api<ScheduleStatusData>(`${BASE}/status`, { signal }).then((r) => r.data),
  tasks: (filters: TaskFilters, signal?: AbortSignal) =>
    api<ScheduleTasksPage>(`${BASE}/tasks${query({ ...filters })}`, { signal }).then((r) => r.data),
  task: (id: number, signal?: AbortSignal) => api<ScheduleTaskDetail>(`${BASE}/tasks/${id}`, { signal }).then((r) => r.data),
  runs: (filters: { group?: string; result?: string; limit?: number }, signal?: AbortSignal) =>
    api<{ ready: boolean; items: ScheduleRunItem[] }>(`${BASE}/runs${query(filters)}`, { signal }).then((r) => r.data),
  preview: (schedule: ScheduleRule, maxRuns: number | null, mode: string, signal?: AbortSignal) =>
    api<SchedulePreview>(`${BASE}/preview`, { method: "POST", body: JSON.stringify({ schedule, maxRuns, mode }), signal }).then((r) => r.data),
  create: (input: ScheduleTaskInput) =>
    api<ScheduleTaskDetail>(`${BASE}/tasks`, { method: "POST", body: JSON.stringify(input) }).then((r) => r.data),
  update: (id: number, input: ScheduleTaskInput) =>
    api<ScheduleTaskDetail>(`${BASE}/tasks/${id}`, { method: "PATCH", body: JSON.stringify(input) }).then((r) => r.data),
  action: (id: number, action: TaskAction) =>
    api<{ changed?: boolean; started?: boolean; task?: ScheduleTaskDetail }>(`${BASE}/tasks/${id}/${action}`, { method: "POST" }).then((r) => r.data),
  remove: (id: number) => api<{ deleted: boolean }>(`${BASE}/tasks/${id}`, { method: "DELETE" }).then((r) => r.data),
  bulk: (ids: number[], action: BulkAction) =>
    api<{ changed: number }>(`${BASE}/bulk`, { method: "POST", body: JSON.stringify({ ids, action }) }).then((r) => r.data),
  settings: (signal?: AbortSignal) =>
    api<{ settings: ScheduleSettings; defaults: ScheduleSettings }>(`${BASE}/settings`, { signal }).then((r) => r.data),
  saveSettings: (settings: ScheduleSettings) =>
    api<{ settings: ScheduleSettings }>(`${BASE}/settings`, { method: "PUT", body: JSON.stringify(settings) }).then((r) => r.data.settings),
};

export type { ScheduleTask };
