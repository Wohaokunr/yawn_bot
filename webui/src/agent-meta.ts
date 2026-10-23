import type { AgentAudit, AuditOutcome, PersonaBehavior, PersonaCard, PersonaEmotion, PersonaPreset } from "./types";

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
  dislike: "不喜欢",
  skill: "技能",
  recurring_topic: "常聊话题",
  current_status: "近期状态",
};

export function profileKeyLabel(key: string): string {
  return PROFILE_KEY_META[key] ?? key;
}

// 人设 v3 的六个档位；标签与后端 yawn_agent/persona.py 的 _TRAIT_TEXT 同一口径。
export type PersonaTraitKey =
  | "warmth"
  | "humor"
  | "verbosity"
  | "expressiveness"
  | "sociability"
  | "reactionTendency";

export interface PersonaFormValues {
  mode: "inherit" | "custom";
  card: PersonaCard;
}

export const PERSONA_TRAIT_META: Record<PersonaTraitKey, { label: string; help: string; levels: string[] }> = {
  warmth: { label: "温度", help: "措辞是冷淡还是温暖，不改变事实与安全边界。", levels: ["偏冷淡", "较克制", "自然", "温和", "很温暖"] },
  humor: { label: "幽默", help: "玩梗与轻松表达的频率。", levels: ["不主动玩梗", "偶尔轻幽默", "适度幽默", "比较会接梗", "很会接梗"] },
  verbosity: { label: "详略", help: "平时回复的长短；复杂问题仍会按需说明白。", levels: ["极简短", "简洁", "适中", "较详细", "很详细"] },
  expressiveness: { label: "表现力", help: "语气、感叹和情绪表现有多明显。", levels: ["情绪表达很淡", "表达克制", "自然表达", "较有表现力", "表现力很强"] },
  sociability: { label: "活跃度", help: "愿不愿意主动参与和续聊；不会突破运行设置的主动发言上限。", levels: ["很少参与", "偏安静", "社交平衡", "比较主动", "很活跃"] },
  reactionTendency: { label: "接梗", help: "对群友玩笑、表情和气氛变化的回应倾向。", levels: ["几乎不接反应", "少量反应", "自然反应", "较常接反应", "很爱接反应"] },
};

export const PERSONA_VOICE_TRAITS: PersonaTraitKey[] = ["warmth", "humor", "verbosity", "expressiveness"];
export const PERSONA_SOCIAL_TRAITS: PersonaTraitKey[] = ["sociability", "reactionTendency"];

// 输入上限与后端 PersonaCard 的裁剪上限一致；超出会被后端截断，这里提前提示。
export const PERSONA_LIMITS = {
  name: 32,
  alias: 16,
  identity: 160,
  longText: 400,
  catchphrases: 6,
  catchphraseLength: 20,
  topics: 8,
  topicLength: 24,
  examples: 4,
  exampleLength: 80,
} as const;

export const PERSONA_TRIAL_SCENARIOS = [
  { value: "ordinary", label: "普通问题", mode: "dialogue", text: "今天适合做什么？" },
  { value: "joke", label: "群友玩梗", mode: "active", text: "你又来晚了，罚你讲个冷笑话。" },
  { value: "cold", label: "群聊冷场", mode: "warmup", text: "群里安静半天了。" },
  { value: "followup", label: "自然续聊", mode: "followup", text: "刚才的话题还有一点可以接。" },
  { value: "challenge", label: "成员质疑", mode: "dialogue", text: "你刚才是不是在瞎说？" },
  { value: "custom", label: "自定义", mode: "dialogue", text: "" },
] as const;

function clampLevel(value: number | undefined): number {
  const level = Math.round(Number(value ?? 0));
  if (!Number.isFinite(level)) return 0;
  return Math.max(0, Math.min(4, level));
}

export function personaTraitLabel(key: PersonaTraitKey, value: number | undefined): string {
  return PERSONA_TRAIT_META[key].levels[clampLevel(value)];
}

/** 与后端 persona_card_summary 同口径：名字 · 自称 · 模板 · 温度/幽默/详略/活跃度。 */
export function personaDraftSummary(card: PersonaCard, presets: PersonaPreset[]): string {
  const preset = presets.find((item) => item.id === card.presetId);
  return [
    card.name,
    card.selfName ? `自称${card.selfName}` : "",
    preset?.label ?? "自定义",
    personaTraitLabel("warmth", card.warmth),
    personaTraitLabel("humor", card.humor),
    personaTraitLabel("verbosity", card.verbosity),
    personaTraitLabel("sociability", card.sociability),
  ].filter(Boolean).join(" · ");
}

/** 套用模板：替换角色内容与档位，保留名字（与后端 persona_apply_preset 一致）。 */
export function mergePersonaPreset(card: PersonaCard, preset: PersonaPreset): PersonaCard {
  return { ...preset.card, name: card.name };
}

/** 续聊轮数由活跃度推导（v3 不再单独设续聊倾向），与后端 persona_behavior_from_card 一致。 */
export function personaBehaviorPreview(card: Pick<PersonaCard, "sociability" | "reactionTendency">): PersonaBehavior {
  const probabilityScales = [0.15, 0.45, 0.75, 0.9, 1] as const;
  const maxTurns = [1, 2, 3, 4, 4] as const;
  const followupTendency = [0, 1, 2, 3, 3] as const;
  const reactionModes = ["off", "restrained", "normal", "expressive", "high"] as const;
  const sociability = clampLevel(card.sociability);
  const reaction = clampLevel(card.reactionTendency);
  return {
    source: "draft",
    sociability,
    followupTendency: followupTendency[sociability],
    reactionTendency: reaction,
    warmupProbabilityScale: probabilityScales[sociability],
    activeProbabilityScale: probabilityScales[sociability],
    maxFollowupBotTurns: maxTurns[sociability],
    allowSpontaneousReaction: reaction >= 2,
    reactionMode: reactionModes[reaction],
  };
}

/** 后端会丢掉空白条目；保存前在前端做同样的整理，避免“看起来改了”的假脏状态。 */
export function cleanPersonaCard(card: PersonaCard): PersonaCard {
  const list = (items: string[] | undefined) => (items ?? []).map((item) => String(item ?? "").trim()).filter(Boolean);
  return {
    ...card,
    name: (card.name ?? "").trim() || "Yawn",
    selfName: (card.selfName ?? "").trim(),
    userTitle: (card.userTitle ?? "").trim(),
    identity: (card.identity ?? "").trim(),
    personality: (card.personality ?? "").trim(),
    speakingStyle: (card.speakingStyle ?? "").trim(),
    catchphrases: list(card.catchphrases),
    likes: list(card.likes),
    avoidTopics: list(card.avoidTopics),
    examples: list(card.examples),
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

// 审计结果分类与后端 yawn_agent/audit_outcome.py 同一口径：主动发言的 wait/close
// 是“这会儿不说话”的正常决策，属于跳过而不是失败。
const AUDIT_SUCCESS_RESULTS = new Set(["success", "speak", "degraded_to_text"]);
const AUDIT_SKIPPED_RESULTS = new Set(["wait", "close"]);
const AUDIT_UNKNOWN_RESULTS = new Set(["delivery_unknown"]);

export const AUDIT_OUTCOME_META: Record<AuditOutcome, { label: string; color: string }> = {
  success: { label: "成功", color: "green" },
  skipped: { label: "跳过", color: "default" },
  failed: { label: "失败", color: "red" },
  unknown: { label: "待确认", color: "orange" },
};

export function auditOutcome(row: Pick<AgentAudit, "result" | "outcome">): AuditOutcome {
  if (row.outcome) return row.outcome;
  const value = String(row.result ?? "").trim().toLowerCase();
  if (AUDIT_SUCCESS_RESULTS.has(value)) return "success";
  if (AUDIT_SKIPPED_RESULTS.has(value)) return "skipped";
  if (AUDIT_UNKNOWN_RESULTS.has(value)) return "unknown";
  return "failed";
}

// 主动发言审计的 result 是参与决策；标签用更贴切的说法。
export function auditOutcomeLabel(row: Pick<AgentAudit, "result" | "outcome" | "toolName">): string {
  const outcome = auditOutcome(row);
  if (row.toolName === "proactive_reply") {
    if (outcome === "success") return "发言";
    if (outcome === "skipped") return "不说话";
  }
  return AUDIT_OUTCOME_META[outcome].label;
}
