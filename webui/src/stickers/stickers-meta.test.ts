import { describe, expect, it } from "vitest";
import {
  cleanName,
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
});
