// 定时任务页的纯逻辑：状态/结果文案、规则表单 ⇄ 规则对象、前端预校验与策略范围。
// 规则的最终校验与“下次触发”计算只在后端（schedule_spec.py）做，这里只挡明显的输入错误。
import type { ScheduleKind, ScheduleMode, ScheduleRule, ScheduleSettings, ScheduleStatus } from "./types";

export type ScheduleTab = "tasks" | "runs" | "policy";
const TABS: ScheduleTab[] = ["tasks", "runs", "policy"];

export function parseScheduleTab(value: string | null): ScheduleTab {
  return TABS.includes(value as ScheduleTab) ? (value as ScheduleTab) : "tasks";
}

export const STATUS_META: Record<ScheduleStatus, { label: string; color: string }> = {
  active: { label: "运行中", color: "green" },
  paused: { label: "已暂停", color: "gold" },
  completed: { label: "已完成", color: "default" },
  cancelled: { label: "已取消", color: "default" },
  failed: { label: "已失败", color: "red" },
};

export const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: "open", label: "进行中（运行 + 暂停）" },
  { value: "", label: "全部状态" },
  ...(Object.keys(STATUS_META) as ScheduleStatus[]).map((value) => ({ value, label: STATUS_META[value].label })),
];

export const MODE_META: Record<ScheduleMode, { label: string; hint: string }> = {
  agent: { label: "唤醒 Agent", hint: "到点把指令交给 Agent，像有人叫它一样思考、可调用工具（查资料、跑代码等），再决定说什么" },
  notify: { label: "直接提醒", hint: "到点把指令原文作为提醒发到群里，不调用模型，最省也最稳" },
};

export const KIND_META: Record<ScheduleKind, { label: string; hint: string }> = {
  once: { label: "单次", hint: "在某个时间点执行一次" },
  interval: { label: "固定间隔", hint: "每隔 N 分钟一次，可限定每天的时间窗口" },
  daily: { label: "每天", hint: "每天的一个或多个钟点" },
  weekly: { label: "每周", hint: "每周的某几天、某几个钟点" },
  monthly: { label: "每月", hint: "每月的某几号（-1 表示最后一天）" },
  cron: { label: "cron", hint: "标准 5 段 cron：分 时 日 月 周（0 和 7 都是周日）" },
};

export const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
export const MONTH_LABELS = Array.from({ length: 12 }, (_, index) => `${index + 1} 月`);

type Tone = "ok" | "warn" | "error" | "muted";
const RESULT_TONES: Record<string, Tone> = {
  spoke: "ok",
  sent: "ok",
  silent: "muted",
  failed: "error",
  delivery_unknown: "warn",
  skipped_missed: "warn",
  skipped_quiet: "muted",
  skipped_quota: "warn",
  skipped_disabled: "muted",
  skipped_closed: "muted",
  skipped_offline: "warn",
  creator_left: "warn",
  deferred_quiet: "muted",
};
const TONE_COLORS: Record<Tone, string> = { ok: "green", warn: "orange", error: "red", muted: "default" };

export const RESULT_FILTERS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部结果" },
  { value: "spoke", label: "已执行并发言" },
  { value: "sent", label: "已发送提醒" },
  { value: "silent", label: "执行但没发言" },
  { value: "failed", label: "执行失败" },
  { value: "delivery_unknown", label: "发送结果未知" },
  { value: "skipped_missed", label: "错过时间跳过" },
  { value: "skipped_quiet", label: "免打扰跳过" },
  { value: "deferred_quiet", label: "免打扰推迟" },
  { value: "skipped_quota", label: "次数用完跳过" },
  { value: "skipped_offline", label: "机器人不在线" },
  { value: "creator_left", label: "创建者已退群" },
];

export function resultColor(result: string | null | undefined): string {
  return TONE_COLORS[RESULT_TONES[String(result ?? "")] ?? "muted"];
}

export function resultTone(result: string | null | undefined): Tone {
  return RESULT_TONES[String(result ?? "")] ?? "muted";
}

export function sourceLabel(source: string): string {
  if (source === "manual") return "管理台创建";
  if (source === "agent") return "群聊里让 Agent 创建";
  return source || "—";
}

// ── 时间显示 ──────────────────────────────────────────────────────────────

/** 后端时间都是北京时间的无时区字符串（YYYY-MM-DDTHH:MM 或 YYYY-MM-DD HH:MM）。 */
export function parseLocal(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value);
  if (!match) return null;
  const [, y, mo, d, h, mi] = match.map(Number);
  return new Date(y, mo - 1, d, h, mi);
}

export function formatLocal(value: string | null | undefined): string {
  const date = parseLocal(value);
  if (!date) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}月${date.getDate()}日 ${WEEKDAY_LABELS[(date.getDay() + 6) % 7]} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** “3 小时后 / 20 分钟前”；以后端给的“现在”为准，避免浏览器时区不是北京时间时算错。 */
export function relativeTo(value: string | null | undefined, now: string | null | undefined): string {
  const target = parseLocal(value);
  const base = parseLocal(now) ?? new Date();
  if (!target) return "";
  const minutes = Math.round((target.getTime() - base.getTime()) / 60_000);
  const abs = Math.abs(minutes);
  const suffix = minutes >= 0 ? "后" : "前";
  if (abs < 1) return "现在";
  if (abs < 60) return `${abs} 分钟${suffix}`;
  if (abs < 60 * 24) return `${Math.round(abs / 60)} 小时${suffix}`;
  return `${Math.round(abs / 60 / 24)} 天${suffix}`;
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`;
  return `${Math.floor(ms / 60_000)} 分 ${Math.round((ms % 60_000) / 1000)} 秒`;
}

export function formatEvery(minutes: number): string {
  if (minutes % 1440 === 0) return `${minutes / 1440} 天`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时`;
  return `${minutes} 分钟`;
}

// ── 规则表单 ──────────────────────────────────────────────────────────────

export interface RuleDraft {
  kind: ScheduleKind;
  onceMode: "at" | "delay";
  at: string;
  delayMinutes: number | null;
  everyMinutes: number | null;
  times: string[];
  weekdays: number[];
  monthDays: number[];
  months: number[];
  cron: string;
  windowEnabled: boolean;
  windowStart: string;
  windowEnd: string;
  startAt: string;
  endAt: string;
  skipDates: string[];
}

export function emptyRuleDraft(kind: ScheduleKind = "daily"): RuleDraft {
  return {
    kind,
    onceMode: "delay",
    at: "",
    delayMinutes: 30,
    everyMinutes: 60,
    times: ["09:00"],
    weekdays: [1, 2, 3, 4, 5],
    monthDays: [1],
    months: [],
    cron: "0 9 * * 1-5",
    windowEnabled: false,
    windowStart: "09:00",
    windowEnd: "22:00",
    startAt: "",
    endAt: "",
    skipDates: [],
  };
}

const CLOCK_RE = /^([01]?\d|2[0-3]):[0-5]\d$/;
const DATETIME_RE = /^\d{4}-\d{2}-\d{2} ([01]\d|2[0-3]):[0-5]\d$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function normalizeClock(value: string): string | null {
  const text = value.trim().replace("：", ":");
  if (!CLOCK_RE.test(text)) return null;
  const [h, m] = text.split(":");
  return `${h.padStart(2, "0")}:${m}`;
}

/** 文本框里的钟点列表（空格/逗号分隔）→ 去重排序后的 HH:MM 列表与不合法项。 */
export function parseClockList(text: string): { times: string[]; invalid: string[] } {
  const times: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,，、;；]+/)) {
    if (!raw.trim()) continue;
    const clock = normalizeClock(raw);
    if (!clock) invalid.push(raw.trim());
    else if (!times.includes(clock)) times.push(clock);
  }
  times.sort();
  return { times, invalid };
}

export function parseDateList(text: string): { dates: string[]; invalid: string[] } {
  const dates: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,，、;；]+/)) {
    const value = raw.trim();
    if (!value) continue;
    if (!DATE_RE.test(value)) invalid.push(value);
    else if (!dates.includes(value)) dates.push(value);
  }
  dates.sort();
  return { dates, invalid };
}

export function ruleFromDraft(draft: RuleDraft): ScheduleRule {
  const rule: ScheduleRule = { kind: draft.kind };
  switch (draft.kind) {
    case "once":
      if (draft.onceMode === "delay") rule.delay_minutes = draft.delayMinutes ?? 0;
      else rule.at = draft.at.trim();
      return rule;
    case "interval":
      rule.every_minutes = draft.everyMinutes ?? 0;
      if (draft.windowEnabled) rule.window = { start: draft.windowStart, end: draft.windowEnd };
      break;
    case "daily":
      rule.times = [...draft.times];
      break;
    case "weekly":
      rule.times = [...draft.times];
      rule.weekdays = [...draft.weekdays].sort((a, b) => a - b);
      break;
    case "monthly":
      rule.times = [...draft.times];
      rule.month_days = [...draft.monthDays];
      break;
    case "cron":
      rule.cron = draft.cron.trim();
      break;
  }
  // 月份限定只对按钟点触发的三种规则有意义。
  if (rule.times && draft.months.length) rule.months = [...draft.months].sort((a, b) => a - b);
  if (draft.startAt.trim()) rule.start_at = draft.startAt.trim();
  if (draft.endAt.trim()) rule.end_at = draft.endAt.trim();
  if (draft.skipDates.length) rule.skip_dates = [...draft.skipDates];
  return rule;
}

export function draftFromRule(rule: ScheduleRule): RuleDraft {
  const draft = emptyRuleDraft(rule.kind);
  if (rule.kind === "once") {
    draft.onceMode = rule.at ? "at" : "delay";
    draft.at = rule.at ?? "";
    if (rule.delay_minutes != null) draft.delayMinutes = rule.delay_minutes;
  }
  if (rule.every_minutes != null) draft.everyMinutes = rule.every_minutes;
  if (rule.times?.length) draft.times = [...rule.times];
  if (rule.weekdays?.length) draft.weekdays = [...rule.weekdays];
  if (rule.month_days?.length) draft.monthDays = [...rule.month_days];
  draft.months = [...(rule.months ?? [])];
  if (rule.cron) draft.cron = rule.cron;
  if (rule.window) {
    draft.windowEnabled = true;
    draft.windowStart = rule.window.start;
    draft.windowEnd = rule.window.end;
  }
  draft.startAt = rule.start_at ?? "";
  draft.endAt = rule.end_at ?? "";
  draft.skipDates = [...(rule.skip_dates ?? [])];
  return draft;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => item !== undefined).sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** 与后端 dict 比较一致：不看键顺序。 */
export function sameRule(left: ScheduleRule, right: ScheduleRule): boolean {
  return stableJson(left) === stableJson(right);
}

/** 前端预校验；返回字段 → 错误。为空不代表后端一定接受（如频率下限由策略决定）。 */
export function validateRuleDraft(draft: RuleDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (draft.kind === "once") {
    if (draft.onceMode === "at" && !DATETIME_RE.test(draft.at.trim())) errors.at = "时间写成 YYYY-MM-DD HH:MM";
    if (draft.onceMode === "delay" && (!draft.delayMinutes || draft.delayMinutes < 1)) errors.delayMinutes = "至少 1 分钟后";
    return errors;
  }
  if (draft.kind === "interval") {
    if (!draft.everyMinutes || draft.everyMinutes < 1) errors.everyMinutes = "间隔至少 1 分钟";
    if (draft.windowEnabled) {
      if (!normalizeClock(draft.windowStart) || !normalizeClock(draft.windowEnd)) errors.window = "时间窗口写成 HH:MM";
      else if (normalizeClock(draft.windowStart) === normalizeClock(draft.windowEnd)) errors.window = "窗口开始和结束不能相同";
    }
  }
  if (["daily", "weekly", "monthly"].includes(draft.kind)) {
    if (!draft.times.length) errors.times = "至少一个钟点";
    else if (draft.times.length > 8) errors.times = "最多 8 个钟点";
  }
  if (draft.kind === "weekly" && !draft.weekdays.length) errors.weekdays = "至少选一天";
  if (draft.kind === "monthly") {
    if (!draft.monthDays.length) errors.monthDays = "至少选一天";
    else if (draft.monthDays.some((day) => day === 0 || day < -1 || day > 31)) errors.monthDays = "日期在 1–31 之间，-1 表示月末";
  }
  if (draft.kind === "cron" && draft.cron.trim().split(/\s+/).length !== 5 && !draft.cron.trim().startsWith("@")) {
    errors.cron = "cron 需要 5 段：分 时 日 月 周";
  }
  for (const key of ["startAt", "endAt"] as const) {
    if (draft[key].trim() && !DATETIME_RE.test(draft[key].trim())) errors[key] = "写成 YYYY-MM-DD HH:MM";
  }
  if (!errors.startAt && !errors.endAt && draft.startAt.trim() && draft.endAt.trim() && draft.endAt.trim() <= draft.startAt.trim()) {
    errors.endAt = "结束时间要晚于开始时间";
  }
  if (draft.skipDates.length > 60) errors.skipDates = "最多 60 个跳过日期";
  return errors;
}

/** 常用规则模板：一键填进表单。 */
export const RULE_PRESETS: Array<{ key: string; label: string; apply: (draft: RuleDraft) => RuleDraft }> = [
  { key: "in30", label: "30 分钟后", apply: (d) => ({ ...d, kind: "once", onceMode: "delay", delayMinutes: 30 }) },
  { key: "morning", label: "每天早 8 点", apply: (d) => ({ ...d, kind: "daily", times: ["08:00"] }) },
  { key: "workday", label: "工作日 9 点", apply: (d) => ({ ...d, kind: "weekly", weekdays: [1, 2, 3, 4, 5], times: ["09:00"] }) },
  { key: "weekend", label: "周末 10 点", apply: (d) => ({ ...d, kind: "weekly", weekdays: [6, 7], times: ["10:00"] }) },
  { key: "hourly", label: "白天每小时", apply: (d) => ({ ...d, kind: "interval", everyMinutes: 60, windowEnabled: true, windowStart: "09:00", windowEnd: "22:00" }) },
  { key: "monthEnd", label: "每月最后一天", apply: (d) => ({ ...d, kind: "monthly", monthDays: [-1], times: ["20:00"] }) },
];

// ── 策略 ──────────────────────────────────────────────────────────────────

type NumericSettingKey = {
  [K in keyof ScheduleSettings]: ScheduleSettings[K] extends number ? K : never;
}[keyof ScheduleSettings];

export const SETTING_RANGES: Record<NumericSettingKey, { min: number; max: number; unit: string; label: string }> = {
  maxActivePerGroup: { min: 1, max: 500, unit: "个", label: "每群进行中的任务上限" },
  maxActivePerMember: { min: 1, max: 100, unit: "个", label: "每位成员进行中的任务上限" },
  minIntervalMinutes: { min: 1, max: 1440, unit: "分钟", label: "循环任务最小间隔" },
  maxHorizonDays: { min: 1, max: 730, unit: "天", label: "最远能定到多久以后" },
  groupDailyRuns: { min: 1, max: 2000, unit: "次/天", label: "每群每日执行次数" },
  misfireGraceMinutes: { min: 1, max: 1440, unit: "分钟", label: "迟到宽限" },
  catchUpMaxHours: { min: 0, max: 72, unit: "小时", label: "最多补跑多久以前的" },
  maxConsecutiveFailures: { min: 1, max: 20, unit: "次", label: "连续失败几次自动暂停" },
  concurrency: { min: 1, max: 8, unit: "个群", label: "同时执行的群数" },
  runTimeoutSeconds: { min: 30, max: 1800, unit: "秒", label: "单次唤醒时限" },
  historyLimit: { min: 5, max: 100, unit: "条", label: "每个任务保留的运行记录" },
};

const GROUP_ID_RE = /^\d{4,12}$/;

export function validateScheduleSettings(settings: ScheduleSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, range] of Object.entries(SETTING_RANGES)) {
    const value = settings[key as NumericSettingKey];
    if (!Number.isInteger(value)) errors[key] = `${range.label}必须是整数`;
    else if (value < range.min || value > range.max) errors[key] = `${range.label}应在 ${range.min}–${range.max} ${range.unit}`;
  }
  if (settings.maxActivePerMember > settings.maxActivePerGroup) {
    errors.maxActivePerMember = "成员上限不应超过群上限";
  }
  if (settings.groupMode === "allowlist" && settings.allowedGroups.length === 0) {
    errors.allowedGroups = "白名单模式下至少填一个群号，否则所有群都用不了";
  }
  for (const key of ["allowedGroups", "blockedGroups"] as const) {
    const bad = settings[key].filter((item) => !GROUP_ID_RE.test(item));
    if (bad.length) errors[key] = `不合法的群号：${bad.slice(0, 3).join("、")}`;
  }
  for (const key of ["quietStart", "quietEnd"] as const) {
    if (!CLOCK_RE.test(settings[key])) errors[key] = "写成 HH:MM";
  }
  if (settings.quietHoursEnabled && !errors.quietStart && !errors.quietEnd && settings.quietStart === settings.quietEnd) {
    errors.quietEnd = "免打扰的开始和结束不能相同";
  }
  if ([...settings.notifyPrefix].length > 8) errors.notifyPrefix = "最多 8 个字符";
  return errors;
}

export function describeScheduleAccess(settings: ScheduleSettings): string {
  if (!settings.enabled) return "已关闭：Agent 不会再创建或执行定时任务";
  const scope = settings.groupMode === "allowlist" ? `仅 ${settings.allowedGroups.length} 个白名单群` : "所有开启 Agent 的群";
  const who = settings.whoCanCreate === "admins" ? "仅群主/管理员可创建" : "群成员都可创建";
  const mode = settings.allowAgentMode ? "可唤醒 Agent" : "只能直接提醒";
  return `${scope} · ${who} · ${mode}`;
}
