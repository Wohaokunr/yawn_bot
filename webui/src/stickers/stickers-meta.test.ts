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
} from "./stickers-meta";
import type { Sticker, StickerSettings } from "./types";

function sticker(overrides: Partial<Sticker>): Sticker {
  return {
    id: "s1",
    name: "狗头",
    tags: ["调侃"],
    description: "",
    enabled: true,
    status: "active",
    groups: [],
    source: "upload",
    addedBy: null,
    addedGroup: null,
    size: 1024,
    mime: "image/png",
    createdAt: null,
    updatedAt: null,
    uses: 0,
    lastUsed: null,
    fileMissing: false,
    ai: null,
    origin: null,
    canUndoLabel: false,
    placeholderName: false,
    ...overrides,
  };
}

const settings: StickerSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  frequency: "normal",
  groupCooldownSeconds: 60,
  groupDailyLimit: 60,
  maxPerMessage: 1,
  menuSize: 12,
  allowProactive: true,
  collectMode: "review",
  collectAdminOnly: false,
  memberDailyCollect: 10,
  maxBytes: 3145728,
  privateEnabled: true,
  privateDailyLimit: 20,
  privateCooldownSeconds: 20,
  aiLabelEnabled: true,
  aiLabelOnUpload: true,
  aiLabelOnCollect: true,
  aiLabelStyle: "",
  aiLabelReviewUnsafe: true,
  stealMode: "auto",
  stealPhotos: false,
  groupDailySteals: 5,
  stealCooldownSeconds: 300,
  privateStealEnabled: true,
  privateDailySteals: 5,
  stealProtectedUsers: [],
};

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
});
