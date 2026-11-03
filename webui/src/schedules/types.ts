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
  /** 私聊提醒汇总（和群定时任务同一套规则引擎，存在每人的个人数据里） */
  private?: PrivateReminderCounts;
}

export interface ScheduleTasksPage {
  ready: boolean;
  items: ScheduleTask[];
  total: number;
  page?: number;
  pageSize?: number;
}

export interface ScheduleRunItem extends ScheduleHistoryEntry {
  /** 私聊提醒的运行记录没有群任务编号，用 ref（p3）定位 */
  taskId: number | null;
  ref?: string;
  title: string;
  mode: ScheduleMode;
  groupId: string;
  groupName: string | null;
  /** group：群定时任务；private：某人的私聊提醒（此时 userId 有值、taskId 是提醒编号） */
  channel?: "group" | "private";
  userId?: string;
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
  /** 私聊里能给群设/改群定时任务 */
  crossCreate: boolean;
  /** 群里能让 Bot 到点私聊提醒自己 */
  deliverToPrivate: boolean;
  /** 群任务被自动暂停时私聊告诉设的人 */
  notifyCreatorOnPause: boolean;
  /** 私聊提醒可选“到点替他去办” */
  privateAgentMode: boolean;
  privateMaxActive: number;
  privateMinIntervalMinutes: number;
  privateRetryLimit: number;
}

// ── 私聊提醒（每人的个人助理数据；后端 /schedules/private）────────────────

export type PrivateReminderStatus = "active" | "paused" | "retrying" | "completed" | "broken" | string;

export interface PrivateReminder {
  key: string;
  ref: string;
  channel: "private";
  userId: string;
  itemId: number;
  title: string;
  content: string;
  mode: ScheduleMode;
  kindLabel: string;
  description: string;
  status: PrivateReminderStatus;
  nextRunAt: string | null;
  runCount: number;
  maxRuns: number | null;
  lastResult: string | null;
  lastResultLabel: string | null;
  lastRunAt: string | null;
  retryAt: string | null;
  /** private：私聊里设的；group：群里让 Bot 私聊提醒；webui：网页上设的 */
  sourceKind: "private" | "group" | "webui" | string;
  sourceLabel: string;
  originGroupId: string | null;
  createdAt: string | null;
}

export interface PrivateReminderDetail extends PrivateReminder {
  schedule: ScheduleRule;
  history: ScheduleHistoryEntry[];
}

export interface PrivateReminderCounts {
  active: number;
  paused: number;
  agent: number;
  fromGroups: number;
  users: number;
}

export interface PrivateRemindersPage {
  items: PrivateReminder[];
  total: number;
  counts: PrivateReminderCounts;
}
