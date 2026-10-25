// 提示词工作台与「完整提示词」视图共用的类型和纯函数（无 React，便于单测）。

export type PromptMode = "replace" | "append" | "prepend" | "disable" | "builtin";
export type PromptScope = "global" | "group";

export interface PromptOverride {
  mode: PromptMode;
  text: string;
  note: string;
  updatedAt: string;
  updatedBy: string;
}

export interface PromptSample {
  text: string;
  chars: number;
  truncated: boolean;
  seen: number;
  lastSeenAt: string;
  lastGroupId: string | null;
  effectiveChars: number;
}

export interface PromptSectionRow {
  id: string;
  title: string;
  category: string;
  categoryLabel: string;
  layer: string;
  layerLabel: string;
  kind: "static" | "generated" | "header" | "data";
  description: string;
  when: string;
  safety: boolean;
  protocol: boolean;
  editable: boolean;
  modes: PromptMode[];
  tasks: string[];
  defaultText: string | null;
  defaultTokens: number;
  sample: PromptSample | null;
  globalOverride: PromptOverride | null;
  groupOverride: PromptOverride | null;
  effectiveScope: "global" | "group" | "draft" | null;
  effectiveMode: PromptMode | "default";
  effectiveText: string | null;
  effectiveTokens: number;
}

export interface PromptCatalog {
  promptVersion: string;
  revision: number;
  updatedAt: string | null;
  loadError: string | null;
  storeFile: string;
  groupId: string | null;
  categories: Array<{ key: string; label: string; hint: string }>;
  sections: PromptSectionRow[];
  summary: { total: number; globalOverrides: number; groupOverrides: number; disabled: number; safetyDisabled: number };
}

export interface PromptHistoryEntry {
  action: "set" | "clear" | "bulk";
  revision: number;
  at: string;
  by: string;
  override: PromptOverride | null;
}

export interface PromptSpan {
  message: number;
  sectionId: string;
  start: number;
  end: number;
  title?: string;
  category?: string | null;
  safety?: boolean;
  mode?: string | null;
  scope?: string | null;
}

export interface PromptMessage {
  role: string;
  content?: unknown;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
  [key: string]: unknown;
}

export interface PromptSnapshot {
  id: string;
  round: number;
  capturedAt: string;
  kind: "complete" | "tools" | "unsent" | string;
  task: string;
  provider: string | null;
  profile: string | null;
  model: string | null;
  outcome: string;
  durationMs: number;
  timeoutSeconds: number | null;
  params: Record<string, unknown>;
  messages: PromptMessage[];
  messageStats: Array<{ index: number; role: string; chars: number; tokens: number; media: number; toolCalls: number }>;
  tools: Array<Record<string, unknown>>;
  toolNames: string[];
  sections: PromptSpan[];
  response: {
    id: string | null;
    model: string | null;
    finishReason: string | null;
    content: string;
    contentChars: number;
    reasoning: string | null;
    reasoningChars: number;
    toolCalls: Array<{ id: string; name: string; arguments: string }>;
    usage: Record<string, number>;
  } | null;
  totals: { messages: number; chars: number; tokens: number; toolsChars: number; toolsTokens: number; byRole: Record<string, number> };
  redactions: number;
  truncated: boolean;
  omittedChars: number;
}

export interface TracePrompts {
  traceId: string;
  captureEnabled: boolean;
  prompts: PromptSnapshot[];
  evicted: number;
  dropped: number;
  limits: { perTrace: number; snapshotChars: number; tracesWithPrompts: number };
}

export const MODE_META: Record<PromptMode | "default" | "inherit", { label: string; color: string; hint: string }> = {
  default: { label: "默认", color: "default", hint: "使用代码里的默认文字（或本轮动态生成的内容）" },
  inherit: { label: "继承全局", color: "default", hint: "本群不单独设置，跟随全局" },
  replace: { label: "替换", color: "magenta", hint: "整段换成你写的文字" },
  append: { label: "追加", color: "blue", hint: "默认内容后面再接一段（换行分隔）" },
  prepend: { label: "前置", color: "geekblue", hint: "默认内容前面先放一段（换行分隔）" },
  disable: { label: "删除", color: "red", hint: "这一段不再发给模型" },
  builtin: { label: "用代码默认", color: "purple", hint: "本群强制用代码默认值，不继承全局覆盖" },
};

export const KIND_LABELS: Record<PromptSectionRow["kind"], string> = {
  static: "固定文字",
  generated: "动态生成",
  header: "表头",
  data: "上下文数据",
};

export const TASK_LABELS: Record<string, string> = {
  agent_dialogue: "群对话",
  agent_proactive: "主动/续聊",
  agent_router: "工具路由",
  agent_image: "识图",
  agent_memory: "记忆整理",
};

export const SCOPE_LABELS: Record<string, string> = {
  global: "全局",
  group: "本群",
  draft: "草稿",
};

export function modeLabel(mode: string | null | undefined): string {
  return MODE_META[(mode ?? "default") as PromptMode]?.label ?? String(mode ?? "默认");
}

export function taskLabel(task: string | null | undefined): string {
  return TASK_LABELS[String(task ?? "")] ?? String(task ?? "—");
}

/** 与后端 prompt_sections.compose 同口径。 */
export function composeText(base: string, mode: PromptMode | "default" | "inherit", text: string): string {
  if (mode === "replace") return text;
  if (mode === "disable") return "";
  if (mode === "append") return base ? `${base}\n${text}` : text;
  if (mode === "prepend") return base ? `${text}\n${base}` : text;
  return base;
}

/** 这一行的“基础文字”：固定段落用默认值，动态段落用最近一次生成的样例。 */
export function baseText(row: PromptSectionRow): string {
  return row.defaultText ?? row.sample?.text ?? "";
}

/** 当前范围下“这一层”的覆盖；全局范围看 globalOverride，本群范围看 groupOverride。 */
export function scopedOverride(row: PromptSectionRow, scope: PromptScope): PromptOverride | null {
  return scope === "group" ? row.groupOverride : row.globalOverride;
}

/** 本群范围下下层（全局）的生效文字，用来预览“本群改动叠在全局之上”的效果。 */
export function inheritedText(row: PromptSectionRow, scope: PromptScope): string {
  const base = baseText(row);
  if (scope === "global" || !row.globalOverride) return base;
  return composeText(base, row.globalOverride.mode, row.globalOverride.text);
}

export function validateDraft(row: PromptSectionRow, mode: PromptMode, text: string, confirmSafety: boolean): string | null {
  if (!row.editable) return "这一段是本轮数据，不能修改";
  if (!row.modes.includes(mode)) return `这一段不支持“${modeLabel(mode)}”`;
  if ((mode === "replace" || mode === "append" || mode === "prepend") && !text.trim()) return "替换/追加/前置需要填写文字；想去掉这段请选“删除”";
  if (text.length > 12_000) return "文字最长 12000 字";
  if (row.safety && (mode === "disable" || mode === "replace") && !confirmSafety) return "这是安全相关分段，请先勾选确认";
  return null;
}

export type SectionFilter = "all" | "modified" | "disabled" | "safety" | "protocol" | "generated" | "unseen";

export const SECTION_FILTERS: Array<{ value: SectionFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "modified", label: "已修改" },
  { value: "disabled", label: "已删除" },
  { value: "safety", label: "安全相关" },
  { value: "protocol", label: "输出协议" },
  { value: "generated", label: "动态生成" },
  { value: "unseen", label: "本次运行未出现" },
];

export function isModified(row: PromptSectionRow): boolean {
  return row.effectiveMode !== "default" || Boolean(row.globalOverride) || Boolean(row.groupOverride);
}

export function matchesFilter(row: PromptSectionRow, filter: SectionFilter): boolean {
  switch (filter) {
    case "modified": return isModified(row);
    case "disabled": return row.effectiveMode === "disable";
    case "safety": return row.safety;
    case "protocol": return row.protocol;
    case "generated": return row.kind === "generated";
    case "unseen": return !row.sample && row.category !== "tool_schema";
    default: return true;
  }
}

export function sectionSearchText(row: PromptSectionRow): string {
  return [row.id, row.title, row.description, row.when, row.categoryLabel, row.layerLabel, row.defaultText ?? "", row.effectiveText ?? "", row.sample?.text ?? ""].join("\n").toLowerCase();
}

export function filterSections(rows: PromptSectionRow[], { category, filter, query }: { category: string; filter: SectionFilter; query: string }): PromptSectionRow[] {
  const needle = query.trim().toLowerCase();
  return rows.filter((row) =>
    (category === "all" || row.category === category)
    && matchesFilter(row, filter)
    && (!needle || sectionSearchText(row).includes(needle)));
}

export function categoryCounts(rows: PromptSectionRow[]): Record<string, { total: number; modified: number; disabled: number }> {
  const counts: Record<string, { total: number; modified: number; disabled: number }> = {};
  for (const row of rows) {
    const entry = counts[row.category] ?? { total: 0, modified: 0, disabled: 0 };
    entry.total += 1;
    if (isModified(row)) entry.modified += 1;
    if (row.effectiveMode === "disable") entry.disabled += 1;
    counts[row.category] = entry;
  }
  return counts;
}

/** 一键删除安全上下文时要提交的改动（只含还没被删除、支持删除的安全段）。 */
export function safetyRemovalChanges(rows: PromptSectionRow[], scope: PromptScope): Array<{ sectionId: string; mode: PromptMode }> {
  return rows
    .filter((row) => row.safety && row.modes.includes("disable") && scopedOverride(row, scope)?.mode !== "disable")
    .map((row) => ({ sectionId: row.id, mode: "disable" as const }));
}

/** 把这一范围的覆盖全部清掉（恢复默认 / 恢复继承）。 */
export function resetChanges(rows: PromptSectionRow[], scope: PromptScope, category = "all"): Array<{ sectionId: string; mode: "inherit" }> {
  return rows
    .filter((row) => (category === "all" || row.category === category) && scopedOverride(row, scope))
    .map((row) => ({ sectionId: row.id, mode: "inherit" as const }));
}

export interface PromptOverridesExport {
  kind: "yawnbot.prompt-overrides";
  schemaVersion: number;
  scope: string;
  exportedAt: string;
  overrides: Record<string, PromptOverride>;
}

export function parseImport(raw: string): { changes: Array<{ sectionId: string; mode: PromptMode; text: string; note: string }>; error: string | null } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { changes: [], error: "不是合法的 JSON" };
  }
  if (!parsed || typeof parsed !== "object") return { changes: [], error: "文件内容不是对象" };
  const record = parsed as Partial<PromptOverridesExport>;
  if (record.kind !== "yawnbot.prompt-overrides" || !record.overrides || typeof record.overrides !== "object") {
    return { changes: [], error: "不是 YawnBot 提示词覆盖导出文件" };
  }
  const changes = Object.entries(record.overrides).flatMap(([sectionId, value]) => {
    if (!value || typeof value !== "object") return [];
    const mode = String((value as PromptOverride).mode) as PromptMode;
    if (!["replace", "append", "prepend", "disable", "builtin"].includes(mode)) return [];
    return [{ sectionId, mode, text: String((value as PromptOverride).text ?? ""), note: String((value as PromptOverride).note ?? "") }];
  });
  return { changes, error: changes.length ? null : "文件里没有可导入的覆盖" };
}

// ---------- 完整提示词视图 ----------

export function messageText(message: PromptMessage): string {
  const content = message.content;
  if (Array.isArray(content)) {
    return content
      .filter((part): part is { type: string; text?: string } => Boolean(part) && typeof part === "object" && (part as { type?: unknown }).type === "text")
      .map((part) => String(part.text ?? ""))
      .join("");
  }
  if (content === null || content === undefined) return "";
  return typeof content === "string" ? content : JSON.stringify(content);
}

export function messageMedia(message: PromptMessage): string[] {
  const content = message.content;
  if (!Array.isArray(content)) return [];
  return content
    .filter((part) => part && typeof part === "object" && (part as { type?: unknown }).type !== "text")
    .map((part) => {
      const record = part as Record<string, unknown>;
      const inner = record.image_url ?? record.url ?? record.file_data ?? record.input_audio;
      const label = typeof inner === "string" ? inner : typeof inner === "object" && inner ? String((inner as Record<string, unknown>).url ?? JSON.stringify(inner)) : "";
      return `${String(record.type)} ${label}`.trim();
    });
}

export interface TextSegment {
  text: string;
  sectionId: string | null;
  span: PromptSpan | null;
}

/** 按分段区间把一条消息切开，重叠/越界的区间按先到先得处理。 */
export function segmentText(text: string, spans: PromptSpan[]): TextSegment[] {
  const ordered = [...spans].sort((a, b) => a.start - b.start || b.end - a.end);
  const segments: TextSegment[] = [];
  let cursor = 0;
  for (const span of ordered) {
    const start = Math.max(span.start, cursor);
    const end = Math.min(span.end, text.length);
    if (end <= start) continue;
    if (start > cursor) segments.push({ text: text.slice(cursor, start), sectionId: null, span: null });
    segments.push({ text: text.slice(start, end), sectionId: span.sectionId, span });
    cursor = end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), sectionId: null, span: null });
  return segments.length ? segments : [{ text, sectionId: null, span: null }];
}

export function spansByMessage(spans: PromptSpan[]): Map<number, PromptSpan[]> {
  const map = new Map<number, PromptSpan[]>();
  for (const span of spans) {
    const list = map.get(span.message) ?? [];
    list.push(span);
    map.set(span.message, list);
  }
  return map;
}

/** 相对上一次请求新增的消息下标（工具循环每轮会在末尾追加 assistant/tool 消息）。 */
export function newMessageIndexes(previous: PromptSnapshot | null, current: PromptSnapshot): Set<number> {
  const added = new Set<number>();
  if (!previous || previous.task !== current.task) return added;
  current.messages.forEach((message, index) => {
    const before = previous.messages[index];
    if (!before || before.role !== message.role || messageText(before) !== messageText(message)) added.add(index);
  });
  return added;
}

/** 还原成可直接粘进 OpenAI 兼容接口的请求体（不含密钥）。 */
export function requestBody(snapshot: PromptSnapshot): Record<string, unknown> {
  return {
    model: snapshot.model,
    messages: snapshot.messages,
    ...(snapshot.tools.length ? { tools: snapshot.tools } : {}),
    ...snapshot.params,
  };
}

export function curlCommand(snapshot: PromptSnapshot): string {
  const body = JSON.stringify(requestBody(snapshot)).replace(/'/g, "'\\''");
  return [
    "curl -sS \"$AI_BASE_URL/chat/completions\" \\",
    "  -H 'Content-Type: application/json' \\",
    "  -H \"Authorization: Bearer $AI_API_KEY\" \\",
    `  -d '${body}'`,
  ].join("\n");
}

export function snapshotAsText(snapshot: PromptSnapshot): string {
  const lines = snapshot.messages.map((message, index) => {
    const media = messageMedia(message);
    const calls = (message.tool_calls ?? []).map((call) => {
      const fn = (call.function ?? {}) as Record<string, unknown>;
      return `→ ${String(fn.name ?? "")}(${String(fn.arguments ?? "")})`;
    });
    return [`### #${index + 1} ${message.role}${message.tool_call_id ? ` (${message.tool_call_id})` : ""}`, messageText(message), ...media, ...calls].filter(Boolean).join("\n");
  });
  if (snapshot.tools.length) lines.push(`### tools (${snapshot.tools.length})\n${snapshot.toolNames.join(", ")}`);
  return lines.join("\n\n");
}

export function snapshotLabel(snapshot: PromptSnapshot): string {
  const base = taskLabel(snapshot.task);
  if (snapshot.kind === "unsent") return `${base} · 未发送`;
  return snapshot.task === "agent_dialogue" || snapshot.task === "agent_proactive" ? `${base} 第 ${snapshot.round} 轮` : base;
}

export const OUTCOME_META: Record<string, { label: string; color: string }> = {
  success: { label: "成功", color: "green" },
  timeout: { label: "超时", color: "red" },
  empty: { label: "空回复", color: "orange" },
  error: { label: "失败", color: "red" },
  not_configured: { label: "未配置", color: "default" },
  unsupported_multimodal: { label: "不支持多模态", color: "orange" },
  cancelled: { label: "已取消", color: "default" },
  not_sent: { label: "未发送", color: "default" },
};

export function sectionCoverage(snapshot: PromptSnapshot): { chars: number; covered: number; ratio: number } {
  const chars = snapshot.messages.reduce((sum, message) => sum + messageText(message).length, 0);
  const covered = snapshot.sections.reduce((sum, span) => sum + Math.max(0, span.end - span.start), 0);
  return { chars, covered, ratio: chars ? Math.min(1, covered / chars) : 0 };
}
