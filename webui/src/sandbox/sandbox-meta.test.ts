import { describe, expect, it } from "vitest";
import {
  canPreviewText,
  describeAccess,
  formatBytes,
  formatDuration,
  formatLimit,
  inlineMediaKind,
  parseGroupList,
  parseSandboxTab,
  relativeEpoch,
  runStatusMeta,
  updateSearchParams,
  validateSettings,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxSettings } from "./types";

const defaults: SandboxSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  adminOnly: false,
  memberDailyRuns: 20,
  groupDailyRuns: 120,
  memberCooldownSeconds: 5,
  timeoutSeconds: 30,
  memoryMb: 1024,
  diskQuotaMb: 200,
  maxOutputChars: 3000,
  maxShareMb: 20,
  allowUnisolated: false,
};

describe("格式化", () => {
  it("字节按 1024 进位，保留一位小数", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(200 * 1024 * 1024)).toBe("200 MB");
  });

  it("耗时分毫秒/秒/分钟", () => {
    expect(formatDuration(420)).toBe("420 ms");
    expect(formatDuration(1234)).toBe("1.23 s");
    expect(formatDuration(45_600)).toBe("45.6 s");
    expect(formatDuration(125_000)).toBe("2 分 5 秒");
  });

  it("相对时间以 Unix 秒为单位", () => {
    const now = 1_700_000_000_000;
    expect(relativeEpoch(now / 1000 - 10, now)).toBe("刚刚");
    expect(relativeEpoch(now / 1000 - 600, now)).toBe("10 分钟前");
    expect(relativeEpoch(now / 1000 - 7200, now)).toBe("2 小时前");
    expect(relativeEpoch(null, now)).toBe("—");
  });

  it("限额键转成中文", () => {
    expect(formatLimit("timeoutSeconds", 30)).toBe("时限 30s");
    expect(formatLimit("outputBytes", 65536)).toBe("输出 64.0 KB");
    expect(formatLimit("unknownKey", 3)).toBe("unknownKey 3");
  });
});

describe("工作区与状态", () => {
  it("群工作区显示群名和群号，admin 显示为控制台", () => {
    expect(workspaceLabel("g123456", "摸鱼群")).toBe("摸鱼群（123456）");
    expect(workspaceLabel("g123456")).toBe("群 123456");
    expect(workspaceLabel("admin")).toBe("管理员控制台");
    expect(workspaceLabel("custom")).toBe("custom");
  });

  it("未知运行状态原样展示而不是崩溃", () => {
    expect(runStatusMeta("ok").label).toBe("成功");
    expect(runStatusMeta("memory_limit").label).toBe("内存超限");
    expect(runStatusMeta("weird")).toEqual({ label: "weird", color: "default" });
  });
});

describe("预览判定", () => {
  it("xlsx 虽是表格但不能当文本预览", () => {
    expect(canPreviewText("data.csv", "table")).toBe(true);
    expect(canPreviewText("data.XLSX", "table")).toBe(false);
    expect(canPreviewText("chart.svg", "vector")).toBe(true);
    expect(canPreviewText("a.png", "image")).toBe(false);
  });

  it("只有浏览器能播放的格式才内联，SVG 永远不内联渲染", () => {
    expect(inlineMediaKind("a.png", "image")).toBe("image");
    expect(inlineMediaKind("a.bmp", "image")).toBeNull();
    expect(inlineMediaKind("a.svg", "vector")).toBeNull();
    expect(inlineMediaKind("song.wav", "audio")).toBe("audio");
    expect(inlineMediaKind("song.mid", "midi")).toBeNull();
  });
});

describe("URL 参数", () => {
  it("未知 tab 回落到概览", () => {
    expect(parseSandboxTab("files")).toBe("files");
    expect(parseSandboxTab("nope")).toBe("overview");
    expect(parseSandboxTab(null)).toBe("overview");
  });

  it("空值删除参数，其余参数保留", () => {
    const next = updateSearchParams(new URLSearchParams("sandbox.tab=runs&other=1"), { "sandbox.tab": null, "sandbox.ws": "g1" });
    expect(next.toString()).toBe("other=1&sandbox.ws=g1");
  });
});

describe("策略校验", () => {
  it("群号列表支持多种分隔符并去重", () => {
    expect(parseGroupList("123456, 123456\n7890123；abc 12")).toEqual({ ids: ["123456", "7890123"], invalid: ["abc", "12"] });
  });

  it("默认设置合法", () => {
    expect(validateSettings(defaults)).toEqual({});
  });

  it("越界、白名单为空、成员次数大于群次数都会报错", () => {
    const errors = validateSettings({
      ...defaults,
      timeoutSeconds: 200,
      groupMode: "allowlist",
      memberDailyRuns: 500,
      groupDailyRuns: 100,
    });
    expect(errors.timeoutSeconds).toContain("5–90");
    expect(errors.allowedGroups).toBeTruthy();
    expect(errors.memberDailyRuns).toBeTruthy();
  });

  it("一句话描述谁能用", () => {
    expect(describeAccess({ ...defaults, enabled: false })).toContain("已关闭");
    expect(describeAccess(defaults)).toBe("所有群的所有群成员可用，每人每天 20 次");
    expect(describeAccess({ ...defaults, adminOnly: true, groupMode: "allowlist", allowedGroups: ["123456"] })).toBe(
      "1 个白名单群的群主/管理员可用，每人每天 20 次",
    );
  });
});
