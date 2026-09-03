import type { PersonaBehavior, PersonaEmotion, PersonaProfile, PersonaPreset } from "./types";

export const MEMORY_TYPE_META: Record<string, { label: string; color: string }> = {
  summary: { label: "群摘要", color: "geekblue" },
  profile: { label: "成员画像", color: "purple" },
  core: { label: "核心记忆", color: "red" },
  manual: { label: "置顶事实", color: "gold" },
};

export function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_META[type]?.label ?? type;
}

// 画像键中文标签：与后端 memory.py 的 _FACT_KEYS 对齐（多值键内容以「、」连接），
// 手工新增的自定义键原样展示。
export const PROFILE_KEY_META: Record<string, string> = {
  display_name: "昵称/自称",
  preferred_address: "偏好称呼",
  hobby: "爱好",
  preference: "偏好",
  skill: "技能",
  recurring_topic: "常聊话题",
};

export function profileKeyLabel(key: string): string {
  return PROFILE_KEY_META[key] ?? key;
}

export type PersonaTraitKey =
  | "warmth"
  | "humor"
  | "directness"
  | "verbosity"
  | "expressiveness"
  | "sociability"
  | "followupTendency"
  | "reactionTendency";

export interface PersonaFormValues {
  mode: "inherit" | "custom";
  profile: PersonaProfile;
}

export const PERSONA_TRAIT_META: Record<PersonaTraitKey, { label: string; help: string; levels: string[] }> = {
  warmth: { label: "温和程度", help: "控制措辞的温度，不改变事实与安全边界。", levels: ["偏冷淡", "较克制", "自然", "温和", "很温暖"] },
  humor: { label: "幽默程度", help: "控制玩梗与轻松表达的频率。", levels: ["不玩梗", "偶尔", "适度", "会接梗", "很会接梗"] },
  directness: { label: "直接程度", help: "控制结论是委婉表达还是直接说明。", levels: ["很委婉", "偏委婉", "适中", "较直接", "很直接"] },
  verbosity: { label: "回复详略", help: "控制通常回复的展开程度；复杂问题仍可按需说明。", levels: ["极简", "简洁", "适中", "较详细", "很详细"] },
  expressiveness: { label: "表现力", help: "控制感叹、语气变化与情绪表现的明显程度。", levels: ["很淡", "克制", "自然", "明显", "很强"] },
  sociability: { label: "社交活跃度", help: "描述角色愿不愿意参与群聊；不会突破运行配置的主动发言上限。", levels: ["很少参与", "偏安静", "平衡", "较主动", "很活跃"] },
  followupTendency: { label: "续聊倾向", help: "控制回答后是否倾向自然延展话题。", levels: ["不续聊", "很少", "适度", "较愿意", "很愿意"] },
  reactionTendency: { label: "接梗 / 反应", help: "控制对群友玩笑、表情和气氛变化的回应倾向。", levels: ["几乎不接", "较少", "自然", "较常", "很爱接"] },
};

export const PERSONA_STYLE_TRAITS: PersonaTraitKey[] = ["warmth", "humor", "directness", "verbosity", "expressiveness"];
export const PERSONA_SOCIAL_TRAITS: PersonaTraitKey[] = ["sociability", "followupTendency", "reactionTendency"];

export const PERSONA_TRIAL_SCENARIOS = [
  { value: "ordinary", label: "普通问题", mode: "dialogue", text: "今天适合做什么？" },
  { value: "joke", label: "群友玩梗", mode: "active", text: "你又来晚了，罚你讲个冷笑话。" },
  { value: "cold", label: "群聊冷场", mode: "warmup", text: "群里安静半天了。" },
  { value: "followup", label: "自然续聊", mode: "followup", text: "刚才的话题还有一点可以接。" },
  { value: "challenge", label: "成员质疑", mode: "dialogue", text: "你刚才是不是在瞎说？" },
  { value: "custom", label: "自定义", mode: "dialogue", text: "" },
] as const;

export function personaDraftSummary(profile: PersonaProfile, presets: PersonaPreset[]): string {
  const preset = presets.find((item) => item.id === profile.presetId);
  const meta = PERSONA_TRAIT_META;
  return [
    profile.name,
    preset?.label ?? profile.presetId,
    meta.warmth.levels[profile.warmth],
    meta.humor.levels[profile.humor],
    meta.verbosity.levels[profile.verbosity],
    meta.sociability.levels[profile.sociability],
  ].filter(Boolean).join(" · ");
}

export function mergePersonaPreset(profile: PersonaProfile, preset: PersonaPreset): PersonaProfile {
  return {
    ...profile,
    presetId: preset.id,
    identity: preset.identity,
    groupRole: preset.groupRole,
    warmth: preset.warmth,
    humor: preset.humor,
    directness: preset.directness,
    verbosity: preset.verbosity,
    expressiveness: preset.expressiveness,
    sociability: preset.sociability,
    followupTendency: preset.followupTendency,
    reactionTendency: preset.reactionTendency,
  };
}

export function personaBehaviorPreview(profile: PersonaProfile): PersonaBehavior {
  const probabilityScales = [0.15, 0.45, 0.75, 0.9, 1] as const;
  const maxTurns = [1, 2, 3, 4, 4] as const;
  const reactionModes = ["off", "restrained", "normal", "expressive", "high"] as const;
  const sociability = Math.max(0, Math.min(4, profile.sociability));
  const followup = Math.max(0, Math.min(4, profile.followupTendency));
  const reaction = Math.max(0, Math.min(4, profile.reactionTendency));
  return {
    source: "draft",
    sociability,
    followupTendency: followup,
    reactionTendency: reaction,
    warmupProbabilityScale: probabilityScales[sociability],
    activeProbabilityScale: probabilityScales[sociability],
    maxFollowupBotTurns: maxTurns[followup],
    allowSpontaneousReaction: reaction >= 2,
    reactionMode: reactionModes[reaction],
  };
}

export function personaEmotionExpressionPreview(
  emotion: PersonaEmotion,
  expressiveness: number,
): number {
  const scales = [0.2, 0.4, 0.62, 0.82, 1] as const;
  const level = Math.max(0, Math.min(4, Math.round(expressiveness)));
  return Math.max(0, Math.min(1, emotion.intensity * scales[level]));
}

// 画像成员的展示名：群名片优先、全局昵称兜底，解析失败回退 QQ 号（与关系图谱同口径）。
export function memberDisplayName(
  groupNickname: string | null | undefined,
  nickname: string | null | undefined,
  userId: string,
): string {
  return (groupNickname || nickname || "").trim() || userId;
}

export const RELATION_TYPE_PRESETS = ["好友", "死党", "情侣", "伴侣", "亲属", "师徒", "同事", "同学", "搭子", "对立"];
export const RELATION_SOURCE_META: Record<string, { label: string; color: string }> = {
  manual: { label: "手工", color: "gold" },
  auto: { label: "自动", color: "default" },
  mention: { label: "提及", color: "blue" },
  agent: { label: "Agent", color: "green" },
};

export const MEMORY_ROLE_OPTIONS = [
  { value: "", label: "全部角色" },
  { value: "member", label: "成员" },
  { value: "admin", label: "管理员" },
  { value: "owner", label: "群主" },
  { value: "bot", label: "Bot" },
];
