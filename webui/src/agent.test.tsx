import { describe, expect, it } from "vitest";
import {
  MEMORY_TYPE_META,
  debugMessageLabel,
  mergePersonaPreset,
  memberDisplayName,
  personaBehaviorPreview,
  personaEmotionExpressionPreview,
  memoryTypeLabel,
  personaDraftSummary,
  PROFILE_KEY_META,
  profileKeyLabel,
} from "./agent";
import type { PersonaPreset, PersonaProfile } from "./types";

describe("MEMORY_TYPE_META", () => {
  it("覆盖后端 memory_type 的全部已知取值，防止口径漂移", () => {
    expect(Object.keys(MEMORY_TYPE_META).sort()).toEqual([
      "core",
      "manual",
      "profile",
      "summary",
    ]);
  });

  it("每个类型都有非空中文标签与颜色", () => {
    for (const meta of Object.values(MEMORY_TYPE_META)) {
      expect(meta.label.length).toBeGreaterThan(0);
      expect(meta.color.length).toBeGreaterThan(0);
    }
  });
});

describe("memoryTypeLabel", () => {
  it("已知类型映射为中文标签", () => {
    expect(memoryTypeLabel("summary")).toBe("群摘要");
    expect(memoryTypeLabel("profile")).toBe("成员画像");
    expect(memoryTypeLabel("core")).toBe("核心记忆");
    expect(memoryTypeLabel("manual")).toBe("置顶事实");
  });

  it("未知类型回退为原值，不抛错", () => {
    expect(memoryTypeLabel("future_type")).toBe("future_type");
  });
});

describe("PROFILE_KEY_META", () => {
  it("覆盖后端 _FACT_KEYS 的全部画像键，防止口径漂移", () => {
    expect(Object.keys(PROFILE_KEY_META).sort()).toEqual([
      "display_name",
      "hobby",
      "preference",
      "preferred_address",
      "recurring_topic",
      "skill",
    ]);
  });

  it("每个画像键都有非空中文标签", () => {
    for (const label of Object.values(PROFILE_KEY_META)) {
      expect(label.length).toBeGreaterThan(0);
    }
  });
});

describe("profileKeyLabel", () => {
  it("已知画像键映射为中文标签", () => {
    expect(profileKeyLabel("display_name")).toBe("昵称/自称");
    expect(profileKeyLabel("recurring_topic")).toBe("常聊话题");
  });

  it("手工自定义键回退为原值，不抛错", () => {
    expect(profileKeyLabel("custom_key")).toBe("custom_key");
  });
});

describe("memberDisplayName", () => {
  it("群名片优先，其次全局昵称，均缺失回退 QQ 号", () => {
    expect(memberDisplayName("群里的我", "全局的我", "10001")).toBe("群里的我");
    expect(memberDisplayName(null, "全局的我", "10001")).toBe("全局的我");
    expect(memberDisplayName("  ", "", "10001")).toBe("10001");
    expect(memberDisplayName(undefined, undefined, "10001")).toBe("10001");
  });
});

describe("debugMessageLabel", () => {
  it("使用昵称与截断后的正文构造失败案例选项", () => {
    expect(debugMessageLabel({
      id: "1",
      messageId: "99",
      groupId: "10",
      userId: "10001",
      senderName: "小明",
      role: "member",
      title: null,
      text: "到底有没有一起玩",
      receivedAt: null,
      expiresAt: null,
    })).toBe("小明 · 到底有没有一起玩");
  });
});

describe("Persona v2 editor helpers", () => {
  const profile: PersonaProfile = {
    presetId: "natural",
    name: "Yawn",
    identity: "自然群友",
    groupRole: "普通群友",
    warmth: 2,
    humor: 1,
    directness: 2,
    verbosity: 1,
    expressiveness: 1,
    sociability: 2,
    followupTendency: 1,
    reactionTendency: 2,
    customNotes: "保留这条补充",
  };
  const preset: PersonaPreset = {
    id: "lively_sidekick",
    label: "活跃捧哏",
    description: "更会接梗",
    identity: "活跃群友",
    groupRole: "捧哏",
    warmth: 3,
    humor: 4,
    directness: 3,
    verbosity: 1,
    expressiveness: 4,
    sociability: 4,
    followupTendency: 3,
    reactionTendency: 4,
  };

  it("切换模板会更新结构化特征，但保留名字和自定义补充", () => {
    const next = mergePersonaPreset(profile, preset);
    expect(next.presetId).toBe("lively_sidekick");
    expect(next.humor).toBe(4);
    expect(next.sociability).toBe(4);
    expect(next.name).toBe("Yawn");
    expect(next.customNotes).toBe("保留这条补充");
  });

  it("动态情绪表达强度由 Persona 表现力收窄", () => {
    const emotion = {
      schemaVersion: 1,
      label: "amused",
      displayLabel: "愉快",
      valence: 0.6,
      arousal: 0.7,
      intensity: 0.8,
      expressionIntensity: 0.5,
      expressionHint: "带一点轻松",
      source: "direct",
      reason: "收到积极反馈",
      updatedAt: "2026-08-31T03:30:00+00:00",
      ageMinutesBucket: 0,
      eventCount: 1,
    };
    expect(personaEmotionExpressionPreview(emotion, 0)).toBeCloseTo(0.16);
    expect(personaEmotionExpressionPreview(emotion, 4)).toBeCloseTo(0.8);
  });

  it("结构化社交特征会编译成真实行为预览", () => {
    const quiet = personaBehaviorPreview({
      ...profile,
      sociability: 0,
      followupTendency: 0,
      reactionTendency: 1,
    });
    expect(quiet.activeProbabilityScale).toBe(0.15);
    expect(quiet.warmupProbabilityScale).toBe(0.15);
    expect(quiet.maxFollowupBotTurns).toBe(1);
    expect(quiet.allowSpontaneousReaction).toBe(false);

    const lively = personaBehaviorPreview({
      ...profile,
      sociability: 4,
      followupTendency: 3,
      reactionTendency: 4,
    });
    expect(lively.activeProbabilityScale).toBe(1);
    expect(lively.maxFollowupBotTurns).toBe(4);
    expect(lively.allowSpontaneousReaction).toBe(true);
    expect(lively.reactionMode).toBe("high");
  });

  it("草稿摘要只展示用户可理解的模板和特征", () => {
    const next = mergePersonaPreset(profile, preset);
    expect(personaDraftSummary(next, [preset])).toBe(
      "Yawn · 活跃捧哏 · 温和 · 很会接梗 · 简洁 · 很活跃",
    );
  });
});
