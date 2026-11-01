import { describe, expect, it } from "vitest";
import {
  breadcrumbs,
  canPreviewText,
  childDirs,
  filesInDir,
  inboxStatusMeta,
  inboxViaLabel,
  isInboxPath,
  joinPath,
  kindCounts,
  nextFreeName,
  parseCsvPreview,
  prettyJson,
  readSnippet,
  supportsFeature,
  validateRelativePath,
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
  inboxEnabled: true,
  inboxAutoImport: "sandbox_turns",
  inboxTypes: ["image", "file", "record", "video"],
  inboxMaxFileMb: 20,
  inboxMaxPerTurn: 6,
  inboxHistoryMinutes: 180,
  inboxGroupDailyMb: 500,
  privateEnabled: true,
  privateDailyRuns: 500,
  privateInbox: true,
  privateInboxDailyMb: 200,
  privateInboxMaxFileMb: 1024,
  privateDiskQuotaMb: 5120,
  privateFileMb: 1024,
  privateNetwork: true,
  groupNetwork: false,
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

describe("文件浏览与附件辅助函数", () => {
  const files = [
    { path: "inbox/a.png", size: 10, mtime: 1, kind: "image" as const, mime: "image/png" },
    { path: "inbox/sub/b.mp3", size: 20, mtime: 2, kind: "audio" as const, mime: "audio/mpeg" },
    { path: "c.csv", size: 5, mtime: 3, kind: "table" as const, mime: "text/csv" },
  ];

  it("在旧 runner 上从文件路径推导子目录", () => {
    expect(childDirs("", undefined, files)).toEqual([{ path: "inbox", files: 2, bytes: 30 }]);
    expect(childDirs("inbox", undefined, files)).toEqual([{ path: "inbox/sub", files: 1, bytes: 20 }]);
    expect(childDirs("", [{ path: "inbox", files: 9, bytes: 1 }, { path: "inbox/sub", files: 1, bytes: 1 }], files)).toEqual([{ path: "inbox", files: 9, bytes: 1 }]);
    expect(filesInDir(files, "").map((item) => item.path)).toEqual(["c.csv"]);
    expect(filesInDir(files, "inbox").map((item) => item.path)).toEqual(["inbox/a.png"]);
  });

  it("路径拼接、面包屑和同名改名", () => {
    expect(joinPath("uploads", "../evil\\x.txt")).toBe("uploads/evil/x.txt");
    expect(breadcrumbs("a/b").map((item) => item.path)).toEqual(["", "a", "a/b"]);
    expect(nextFreeName("data/报表.xlsx", new Set(["data/报表.xlsx", "data/报表 (2).xlsx"]))).toBe("data/报表 (3).xlsx");
    expect(nextFreeName("README", new Set(["README"]))).toBe("README (2)");
    expect(isInboxPath("inbox/a.png")).toBe(true);
    expect(isInboxPath("inboxes/a.png")).toBe(false);
  });

  it("校验相对路径", () => {
    expect(validateRelativePath("")).toBeTruthy();
    expect(validateRelativePath("/etc/passwd")).toBeTruthy();
    expect(validateRelativePath("a/../b")).toBeTruthy();
    expect(validateRelativePath("a\u0001b")).toBeTruthy();
    expect(validateRelativePath("data/结果.csv")).toBeNull();
  });

  it("按类型给读取代码", () => {
    expect(readSnippet("inbox/a.png", "image")).toContain('Image.open("inbox/a.png")');
    expect(readSnippet("x.tsv", "table")).toContain("sep='\\t'");
    expect(readSnippet("x.xlsx", "table")).toContain("read_excel");
    expect(readSnippet("v.wav", "audio")).toContain("soundfile");
    expect(readSnippet("z.zip", "archive")).toContain("zipfile");
  });

  it("CSV 预览支持引号与转义", () => {
    expect(parseCsvPreview('a,b\n"x, y","he said ""hi"""\r\n1,2')).toEqual([["a", "b"], ["x, y", 'he said "hi"'], ["1", "2"]]);
    expect(parseCsvPreview("a\tb\n1\t2", 10, "\t")).toEqual([["a", "b"], ["1", "2"]]);
    expect(parseCsvPreview("a\n1\n2\n3", 2)).toHaveLength(2);
  });

  it("入箱途径、状态和设置校验", () => {
    expect(inboxViaLabel("get_record:base64")).toBe("语音接口 · base64");
    expect(inboxViaLabel("group_file:url")).toBe("群文件链接 · 下载");
    expect(inboxStatusMeta("nope").label).toBe("nope");
    expect(prettyJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyJson("not json")).toBe("not json");
    expect(kindCounts(files)[0]).toEqual({ kind: "image", count: 1 });
    expect(supportsFeature(["rename"], "rename")).toBe(true);
    expect(supportsFeature(undefined, "rename")).toBe(false);
    expect(validateSettings({ ...defaults, inboxTypes: [] }).inboxTypes).toBeTruthy();
    expect(validateSettings({ ...defaults, inboxEnabled: false, inboxTypes: [] }).inboxTypes).toBeUndefined();
    expect(validateSettings({ ...defaults, inboxMaxFileMb: 64, inboxGroupDailyMb: 10 }).inboxMaxFileMb).toBeTruthy();
    expect(validateSettings({ ...defaults, inboxHistoryMinutes: 99999 }).inboxHistoryMinutes).toBeTruthy();
  });
});
