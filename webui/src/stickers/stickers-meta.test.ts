import { describe, expect, it } from "vitest";
import {
  applySuggestion,
  cleanName,
  confidenceText,
  describeJob,
  describeOrigin,
  describeSteal,
  isAiLabeled,
  jobPercent,
  looksLikePlaceholder,
  describeStickerAccess,
  filterStickers,
  nameFromFile,
  parseTags,
  stickerState,
  validateStickerSettings,
  cleanAliases,
  describeOverride,
  describeQuiet,
  emptyOverride,
  formatEta,
  healthTone,
  historySeries,
  isClock,
  overrideChanges,
  overrideIsEmpty,
  parseEnum,
  sortStickers,
  sparkPoints,
  trashDaysLeft,
  trendText,
} from "./stickers-meta";
import { fixtureSettings, fixtureSticker } from "./sticker-fixtures";
import type { Sticker, StickerSettings } from "./types";

function sticker(overrides: Partial<Sticker>): Sticker {
  return fixtureSticker({ size: 1024, createdAt: null, updatedAt: null, uses: 0, ...overrides });
}

const settings: StickerSettings = fixtureSettings;

describe("stickers-meta", () => {
  it("classifies and filters stickers", () => {
    const rows = [
      sticker({ id: "a", name: "狗头", tags: ["调侃"] }),
      sticker({ id: "b", name: "白眼", tags: ["无语"], status: "pending" }),
      sticker({ id: "c", name: "捂脸", tags: ["无语"], enabled: false }),
    ];
    expect(rows.map(stickerState)).toEqual(["active", "pending", "disabled"]);
    expect(filterStickers(rows, "all", "", "无语").map((row) => row.id)).toEqual(["b", "c"]);
    expect(filterStickers(rows, "pending", "", null).map((row) => row.id)).toEqual(["b"]);
    expect(filterStickers(rows, "all", "狗", null).map((row) => row.id)).toEqual(["a"]);
  });

  it("parses tags and names like the backend", () => {
    expect(parseTags("尴尬, 好笑 无语，尴尬 [坏]")).toEqual(["尴尬", "好笑", "无语", "坏"]);
    expect(cleanName("  [表情]:狗头  ")).toBe("表情 狗头");
    expect(nameFromFile("微信图片_20261027101010.png")).toBe("");
    expect(nameFromFile("doge_smile.gif")).toBe("doge smile");
  });

  it("validates settings and describes access", () => {
    expect(validateStickerSettings(settings)).toEqual({});
    expect(validateStickerSettings({ ...settings, maxPerMessage: 5 })).toHaveProperty("maxPerMessage");
    expect(validateStickerSettings({ ...settings, groupMode: "allowlist" })).toHaveProperty("allowedGroups");
    expect(describeStickerAccess(settings)).toContain("所有群可用");
    expect(describeStickerAccess({ ...settings, enabled: false })).toContain("已关闭");
  });
  it("filters by source, AI state and OCR text", () => {
    const ai = { model: "m", confidence: 0.9, kind: "reaction", safe: true, text: "我太难了", labeledAt: null, undone: false };
    const rows = [
      sticker({ id: "a", source: "stolen", ai }),
      sticker({ id: "b", source: "upload" }),
      sticker({ id: "c", ai: { ...ai, safe: false, text: "" } }),
      sticker({ id: "d", ai: { ...ai, undone: true, text: "" } }),
    ];
    expect(filterStickers(rows, "all", "", null, "stolen").map((s) => s.id)).toEqual(["a"]);
    expect(filterStickers(rows, "all", "", null, "any", "labeled").map((s) => s.id)).toEqual(["a", "c"]);
    expect(filterStickers(rows, "all", "", null, "any", "unlabeled").map((s) => s.id)).toEqual(["b", "d"]);
    expect(filterStickers(rows, "all", "", null, "any", "unsafe").map((s) => s.id)).toEqual(["c"]);
    expect(filterStickers(rows, "all", "太难", null).map((s) => s.id)).toEqual(["a"]);
    expect(isAiLabeled(rows[3])).toBe(false);
  });

  it("formats AI confidence, jobs and origins", () => {
    expect(confidenceText(0.834)).toBe("把握 83%");
    expect(confidenceText(null)).toBe("");
    expect(confidenceText(3)).toBe("把握 100%");
    const job = { id: "j1", status: "done" as const, mode: "fill" as const, modeLabel: "只补空的", fields: [], total: 4, done: 4, changed: 3, unchanged: 0, failed: 1, held: 1, createdAt: 0, finishedAt: 1, elapsedMs: 1 };
    expect(jobPercent({ done: 1, total: 4 })).toBe(25);
    expect(jobPercent({ done: 0, total: 0 })).toBe(100);
    expect(describeJob(job)).toBe("标完 4 张：改了 3 张，1 张失败，1 张转待审核");
    expect(describeJob({ ...job, status: "running", done: 2, changed: 0, failed: 0, held: 0 })).toBe("正在标注 2/4");
    const stolen = sticker({ origin: { via: "steal", groupId: "1", senderId: "42", senderName: "小明", messageId: null, qqSticker: true } });
    expect(describeOrigin(stolen, { "1": "测试群" })).toContain("测试群 · 小明（42） 发的 · 原本就是 QQ 表情包");
    expect(describeOrigin(sticker({}), {})).toBe("");
  });

  it("fills AI suggestions only into empty or placeholder fields", () => {
    const label = { ok: true, name: "无语", tags: ["白眼", "无奈"], description: "", text: "", kind: "reaction", kindLabel: "表情", safe: true, confidence: 0.8, model: "m", reason: "", reasonText: null, cached: false, durationMs: 1, warnings: [] };
    expect(applySuggestion({ name: "", tags: "" }, label, false)).toEqual({ name: "无语", tags: "白眼，无奈" });
    expect(applySuggestion({ name: "我的名字", tags: "自定" }, label, false)).toEqual({ name: "我的名字", tags: "自定" });
    expect(applySuggestion({ name: "IMG_2034", tags: "" }, label, true).name).toBe("无语");
    expect(applySuggestion({ name: "", tags: "" }, { ...label, ok: false }, false)).toEqual({ name: "", tags: "" });
    expect(looksLikePlaceholder("IMG_2034")).toBe(true);
    expect(looksLikePlaceholder("微信图片_20261101")).toBe(true);
    expect(looksLikePlaceholder("狗头")).toBe(false);
  });

  it("describes steal settings and validates new ranges", () => {
    expect(describeSteal(settings)).toContain("每群每天最多 5 张");
    expect(describeSteal({ ...settings, stealMode: "off" })).toBe("Bot 不会偷图");
    expect(validateStickerSettings({ ...settings, groupDailySteals: -1 })).toHaveProperty("groupDailySteals");
    expect(validateStickerSettings({ ...settings, aiLabelStyle: "x".repeat(400) })).toHaveProperty("aiLabelStyle");
  });

  it("filters by pack, mood and flags, and searches aliases and notes", () => {
    const rows = [
      sticker({ id: "a", name: "狗头", aliases: ["doge"], packs: ["p1"], pinned: true }),
      sticker({ id: "b", name: "猫", note: "群主专属", animated: true, ai: { model: "m", confidence: 0.9, kind: "sticker", safe: true, text: "", labeledAt: null, undone: false, mood: "开心" } }),
      sticker({ id: "c", name: "旧图", uses: 4, groups: ["1"], weight: 0, dailyCap: 2, fileMissing: true }),
    ];
    const ids = (extra: Parameters<typeof filterStickers>[6], keyword = "") => filterStickers(rows, "all", keyword, null, "any", "any", extra).map((row) => row.id);
    expect(ids({ pack: "p1" })).toEqual(["a"]);
    expect(ids({ mood: "开心" })).toEqual(["b"]);
    expect(ids({ flag: "pinned" })).toEqual(["a"]);
    expect(ids({ flag: "animated" })).toEqual(["b"]);
    expect(ids({ flag: "never" })).toEqual(["a", "b"]);
    expect(ids({ flag: "unpacked" })).toEqual(["b", "c"]);
    expect(ids({ flag: "scoped" })).toEqual(["c"]);
    expect(ids({ flag: "silent" })).toEqual(["c"]);
    expect(ids({ flag: "capped" })).toEqual(["c"]);
    expect(ids({ flag: "missing" })).toEqual(["c"]);
    expect(ids({}, "DOGE")).toEqual(["a"]);
    expect(ids({}, "群主")).toEqual(["b"]);
  });

  it("sorts without mutating and keeps pinned first by default", () => {
    const rows = [
      sticker({ id: "a", name: "乙", uses: 1, createdAt: "2026-10-01 00:00:00", size: 10 }),
      sticker({ id: "b", name: "甲", uses: 9, createdAt: "2026-10-03 00:00:00", size: 30, pinned: true, lastUsed: "2026-10-05 00:00:00" }),
      sticker({ id: "c", name: "丙", uses: 5, createdAt: "2026-10-02 00:00:00", size: 20 }),
    ];
    const order = (sort: Parameters<typeof sortStickers>[1]) => sortStickers(rows, sort).map((row) => row.id);
    expect(order("default")).toEqual(["b", "a", "c"]);
    expect(order("newest")).toEqual(["b", "c", "a"]);
    expect(order("oldest")).toEqual(["a", "c", "b"]);
    expect(order("most")).toEqual(["b", "c", "a"]);
    expect(order("least")).toEqual(["a", "c", "b"]);
    expect(order("size")).toEqual(["b", "c", "a"]);
    expect(order("recent")[0]).toBe("b");
    expect(rows.map((row) => row.id)).toEqual(["a", "b", "c"]);
    expect(parseEnum("most", ["default", "most"], "default")).toBe("most");
    expect(parseEnum("nope", ["default", "most"], "default")).toBe("default");
  });

  it("cleans aliases like the backend", () => {
    expect(cleanAliases(["狗头", " doge ", "DOGE", "a", "b", "c", "d"], "狗头")).toEqual(["doge", "a", "b", "c"]);
    expect(cleanAliases(["[坏]", ""], "x")).toEqual(["坏"]);
  });

  it("describes group overrides and spots empty ones", () => {
    const blank = emptyOverride();
    expect(overrideIsEmpty(blank)).toBe(true);
    expect(describeOverride(blank)).toBe("和全局一样");
    const changed = { ...blank, enabled: false, frequency: "often" as const, menuSize: 6 };
    expect(overrideChanges(changed)).toEqual(["frequency", "menuSize"]);
    expect(describeOverride(changed)).toBe("单独关闭，改了性格、菜单大小");
    expect(overrideIsEmpty({ ...blank, note: "只有备注" })).toBe(true);
  });

  it("validates quiet hours, confidence and per-group numbers", () => {
    expect(validateStickerSettings(settings)).toEqual({});
    expect(validateStickerSettings({ ...settings, quietStart: "25:00" }).quietStart).toBeTruthy();
    expect(validateStickerSettings({ ...settings, reviewBelowConfidence: 2 }).reviewBelowConfidence).toBeTruthy();
    expect(validateStickerSettings({ ...settings, trashDays: 400 }).trashDays).toBeTruthy();
    expect(validateStickerSettings({ ...settings, groupOverrides: { "123456": { ...emptyOverride(), menuSize: 99 } } }).groupOverrides).toContain("菜单大小");
    expect(validateStickerSettings({ ...settings, groupOverrides: { abc: emptyOverride() } }).groupOverrides).toContain("群号");
    expect(isClock("07:30")).toBe(true);
    expect(isClock("7:30")).toBe(false);
  });

  it("writes quiet hours, trends and ETA in plain words", () => {
    expect(describeQuiet(settings)).toBe("没有安静时段");
    expect(describeQuiet({ quietEnabled: true, quietStart: "23:00", quietEnd: "07:00" })).toBe("23:00 到第二天 07:00 不发表情包");
    expect(describeQuiet({ quietEnabled: true, quietStart: "08:00", quietEnd: "08:00" })).toBe("全天不发表情包");
    expect(describeStickerAccess({ ...settings, groupHourlyLimit: 6, quietEnabled: true })).toContain("每小时 6 张");
    expect(trendText({ recent: 15, previous: 10, changePercent: 50 })).toBe("比上一周期多 50%");
    expect(trendText({ recent: 5, previous: 10, changePercent: -50 })).toBe("比上一周期少 50%");
    expect(trendText({ recent: 5, previous: 5, changePercent: 0 })).toBe("和上一周期持平");
    expect(trendText({ recent: 5, previous: 0, changePercent: null })).toBe("上一周期没有数据");
    expect(trendText(undefined)).toBe("");
    expect(formatEta(42000)).toBe("大约还要 42 秒");
    expect(formatEta(150000)).toBe("大约还要 3 分钟");
    expect(formatEta(null)).toBe("");
  });

  it("draws sparklines and reads history series", () => {
    expect(sparkPoints([])).toBe("");
    expect(sparkPoints([0, 10], 32)).toBe("0.0,31.0 100.0,1.0");
    expect(sparkPoints([5], 32)).toBe("0.0,1.0");
    expect(historySeries([{ day: "d", sent: 3, dropped: 0, collected: 0, stolen: 0, labeled: 0, private_sent: 1 }], "private_sent")).toEqual([1]);
  });

  it("grades health scores and counts down the trash", () => {
    expect([healthTone(100), healthTone(70), healthTone(40)]).toEqual(["ok", "warn", "bad"]);
    const now = new Date("2026-11-11T00:00:00");
    expect(trashDaysLeft("2026-11-10 00:00:00", 14, now)).toBe(13);
    expect(trashDaysLeft("2026-10-01 00:00:00", 14, now)).toBe(0);
    expect(trashDaysLeft("2026-11-10 00:00:00", 0, now)).toBeNull();
    expect(trashDaysLeft(null, 14, now)).toBeNull();
  });
});
