import type { EnvironmentDiagnostic, EnvironmentEntry, EnvironmentEntryMeta } from "./types";

// 配置页的纯逻辑：分类、筛选、校验与格式化。校验规则镜像后端
// environment_schema.validate_value，前端即时提示，后端写入前再兜底一次。

export interface EnvironmentCategory {
  key: string;
  label: string;
  icon: string;
  hint: string;
  sections: string[];
}

export const MODELS_CATEGORY = "models";
export const OTHER_CATEGORY = "other";

export const ENV_CATEGORIES: EnvironmentCategory[] = [
  { key: MODELS_CATEGORY, label: "模型与提供商", icon: "🧠", hint: "提供商、模型档位与各插件的任务路由", sections: ["OpenAI-compatible 服务", "子插件 LLM 任务路由"] },
  { key: "runtime", label: "运行与连接", icon: "📡", hint: "NoneBot、OneBot 连接、数据库与错误上报", sections: ["NoneBot 运行时", "OneBot V11 连接", "本地数据与 SQLite/ORM", "Sentry 可选错误上报"] },
  { key: "webui", label: "管理后台与安全", icon: "🛡️", hint: "WebUI 开关、运维 Token 与会话", sections: ["Core / Agent 管理 WebUI"] },
  { key: "agent", label: "Agent", icon: "🌸", hint: "默认人设、媒体与文件工具、代码沙箱", sections: ["Agent 全局默认人设", "Agent 媒体与文件工具", "Agent 代码沙箱"] },
  { key: "games", label: "游戏", icon: "🎲", hint: "跑团 RPG 与狼人杀", sections: ["游戏 AI 开关", "Core/Agent AI 开关", "跑团 RPG", "狼人杀", "游戏插件常用覆盖"] },
  { key: "fanqie", label: "番茄小说", icon: "📚", hint: "可选的小说下载插件", sections: ["番茄小说插件（可选）"] },
  { key: OTHER_CATEGORY, label: "其他与自定义", icon: "✨", hint: "目录外的自定义配置", sections: [] },
];

const SECTION_TO_CATEGORY = new Map(
  ENV_CATEGORIES.flatMap((category) => category.sections.map((section) => [section, category.key] as const)),
);

export function categoryOf(section: string): string {
  return SECTION_TO_CATEGORY.get(section) ?? OTHER_CATEGORY;
}

export function categoryByKey(key: string | null | undefined): EnvironmentCategory {
  return ENV_CATEGORIES.find((item) => item.key === key) ?? ENV_CATEGORIES[0];
}

// 只能经提供商表单整体提交的键，不能当普通字段编辑
export const PROVIDER_STORAGE_KEYS = new Set(["AI_BASE_URL", "AI_API_KEY", "AI_PROVIDERS", "AI_PROVIDER_API_KEYS"]);

export type EnvironmentFilter = "all" | "customized" | "dirty" | "issues" | "overridden" | "secret";

export const FILTER_OPTIONS: Array<{ value: EnvironmentFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "customized", label: "已自定义" },
  { value: "dirty", label: "未保存" },
  { value: "issues", label: "有问题" },
  { value: "overridden", label: "外部覆盖" },
  { value: "secret", label: "敏感项" },
];

export function isFilter(value: string | null): value is EnvironmentFilter {
  return FILTER_OPTIONS.some((item) => item.value === value);
}

export const EMPTY_META: EnvironmentEntryMeta = {
  label: "",
  min: null,
  max: null,
  format: null,
  suggestions: [],
  deprecated: false,
};

export function metaOf(entry: EnvironmentEntry): EnvironmentEntryMeta {
  return entry.meta ?? EMPTY_META;
}

export function labelOf(entry: EnvironmentEntry): string {
  return metaOf(entry).label || entry.key;
}

export interface FilterContext {
  changes: Record<string, string | null>;
  errors: Record<string, string>;
  diagnosticKeys: Set<string>;
}

export function matchesFilter(entry: EnvironmentEntry, filter: EnvironmentFilter, context: FilterContext): boolean {
  switch (filter) {
    case "customized":
      return entry.configured;
    case "dirty":
      return Object.hasOwn(context.changes, entry.key);
    case "issues":
      return Boolean(entry.issue) || Boolean(context.errors[entry.key]) || context.diagnosticKeys.has(entry.key);
    case "overridden":
      return entry.source === "process" || entry.source === "environment";
    case "secret":
      return entry.secret;
    default:
      return true;
  }
}

export function searchEntries(entries: EnvironmentEntry[], search: string): EnvironmentEntry[] {
  const needle = search.trim().toLocaleLowerCase();
  if (!needle) return entries;
  return entries.filter((item) =>
    [item.key, item.section, item.description, labelOf(item)].some((value) =>
      value.toLocaleLowerCase().includes(needle),
    ),
  );
}

// ── 校验 ──

const INTEGER_RE = /^[+-]?\d+$/;
const NUMBER_RE = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;

export function validateDraft(entry: EnvironmentEntry, value: string | null): string | null {
  if (value === null) {
    return null;
  }
  const trimmed = value.trim();
  if (entry.options.length > 0 && !entry.options.includes(value)) {
    return `仅支持 ${entry.options.join(", ")}`;
  }
  if (entry.kind === "boolean") {
    return ["true", "false"].includes(trimmed.toLowerCase()) ? null : "只能是 true 或 false";
  }
  if (entry.kind === "integer" || entry.kind === "number") {
    if (!trimmed) return "不能为空；想用默认值请点「恢复默认」";
    const pattern = entry.kind === "integer" ? INTEGER_RE : NUMBER_RE;
    if (!pattern.test(trimmed)) return entry.kind === "integer" ? "必须是整数" : "必须是数字";
    const number = Number(trimmed);
    const { min, max } = metaOf(entry);
    if (min !== null && number < min) return `不能小于 ${min}`;
    if (max !== null && number > max) return `不能大于 ${max}`;
    return null;
  }
  if (entry.kind === "json") {
    if (!trimmed) return "不能为空；想用默认值请点「恢复默认」";
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return "JSON 格式错误";
    }
    let expected: unknown = null;
    try {
      expected = entry.defaultValue ? JSON.parse(entry.defaultValue) : null;
    } catch {
      expected = null;
    }
    if (Array.isArray(expected) && !Array.isArray(parsed)) return "必须是 JSON 数组";
    if (expected && typeof expected === "object" && !Array.isArray(expected) && (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))) {
      return "必须是 JSON 对象";
    }
    if (metaOf(entry).format === "json-string-list" && Array.isArray(parsed) && !parsed.every((item) => typeof item === "string")) {
      return "数组里每一项都必须是字符串（加引号）";
    }
    return null;
  }
  if (entry.key === "WEBUI_ADMIN_TOKEN") {
    if (!value) return "Token 不能为空";
    if (value !== trimmed) return "Token 首尾不能有空格";
  }
  return null;
}

export function draftErrors(entries: Map<string, EnvironmentEntry>, changes: Record<string, string | null>): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, value] of Object.entries(changes)) {
    const entry = entries.get(key);
    if (!entry) continue;
    const error = validateDraft(entry, value);
    if (error) errors[key] = error;
  }
  return errors;
}

// ── 列表值 ──

export function parseCsvList(value: string | null | undefined): string[] {
  return (value ?? "").split(",").map((item) => item.trim()).filter(Boolean);
}

export function serializeCsvList(items: string[]): string {
  return items.map((item) => item.trim()).filter(Boolean).join(",");
}

export function parseJsonStringList(value: string | null | undefined): string[] | null {
  if (!value?.trim()) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string" || typeof item === "number")) {
      return parsed.map(String);
    }
  } catch {
    // 交给 JSON 文本框显示错误
  }
  return null;
}

export function serializeJsonStringList(items: string[]): string {
  return JSON.stringify(items.map((item) => item.trim()).filter(Boolean));
}

/** 格式化成单行紧凑 JSON（.env 只能单行）；无法解析时返回 null */
export function compactJson(value: string): string | null {
  try {
    return JSON.stringify(JSON.parse(value));
  } catch {
    return null;
  }
}

// ── 人类可读的数值说明 ──

function trimNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1).replace(/\.0$/, "");
}

export function humanizeSeconds(seconds: number): string {
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${trimNumber(seconds)} 秒`;
  if (seconds < 3600) return `${trimNumber(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${trimNumber(seconds / 3600)} 小时`;
  return `${trimNumber(seconds / 86400)} 天`;
}

export function humanizeBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${trimNumber(value)} ${units[unit]}`;
}

export function humanizeValue(entry: EnvironmentEntry, value: string | null | undefined): string | null {
  const raw = (value ?? "").trim();
  if (!raw || !NUMBER_RE.test(raw)) return null;
  const number = Number(raw);
  switch (metaOf(entry).format) {
    case "seconds":
      return number >= 60 ? humanizeSeconds(number) : null;
    case "minutes":
      return number >= 60 ? humanizeSeconds(number * 60) : null;
    case "hours":
      return number >= 24 ? humanizeSeconds(number * 3600) : null;
    case "bytes":
      return number >= 1024 ? humanizeBytes(number) : null;
    default:
      return null;
  }
}

export function rangeHint(entry: EnvironmentEntry): string | null {
  const { min, max } = metaOf(entry);
  if (min !== null && max !== null) return `范围 ${min}–${max}`;
  if (max !== null) return `最大 ${max}`;
  if (min !== null && min !== 0 && min !== 1) return `至少 ${min}`;
  return null;
}

// ── 运维 Token ──

export const RECOMMENDED_TOKEN_LENGTH = 16;
const COMMON_TOKEN_PARTS = ["12345", "54321", "password", "admin", "qwerty", "yawnbot", "abc123", "111111", "000000"];

/** 与后端 admin_token_warnings 同一套规则 */
export function tokenWarnings(token: string): string[] {
  if (!token) return [];
  const warnings: string[] = [];
  if (token.length < RECOMMENDED_TOKEN_LENGTH) warnings.push(`长度少于建议的 ${RECOMMENDED_TOKEN_LENGTH} 个字符`);
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((pattern) => pattern.test(token)).length;
  if (classes < 2) warnings.push("只用了一类字符");
  const lowered = token.toLowerCase();
  if (COMMON_TOKEN_PARTS.some((part) => lowered.includes(part))) warnings.push("包含常见弱口令片段");
  if (new Set(token).size <= Math.max(2, Math.floor(token.length / 4))) warnings.push("重复字符过多");
  return warnings;
}

export function tokenStrength(token: string): { score: number; label: string; status: "exception" | "normal" | "success" } {
  if (!token) return { score: 0, label: "未填写", status: "exception" };
  const warnings = tokenWarnings(token).length;
  const lengthScore = Math.min(1, token.length / 32);
  const score = Math.max(5, Math.round((lengthScore * 0.7 + (1 - warnings / 4) * 0.3) * 100));
  if (warnings === 0 && token.length >= 24) return { score: Math.max(score, 90), label: "强", status: "success" };
  if (warnings === 0) return { score, label: "中等", status: "normal" };
  return { score: Math.min(score, 40), label: "偏弱", status: "exception" };
}

/** 浏览器本地生成，不经过服务器以外的任何地方 */
export function generateToken(bytes = 24): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  let binary = "";
  for (const byte of buffer) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ── 体检 ──

export const DIAGNOSTIC_ORDER: Record<EnvironmentDiagnostic["level"], number> = { error: 0, warning: 1, info: 2 };

export function sortDiagnostics(items: EnvironmentDiagnostic[]): EnvironmentDiagnostic[] {
  return [...items].sort((left, right) => DIAGNOSTIC_ORDER[left.level] - DIAGNOSTIC_ORDER[right.level]);
}

export function diagnosticKeySet(items: EnvironmentDiagnostic[]): Set<string> {
  // 汇总类提示（外部覆盖列表）不把每个键都算成“有问题”
  return new Set(items.filter((item) => item.level !== "info" || item.keys.length === 1).flatMap((item) => item.keys));
}

// ── 危险变更 ──

export function dangerousChanges(changes: Record<string, string | null>): string[] {
  const notes: string[] = [];
  if (Object.hasOwn(changes, "WEBUI_ADMIN_TOKEN")) notes.push("运维 Token 将被替换：重启后当前会话失效，需要用新 Token 登录。请先确认你记住了新 Token。");
  if (changes.WEBUI_ENABLED === "false" || (Object.hasOwn(changes, "WEBUI_ENABLED") && changes.WEBUI_ENABLED === null)) {
    notes.push("WebUI 将被关闭：重启后无法再通过网页管理，只能到服务器上改配置文件恢复。");
  }
  if (Object.hasOwn(changes, "PORT") || Object.hasOwn(changes, "HOST")) notes.push("修改监听地址或端口后，反向代理和 OneBot 连接可能需要同步调整。");
  if (Object.hasOwn(changes, "ONEBOT_V11_ACCESS_TOKEN")) notes.push("OneBot 访问令牌需要与 NapCat/Lagrange 端保持一致，否则 Bot 会掉线。");
  return notes;
}
