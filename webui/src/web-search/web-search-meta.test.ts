import { describe, expect, it } from "vitest";
import {
  cacheHitRate,
  describeWebAccess,
  formatYuan,
  monthlyEstimate,
  normalizeDomain,
  parseDomainList,
  parseGroupIds,
  scoreLabel,
  validateWebSettings,
} from "./web-search-meta";
import type { WebSearchSettings } from "./types";

const base: WebSearchSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  adminOnly: false,
  memberDailyCalls: 30,
  groupDailyCalls: 200,
  globalDailyCalls: 1000,
  memberCooldownSeconds: 3,
  maxCallsPerTurn: 6,
  defaultCount: 5,
  maxCount: 10,
  allowFullContent: false,
  fetchEnabled: true,
  maxFetchChars: 6000,
  cacheTtlMinutes: 30,
  blockedDomains: [],
};

describe("web search meta", () => {
  it("normalizes domains the same way as the backend", () => {
    expect(normalizeDomain("https://www.Example.com/path?q=1")).toBe("example.com");
    expect(normalizeDomain("*.bilibili.com")).toBe("bilibili.com");
    expect(normalizeDomain("localhost")).toBe("");
    const parsed = parseDomainList("a.com, www.a.com\nnot a domain");
    expect(parsed.domains).toEqual(["a.com"]);
    expect(parsed.invalid).toEqual(["not", "a", "domain"]);
  });

  it("parses group ids and flags invalid ones", () => {
    expect(parseGroupIds("123456，abc 123456 7890")).toEqual({ ids: ["123456", "7890"], invalid: ["abc"] });
  });

  it("validates ranges and the default/max count relation", () => {
    expect(validateWebSettings(base)).toEqual({});
    expect(validateWebSettings({ ...base, defaultCount: 12, maxCount: 10 }).defaultCount).toContain("不能大于");
    expect(validateWebSettings({ ...base, maxCount: 99 }).maxCount).toBeTruthy();
    expect(validateWebSettings({ ...base, groupMode: "allowlist" }).allowedGroups).toBeTruthy();
  });

  it("describes access and money in plain words", () => {
    expect(describeWebAccess({ ...base, enabled: false })).toContain("已关闭");
    expect(describeWebAccess({ ...base, adminOnly: true })).toContain("群主/管理员");
    expect(formatYuan(0)).toBe("¥0");
    expect(formatYuan(0.015)).toBe("¥0.015");
    expect(formatYuan(5)).toBe("¥5.00");
    expect(monthlyEstimate(1000, 0.005)).toBe("¥150.00");
    expect(cacheHitRate({ hits: 1, misses: 3 })).toBe("25%");
    expect(cacheHitRate({ hits: 0, misses: 0 })).toBe("—");
  });

  it("labels result quality from rerank score and authority", () => {
    expect(scoreLabel(null, null)).toBeNull();
    expect(scoreLabel(2.5, null)?.label).toBe("高");
    expect(scoreLabel(1.6, null)?.label).toBe("较高");
    expect(scoreLabel(0.5, null)?.label).toBe("低");
    expect(scoreLabel(1.0, 1)?.label).toBe("一般");
  });
});
