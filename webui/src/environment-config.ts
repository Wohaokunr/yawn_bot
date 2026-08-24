import type { EnvironmentEntry, EnvironmentValueSource } from "./types";

export const PROVIDER_PANEL_KEY = "llm-providers";
export const LLM_PANEL_KEY = "llm-models";
export const TASK_PANEL_KEY = "task-routing";

export const MODEL_CONFIGS = [
  {
    title: "默认模型",
    description: "复杂对话、KP 与狼人杀决策",
    modelKey: "AI_MODEL",
    providerKey: "AI_DEFAULT_PROVIDER",
    thinkingKey: "AI_DEFAULT_THINKING",
    multimodalKey: "AI_DEFAULT_MULTIMODAL",
  },
  {
    title: "轻量模型",
    description: "高频短文本与结构化任务；留空回退默认模型",
    modelKey: "AI_LIGHT_MODEL",
    providerKey: "AI_LIGHT_PROVIDER",
    thinkingKey: "AI_LIGHT_THINKING",
    multimodalKey: "AI_LIGHT_MULTIMODAL",
  },
  {
    title: "识图模型",
    description: "图片描述与不支持图片时的转述降级",
    modelKey: "AI_VISION_MODEL",
    providerKey: "AI_VISION_PROVIDER",
    thinkingKey: "AI_VISION_THINKING",
    multimodalKey: undefined,
  },
] as const;

export const MODEL_BADGES = [
  { icon: "🌸", tone: "tone-sakura" },
  { icon: "🌱", tone: "tone-mint" },
  { icon: "📷", tone: "tone-sky" },
] as const;

export const TASK_CONFIGS = [
  {
    plugin: "Agent",
    tasks: [
      ["普通对话 / 工具", "AGENT_DIALOGUE_LLM_PROFILE", "AGENT_DIALOGUE_THINKING"],
      ["主动发言", "AGENT_PROACTIVE_LLM_PROFILE", "AGENT_PROACTIVE_THINKING"],
      ["记忆整理", "AGENT_MEMORY_LLM_PROFILE", "AGENT_MEMORY_THINKING"],
      ["图片描述", "AGENT_IMAGE_LLM_PROFILE", "AGENT_IMAGE_THINKING"],
    ],
  },
  {
    plugin: "RPG",
    tasks: [
      ["KP 叙事 / 工具", "RPG_KP_LLM_PROFILE", "RPG_KP_THINKING"],
      ["NPC 路由", "RPG_NPC_ROUTER_LLM_PROFILE", "RPG_NPC_ROUTER_THINKING"],
      ["NPC 短对白", "RPG_NPC_LLM_PROFILE", "RPG_NPC_THINKING"],
    ],
  },
  {
    plugin: "狼人杀",
    tasks: [
      ["AI 行动决策", "WW_DECISION_LLM_PROFILE", "WW_DECISION_THINKING"],
      ["AI 短发言", "WW_SPEECH_LLM_PROFILE", "WW_SPEECH_THINKING"],
    ],
  },
] as const;

export const PLUGIN_META: Record<string, { icon: string; tone: string }> = {
  Agent: { icon: "🌸", tone: "tone-lavender" },
  RPG: { icon: "🎲", tone: "tone-mint" },
  "狼人杀": { icon: "🐺", tone: "tone-sky" },
};

export const SECTION_ICONS: Record<string, string> = {
  "NoneBot 运行时": "🤖",
  "本地数据与 SQLite/ORM": "💾",
  "OneBot V11 连接": "📡",
  "Sentry 可选错误上报": "🛟",
  "OpenAI-compatible 服务": "🧠",
  "Core / Agent 管理 WebUI": "🖥️",
  "子插件 LLM 任务路由、Agent 媒体和文件工具": "🔀",
  "Core/Agent AI 开关": "🔆",
  "Agent 全局默认人设": "🎀",
  "番茄小说插件（可选）": "📚",
  "游戏插件常用覆盖": "🎮",
  "维护提示": "🔧",
  "自定义配置": "✨",
  "其他配置": "🌸",
};

export const MODEL_PANEL_KEYS = MODEL_CONFIGS.flatMap((item) => [
  item.modelKey,
  item.providerKey,
  item.thinkingKey,
  ...(item.multimodalKey ? [item.multimodalKey] : []),
]);

export const TASK_PANEL_KEYS = TASK_CONFIGS.flatMap((group) =>
  group.tasks.flatMap(([, profileKey, thinkingKey]) => [profileKey, thinkingKey]),
);

export const LLM_CONFIG_KEYS = new Set<string>([
  ...MODEL_PANEL_KEYS,
  ...TASK_PANEL_KEYS,
  "AI_BASE_URL",
  "AI_API_KEY",
  "AI_PROVIDERS",
  "AI_PROVIDER_API_KEYS",
]);

export const SOURCE_META: Record<EnvironmentValueSource, { label: string; color: string }> = {
  process: { label: "进程环境覆盖", color: "red" },
  environment: { label: "环境文件覆盖", color: "orange" },
  env: { label: "根 .env", color: "green" },
  default: { label: "默认值", color: "default" },
};

const ENUM_LABELS: Record<string, string> = {
  default: "默认模型",
  light: "轻量模型",
  vision: "识图模型",
  inherit: "继承模型档位",
  auto: "自动",
  enabled: "开启推理",
  disabled: "关闭推理",
  supported: "支持图片",
  unsupported: "不支持图片",
};

export function enumLabel(item: EnvironmentEntry, value: string): string {
  if (value === "auto") {
    return item.key.endsWith("_MULTIMODAL")
      ? "auto（自动探测图片能力）"
      : "auto（不发送推理参数）";
  }
  return `${value}（${ENUM_LABELS[value] ?? value}）`;
}

export function filterEnvironmentEntries(
  entries: EnvironmentEntry[],
  search: string,
): EnvironmentEntry[] {
  const needle = search.trim().toLocaleLowerCase();
  if (!needle) return entries;
  return entries.filter((item) =>
    [item.key, item.section, item.description].some((value) =>
      value.toLocaleLowerCase().includes(needle),
    ),
  );
}

export function groupEnvironmentEntries(
  entries: EnvironmentEntry[],
): Array<{ section: string; entries: EnvironmentEntry[] }> {
  const groups = new Map<string, EnvironmentEntry[]>();
  for (const item of entries) {
    const group = groups.get(item.section) ?? [];
    group.push(item);
    groups.set(item.section, group);
  }
  return [...groups].map(([section, grouped]) => ({ section, entries: grouped }));
}
