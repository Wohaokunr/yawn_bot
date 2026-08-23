import { describe, expect, it } from "vitest";
import {
  MEMORY_TYPE_META,
  memberDisplayName,
  memoryTypeLabel,
  PROFILE_KEY_META,
  profileKeyLabel,
} from "./agent";

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
