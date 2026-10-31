import { describe, expect, it } from "vitest";
import {
  activeFilterCount,
  activityTone,
  batchSummary,
  featureMode,
  groupDisplayName,
  groupFiltersToParams,
  groupListQuery,
  groupOverridesByMember,
  inheritedValue,
  memberDisplayName,
  memberListQuery,
  modeToOverride,
  parseGroupFilters,
  parseGroupTab,
  parseMemberFilters,
  parseProfileTab,
  parseUserFilters,
  percent,
  redundantOverrides,
  relativeTime,
  roleMeta,
  sourceLabel,
  updateParams,
  userListQuery,
} from "./directory-meta";
import type { MemberOverride } from "./types";

const NOW = new Date("2026-10-31T12:00:00+08:00").getTime();
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

describe("URL 状态解析", () => {
  it("群列表：非法值回落到默认，合法值保留", () => {
    const filters = parseGroupFilters(new URLSearchParams("groups.agent=maybe&groups.sort=evil&groups.page=-3&groups.active=7d&groups.overrides=member&groups.q=猫"));
    expect(filters).toEqual({ q: "猫", agent: "", overrides: "member", active: "7d", sort: "active", page: 1 });
  });

  it("群列表：默认值不写进 URL，查询串带上筛选", () => {
    const params = groupFiltersToParams({ q: "", agent: "on", overrides: "", active: "", sort: "active", page: 1 });
    expect(params).toEqual({ "groups.q": "", "groups.agent": "on", "groups.overrides": null, "groups.active": null, "groups.sort": null, "groups.page": null });
    const query = new URLSearchParams(groupListQuery({ q: "猫", agent: "off", overrides: "any", active: "dormant", sort: "members", page: 2 }));
    expect(Object.fromEntries(query)).toEqual({ page: "2", pageSize: "20", sort: "members", search: "猫", agent: "off", overrides: "any", active: "dormant" });
  });

  it("成员与用户筛选", () => {
    const members = parseMemberFilters(new URLSearchParams("members.role=admin&members.overrides=1&members.sort=streak"));
    expect(members.role).toBe("admin");
    expect(members.overrides).toBe(true);
    expect(new URLSearchParams(memberListQuery(members)).get("overrides")).toBe("true");
    expect(parseMemberFilters(new URLSearchParams("members.role=root")).role).toBe("");
    const users = parseUserFilters(new URLSearchParams("users.overrides=private&users.sort=groups"));
    expect(new URLSearchParams(userListQuery(users)).get("overrides")).toBe("private");
    expect(users.sort).toBe("groups");
  });

  it("标签：群详情默认概览；档案在群里默认本群，在用户页没有本群", () => {
    expect(parseGroupTab(null)).toBe("overview");
    expect(parseGroupTab("overrides")).toBe("overrides");
    expect(parseGroupTab("nope")).toBe("overview");
    expect(parseProfileTab(null, true)).toBe("group");
    expect(parseProfileTab(null, false)).toBe("groups");
    expect(parseProfileTab("group", false)).toBe("groups");
    expect(parseProfileTab("agent", false)).toBe("agent");
  });

  it("updateParams 删除空值、保留其他参数", () => {
    const next = updateParams(new URLSearchParams("a=1&user=2"), { user: null, b: "3", c: "" });
    expect(next.toString()).toBe("a=1&b=3");
  });

  it("筛选计数", () => {
    expect(activeFilterCount({ q: "x", agent: "", overrides: true }, ["q", "agent", "overrides"])).toBe(2);
  });
});

describe("文案与推导", () => {
  it("角色与来源翻译成中文，Agent 总开关来源不再显示英文", () => {
    expect(roleMeta("owner").label).toBe("群主");
    expect(roleMeta("admin").label).toBe("管理员");
    expect(roleMeta(undefined).label).toBe("成员");
    expect(sourceLabel("agent_config")).toBe("Agent 总开关");
    expect(sourceLabel("global_user")).toBe("私聊覆盖");
  });

  it("相对时间与冷热度", () => {
    expect(relativeTime(null, NOW)).toBe("从未");
    expect(relativeTime(ago(0.5), NOW)).toBe("刚刚");
    expect(relativeTime(ago(5), NOW)).toBe("5 分钟前");
    expect(relativeTime(ago(180), NOW)).toBe("3 小时前");
    expect(relativeTime(ago(60 * 24 * 3), NOW)).toBe("3 天前");
    expect(relativeTime(ago(60 * 24 * 65), NOW)).toBe("2 个月前");
    expect(activityTone(ago(10), NOW)).toBe("hot");
    expect(activityTone(ago(600), NOW)).toBe("warm");
    expect(activityTone(ago(60 * 24 * 5), NOW)).toBe("quiet");
    expect(activityTone(null, NOW)).toBe("cold");
  });

  it("名称兜底", () => {
    expect(groupDisplayName({ groupId: "1", groupName: "  " })).toBe("未命名群 1");
    expect(memberDisplayName({ userId: "9", groupNickname: "", nickname: "阿狸" })).toBe("阿狸");
    expect(memberDisplayName({ userId: "9" })).toBe("QQ 9");
  });

  it("覆盖模式往返，继承值跟随上一级", () => {
    expect(featureMode(null)).toBe("inherit");
    expect(featureMode(false)).toBe("off");
    expect(modeToOverride("on")).toBe(true);
    expect(modeToOverride("inherit")).toBeNull();
    expect(inheritedValue("rpg")).toBe(true);
    expect(inheritedValue("rpg", [{ key: "rpg", effective: false }])).toBe(false);
    expect(inheritedValue("other", [{ key: "rpg", effective: false }])).toBe(true);
  });

  it("覆盖一览按成员聚合，挑出多余项", () => {
    const row = (userId: string, feature: string, redundant = false): MemberOverride => ({
      userId,
      name: `u${userId}`,
      role: "member",
      feature,
      featureName: feature,
      enabled: false,
      effective: false,
      groupEffective: !redundant,
      blockedByGroup: false,
      redundant,
    });
    const rows = [row("1", "rpg"), row("2", "rpg", true), row("2", "fanqie")];
    expect(groupOverridesByMember(rows).map((item) => [item.userId, item.items.length])).toEqual([["2", 2], ["1", 1]]);
    expect(redundantOverrides(rows)).toHaveLength(1);
  });

  it("批量说明与百分比", () => {
    expect(batchSummary(3, "rpg", false)).toBe("把 3 项的「跑团」设为显式关闭");
    expect(batchSummary(1, "fanqie", null)).toContain("恢复继承");
    expect(percent(1, 0)).toBe(0);
    expect(percent(1, 3)).toBe(33);
  });
});
