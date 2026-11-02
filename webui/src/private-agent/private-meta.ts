// 「私聊 Agent」页的纯函数（无 React，便于单测）。
import { composeText } from "../agent-prompts/prompt-meta";
import type { PromptOverride } from "../agent-prompts/prompt-meta";
import type { PersonaMode, PrivatePromptIssue, PrivateScope, PrivateSectionRow, PrivateSnapshot } from "./private-api";

export type PrivateTab = "prompts" | "persona" | "captures";
export const PRIVATE_TABS: PrivateTab[] = ["prompts", "persona", "captures"];

export function parsePrivateTab(value: string | null): PrivateTab {
  return PRIVATE_TABS.includes(value as PrivateTab) ? (value as PrivateTab) : "prompts";
}

/** QQ 号：5–12 位数字；其他输入当作没选用户。 */
export function cleanUserId(value: string | null | undefined): string {
  const text = String(value ?? "").trim();
  return /^\d{5,12}$/.test(text) ? text : "";
}

export function scopeOf(userId: string): PrivateScope {
  return userId ? "user" : "global";
}

export function ownOverride(row: PrivateSectionRow, scope: PrivateScope): PromptOverride | null {
  return scope === "user" ? row.userOverride : row.globalOverride;
}

export function baseOf(row: PrivateSectionRow): string {
  return row.defaultText ?? row.sample?.text ?? "";
}

/** 本人范围下“下一层”（全局）的生效文字：本人覆盖叠在它上面。 */
export function inheritedOf(row: PrivateSectionRow, scope: PrivateScope): string {
  const base = baseOf(row);
  if (scope === "global" || !row.globalOverride) return base;
  if (row.globalOverride.mode === "builtin") return base;
  return composeText(base, row.globalOverride.mode, row.globalOverride.text);
}

export type PrivateFilter = "all" | "modified" | "mine" | "safety" | "disabled" | "tools";

export const PRIVATE_FILTERS: Array<{ value: PrivateFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "modified", label: "已修改" },
  { value: "mine", label: "本人覆盖" },
  { value: "disabled", label: "已删除" },
  { value: "safety", label: "安全相关" },
  { value: "tools", label: "被多人改过" },
];

export function isRowModified(row: PrivateSectionRow): boolean {
  return row.effectiveMode !== "default" || Boolean(row.globalOverride) || Boolean(row.userOverride);
}

export function filterPrivateSections(
  rows: PrivateSectionRow[],
  { group, filter, query, pipeline }: { group: string; filter: PrivateFilter; query: string; pipeline: string },
): PrivateSectionRow[] {
  const needle = query.trim().toLowerCase();
  return rows.filter((row) => {
    if (group !== "all" && row.group !== group) return false;
    if (pipeline && !row.pipelines.some((item) => item.pipeline === pipeline)) return false;
    if (filter === "modified" && !isRowModified(row)) return false;
    if (filter === "mine" && !row.userOverride) return false;
    if (filter === "disabled" && row.effectiveMode !== "disable") return false;
    if (filter === "safety" && !row.safety) return false;
    if (filter === "tools" && row.userOverrideCount < 2) return false;
    if (!needle) return true;
    return [row.id, row.title, row.description, row.when, row.groupLabel, row.defaultText ?? "", row.effectiveText ?? ""]
      .join("\n")
      .toLowerCase()
      .includes(needle);
  });
}

export function groupCounts(rows: PrivateSectionRow[]): Record<string, { total: number; modified: number }> {
  const counts: Record<string, { total: number; modified: number }> = {};
  for (const row of rows) {
    const entry = (counts[row.group] ??= { total: 0, modified: 0 });
    entry.total += 1;
    if (isRowModified(row)) entry.modified += 1;
  }
  return counts;
}

export const ISSUE_META: Record<PrivatePromptIssue["level"], { label: string; color: string; alert: "error" | "warning" | "info" }> = {
  danger: { label: "严重", color: "red", alert: "error" },
  warning: { label: "注意", color: "orange", alert: "warning" },
  info: { label: "提示", color: "blue", alert: "info" },
};

export function issueScopeLabel(scope: string): string {
  if (scope === "global") return "全局";
  if (scope.startsWith("user:")) return `用户 ${scope.slice(5)}`;
  return scope;
}

export function worstIssue(issues: PrivatePromptIssue[]): PrivatePromptIssue["level"] | null {
  if (issues.some((item) => item.level === "danger")) return "danger";
  if (issues.some((item) => item.level === "warning")) return "warning";
  return issues.length ? "info" : null;
}

export const PIPELINE_WHERE_LABELS: Record<string, string> = {
  "system#1": "system 第 1 层",
  "system#2": "system 第 2 层（长期资料）",
  "system#3": "system 第 3 层（本轮资料，排在最后）",
  user: "user 消息",
  round: "工具循环中追加",
  history: "写进历史",
  worker: "写进任务单",
  tools: "tools 参数",
};

export const PIPELINE_TOOLS_LABELS: Record<string, string> = {
  main: "私聊工具（按开关装载）",
  worker: "小助手工具",
  tasks_only: "只有 tasks 工具",
  none: "不带工具",
};

export const PERSONA_MODE_META: Record<PersonaMode, { label: string; color: string; hint: string }> = {
  follow: { label: "跟随默认", color: "default", hint: "用私聊默认人设（策略里设的模板），没有就跟随群聊全局人设" },
  custom: { label: "专属人设", color: "magenta", hint: "只给这位用户用的一张人设卡" },
  library: { label: "人设库模板", color: "purple", hint: "跟随人设库里的一张模板，模板改了他也跟着变" },
};

export const PERSONA_SOURCE_LABELS: Record<string, string> = {
  user: "本人专属",
  user_library: "本人选的模板",
  private_default: "私聊默认",
  global: "群聊全局",
  builtin: "内置默认",
  draft: "未保存草稿",
};

export const PERSONA_ACTION_LABELS: Record<string, string> = {
  set: "改人设卡",
  patch: "按字段改",
  preset: "套用预设",
  template: "选人设库模板",
  reset: "恢复跟随默认",
  import: "导入",
  copy_group: "复制群聊人设",
};

export function personaActionLabel(action: string | null | undefined): string {
  return PERSONA_ACTION_LABELS[String(action ?? "")] ?? String(action ?? "—");
}

export function personaByLabel(by: string | null | undefined): string {
  if (by === "self") return "本人在私聊里改";
  if (by === "admin") return "管理员";
  return String(by ?? "—");
}

export const PIPELINE_LABELS: Record<string, string> = {
  turn: "主回合",
  report: "汇报",
  coordinate: "协调",
  worker: "后台小助手",
  memory: "记忆整理",
};

export const PREFIX_LAYER_LABELS: Record<string, string> = {
  system: "开头规则/人设",
  tools: "工具表",
  profile: "长期资料",
  history: "历史起点",
};

/** 命中率显示成百分比；服务商没报 usage 时给“—”。 */
export function formatHitRate(rate: number | null | undefined): string {
  return typeof rate === "number" ? `${Math.round(rate * 1000) / 10}%` : "—";
}

export function pipelineLabel(key: string | null | undefined): string {
  return PIPELINE_LABELS[String(key ?? "")] ?? String(key ?? "—");
}

/** 导入私聊提示词：只接受导出的 JSON，且只留私聊分段（private.* / ptool.*）。 */
export function parsePrivateImport(raw: string): { changes: Array<{ sectionId: string; mode: string; text: string; note: string }>; skipped: number; error: string | null } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { changes: [], skipped: 0, error: "不是合法的 JSON" };
  }
  const overrides = (parsed as { overrides?: unknown })?.overrides;
  if (!overrides || typeof overrides !== "object") return { changes: [], skipped: 0, error: "缺少 overrides 字段" };
  const changes: Array<{ sectionId: string; mode: string; text: string; note: string }> = [];
  let skipped = 0;
  for (const [sectionId, value] of Object.entries(overrides as Record<string, unknown>)) {
    if (!sectionId.startsWith("private.") && !sectionId.startsWith("ptool.")) { skipped += 1; continue; }
    const item = value as Partial<PromptOverride>;
    if (!item || typeof item.mode !== "string") { skipped += 1; continue; }
    changes.push({ sectionId, mode: item.mode, text: String(item.text ?? ""), note: String(item.note ?? "") });
  }
  if (changes.length === 0) return { changes, skipped, error: "没有可导入的私聊分段" };
  return { changes, skipped, error: null };
}

export function tokenBar(used: number, limit: number): { percent: number; status: "normal" | "exception" } {
  const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  return { percent, status: used > limit ? "exception" : "normal" };
}

/** 私聊快照按链路显示（任务名都是 agent_dialogue，看不出是主回合还是小助手）。 */
export function withPipelineTask(snapshot: PrivateSnapshot): PrivateSnapshot {
  return snapshot.pipeline ? { ...snapshot, task: `private_${snapshot.pipeline}` } : snapshot;
}
