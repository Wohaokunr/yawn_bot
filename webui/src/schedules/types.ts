// 定时任务页的接口类型：镜像 src/plugins/yawn_core/webui/schedules.py。
// 规则对象（schedule）按后端存储原样保留 snake_case，与 Agent 工具参数同一套写法。

export type ScheduleKind = "once" | "interval" | "daily" | "weekly" | "monthly" | "cron";
export type ScheduleStatus = "active" | "paused" | "completed" | "cancelled" | "failed";
export type ScheduleMode = "agent" | "notify";

export interface ScheduleRule {
  kind: ScheduleKind;
  at?: string;
  delay_minutes?: number;
  every_minutes?: number;
  times?: string[];
  weekdays?: number[];
  month_days?: number[];
  months?: number[];
  cron?: string;
  window?: { start: string; end: string };
  start_at?: string;
  end_at?: string;
  skip_dates?: string[];
}

export interface ScheduleHistoryEntry {
  at: string;
  planned?: string;
  result: string;
  label?: string;
  ms?: number;
  late?: number;
  preview?: string;
  error?: string;
  trace?: string;
}

export interface ScheduleTask {
  id: number;
  groupId: string;
  groupName: string | null;
  botId: string | null;
  creatorUserId: string;
  creatorName: string | null;
  title: string;
  instruction: string;
  mode: ScheduleMode;
  kind: ScheduleKind;
  kindLabel: string;
  description: string;
  status: ScheduleStatus;
  statusReason: string | null;
  mentionCreator: boolean;
  maxRuns: number | null;
  runCount: number;
  failureCount: number;
  skipCount: number;
  nextRunAt: string | null;
  lastRunAt: string | null;
  lastResult: string | null;
  lastResultLabel: string | null;
  lastError: string | null;
  lastOutput: string | null;
  lastTraceId: string | null;
  sourceKind: string;
  revision: number;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface ScheduleTaskDetail extends ScheduleTask {
  schedule: ScheduleRule;
  upcoming: string[];
  history: ScheduleHistoryEntry[];
  cancelledAt: string | null;
  cancelledBy: string | null;
}

export interface ScheduleStatusData {
  ready: boolean;
  enabled: boolean;
  now: string;
  counts: Record<ScheduleStatus, number>;
  today: { day: string | null; runs: number; results: Record<string, number> };
  upcoming: ScheduleTask[];
  groups: number;
}

export interface ScheduleTasksPage {
  ready: boolean;
  items: ScheduleTask[];
  total: number;
  page?: number;
  pageSize?: number;
}

export interface ScheduleRunItem extends ScheduleHistoryEntry {
  taskId: number;
  title: string;
  mode: ScheduleMode;
  groupId: string;
  groupName: string | null;
}

export interface SchedulePreview {
  valid: boolean;
  error?: string;
  code?: string;
  hint?: string | null;
  description?: string;
  kind?: ScheduleKind;
  schedule?: ScheduleRule;
  upcoming?: string[];
}

export interface ScheduleTaskInput {
  groupId?: string;
  creatorUserId?: string;
  title: string;
  instruction: string;
  mode: ScheduleMode;
  schedule: ScheduleRule;
  maxRuns: number | null;
  mentionCreator: boolean;
  revision?: number;
}

export interface ScheduleSettings {
  enabled: boolean;
  groupMode: "all" | "allowlist";
  allowedGroups: string[];
  blockedGroups: string[];
  whoCanCreate: "members" | "admins";
  allowAgentMode: boolean;
  maxActivePerGroup: number;
  maxActivePerMember: number;
  minIntervalMinutes: number;
  maxHorizonDays: number;
  groupDailyRuns: number;
  misfireGraceMinutes: number;
  missedPolicy: "skip" | "run_once";
  catchUpMaxHours: number;
  maxConsecutiveFailures: number;
  quietHoursEnabled: boolean;
  quietStart: string;
  quietEnd: string;
  quietPolicy: "defer" | "skip";
  concurrency: number;
  runTimeoutSeconds: number;
  notifyPrefix: string;
  historyLimit: number;
}
