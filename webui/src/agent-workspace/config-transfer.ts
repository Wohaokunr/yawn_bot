import type { AgentConfig } from "../types";

// 可在群之间迁移的策略字段：不含总开关、版本号与今日计数，导入后仍需管理员手动保存。
const NUMBER_FIELDS = [
  "proactiveProbability",
  "proactiveActiveProbability",
  "proactiveActiveWindowMinutes",
  "idleThresholdMinutes",
  "cooldownMinutes",
  "dailyLimit",
  "rawRetentionDays",
  "adminToolDailyLimit",
  "criticalToolDailyLimit",
] as const;
const BOOLEAN_FIELDS = [
  "replyTriggerEnabled",
  "explicitWakeupEnabled",
  "proactiveEnabled",
  "proactiveActiveEnabled",
  "shortConversationEnabled",
  "mediaCacheEnabled",
] as const;

export type PortableAgentConfig = Partial<Pick<
  AgentConfig,
  (typeof NUMBER_FIELDS)[number] | (typeof BOOLEAN_FIELDS)[number] | "crossGroupVisibility" | "toolAllowlist"
>>;

export const CONFIG_EXPORT_KIND = "yawnbot.agent-config";

export function pickPortableConfig(source: Record<string, unknown>): PortableAgentConfig {
  const result: Record<string, unknown> = {};
  for (const field of NUMBER_FIELDS) {
    const value = source[field];
    if (typeof value === "number" && Number.isFinite(value)) result[field] = value;
  }
  for (const field of BOOLEAN_FIELDS) {
    if (typeof source[field] === "boolean") result[field] = source[field];
  }
  if (source.crossGroupVisibility === "isolated" || source.crossGroupVisibility === "public_summary") {
    result.crossGroupVisibility = source.crossGroupVisibility;
  }
  if (Array.isArray(source.toolAllowlist) && source.toolAllowlist.every((item) => typeof item === "string")) {
    result.toolAllowlist = [...source.toolAllowlist];
  }
  return result as PortableAgentConfig;
}

export function serializeConfigExport(groupId: string, values: Record<string, unknown>): string {
  return JSON.stringify(
    { kind: CONFIG_EXPORT_KIND, version: 1, sourceGroupId: groupId, exportedAt: new Date().toISOString(), config: pickPortableConfig(values) },
    null,
    2,
  );
}

export function parseConfigImport(text: string): PortableAgentConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("文件不是有效的 JSON");
  }
  const record = (parsed && typeof parsed === "object" ? parsed : {}) as Record<string, unknown>;
  const body = record.kind === CONFIG_EXPORT_KIND && record.config && typeof record.config === "object"
    ? record.config as Record<string, unknown>
    : record;
  const picked = pickPortableConfig(body);
  if (Object.keys(picked).length === 0) throw new Error("文件里没有可导入的 Agent 配置字段");
  return picked;
}

export function downloadText(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

// 特权工具一键预设；高风险工具永远需要逐项勾选。
export const TOOL_PRESETS: Array<{ key: string; label: string; tools: string[] }> = [
  { key: "none", label: "全部关闭", tools: [] },
  { key: "default", label: "默认三项", tools: ["mute_member", "create_group_announcement", "set_essence_message"] },
  {
    key: "standard",
    label: "全部低风险",
    tools: [
      "mute_member",
      "unmute_member",
      "recall_message",
      "create_group_announcement",
      "set_essence_message",
      "remove_essence_message",
      "delete_group_notice",
      "set_group_card",
      "set_special_title",
      "set_group_name",
      "create_group_folder",
      "send_file",
    ],
  },
];
