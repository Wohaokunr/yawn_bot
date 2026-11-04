import { describe, expect, it } from "vitest";
import type { PersonaCard } from "../types";
import { homePathForRole, navForRole, selectedNavKey, USER_NAV } from "../nav-config";
import { MUTE_PRESETS, groupRoleLabel, isQqNumber, manageMinutesLabel, manageResultMeta, manageSourceLabel, normalizeSetupCode, passwordProblem, passwordStrength, personaDiff, personaFieldKey } from "./me-meta";

describe("passwordProblem mirrors the backend policy", () => {
  it("rejects weak passwords without echoing them", () => {
    expect(passwordProblem("short1")).toBe("密码至少 8 位");
    expect(passwordProblem("abcdefgh")).toMatch(/两种/);
    expect(passwordProblem("12345678")).not.toBeNull();
    expect(passwordProblem("password1")).toMatch(/常见/);
    expect(passwordProblem("has space 1")).toMatch(/空格/);
    expect(passwordProblem("aaaa1111")).toMatch(/简单/);
    expect(passwordProblem("x123456789y", "123456789")).toMatch(/QQ/);
    expect(passwordProblem("Mao-mao-2026", "123456789")).toBeNull();
    for (const weak of ["abcdefgh", "password1"]) expect(passwordProblem(weak) ?? "").not.toContain(weak);
  });

  it("rates strength and normalizes setup codes", () => {
    expect(passwordStrength("").label).toBe("");
    expect(passwordStrength("abcd1234").label).toBe("一般");
    expect(passwordStrength("Abcd-1234-xyz!").score).toBe(3);
    expect(normalizeSetupCode(" ab-cd 23ef ")).toBe("ABCD23EF");
    expect(isQqNumber("10001")).toBe(true);
    expect(isQqNumber("01234")).toBe(false);
    expect(isQqNumber("12a45")).toBe(false);
  });
});

describe("personaDiff", () => {
  const base = { name: "小橘", selfName: "我", userTitle: "", identity: "", personality: "慢吞吞", speakingStyle: "", catchphrases: [], likes: [], avoidTopics: [], examples: [], presetId: "natural", warmth: 2 } as unknown as PersonaCard;

  it("only submits changed fields and flags fields the policy does not open", () => {
    const after = { ...base, selfName: "本喵", catchphrases: ["喵"], identity: "海盗", presetId: "custom" } as PersonaCard;
    const diff = personaDiff(base, after, { self_name: "自称", catchphrases: "口癖" });
    expect(diff.fields).toEqual({ selfName: "本喵", catchphrases: ["喵"] });
    expect(diff.changed).toEqual(["自称", "口癖"]);
    expect(diff.blocked).toEqual(["identity"]);
    expect(personaDiff(base, { ...base }, { self_name: "自称" }).fields).toEqual({});
    expect(personaFieldKey("avoidTopics")).toBe("avoid_topics");
    expect(groupRoleLabel("owner")).toBe("群主");
  });
});

describe("user navigation", () => {
  it("keeps QQ accounts inside 我的空间", () => {
    expect(navForRole("user")).toBe(USER_NAV);
    expect(homePathForRole("user")).toBe("/me");
    expect(selectedNavKey("/me/groups/123", "user")).toBe("/me/groups");
    expect(selectedNavKey("/me/memory", "user")).toBe("/me/memory");
    expect(selectedNavKey("/me/nope", "user")).toBe("/me");
    expect(selectedNavKey("/me/persona", "user")).toBe("/me/persona");
    expect(selectedNavKey("/me/account", "user")).toBe("/me/account");
    expect(selectedNavKey("/", "guest")).toBe("/guest");
    expect(selectedNavKey("/agent/1", "admin")).toBe("/groups");
    expect(USER_NAV.every((entry) => entry.key.startsWith("/me"))).toBe(true);
  });
});

describe("群管代办文案", () => {
  it("分钟数换算和后端一致", () => {
    expect(manageMinutesLabel(10)).toBe("10 分钟");
    expect(manageMinutesLabel(60)).toBe("1 小时");
    expect(manageMinutesLabel(90)).toBe("90 分钟");
    expect(manageMinutesLabel(1440)).toBe("1 天");
    expect(manageMinutesLabel(43200)).toBe("30 天");
  });
  it("结果与来源", () => {
    expect(manageResultMeta("denied").label).toBe("被拒");
    expect(manageResultMeta("weird").label).toBe("weird");
    expect(manageSourceLabel("private")).toBe("私聊");
    expect(manageSourceLabel("webui")).toBe("网页");
    expect(MUTE_PRESETS[0]).toBe(10);
  });
});
