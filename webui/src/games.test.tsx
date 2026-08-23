import { describe, expect, it } from "vitest";
import { rpgActionOptions, wwSeatPosition } from "./games";

describe("wwSeatPosition", () => {
  it("1 号位在正上方", () => {
    const pos = wwSeatPosition(1, 9);
    expect(pos.x).toBeCloseTo(50, 5);
    expect(pos.y).toBeCloseTo(8, 5); // 50 - 42
  });

  it("绕圈均匀分布且回到起点", () => {
    const total = 12;
    for (let seat = 1; seat <= total; seat++) {
      const angle = ((seat - 1) / total) * Math.PI * 2 - Math.PI / 2;
      expect(wwSeatPosition(seat, total).x).toBeCloseTo(50 + 42 * Math.cos(angle), 5);
      expect(wwSeatPosition(seat, total).y).toBeCloseTo(50 + 42 * Math.sin(angle), 5);
    }
    // 13 号位(绕满一圈)与 1 号位重合
    const wrap = wwSeatPosition(13, 12);
    const origin = wwSeatPosition(1, 12);
    expect(wrap.x).toBeCloseTo(origin.x, 5);
    expect(wrap.y).toBeCloseTo(origin.y, 5);
  });

  it("人数为 0 时不产生 NaN", () => {
    const pos = wwSeatPosition(1, 0);
    expect(Number.isFinite(pos.x)).toBe(true);
    expect(Number.isFinite(pos.y)).toBe(true);
  });
});

describe("rpgActionOptions", () => {
  it("按进行中阶段提供玩家行动", () => {
    expect(rpgActionOptions("PLAY").map((item) => item.value)).toEqual(["SAY", "WAIT", "PASS_TURN"]);
  });

  it("报名阶段只提供管理员开局行动", () => {
    expect(rpgActionOptions("SIGNUP").map((item) => item.value)).toEqual(["MODULE_SELECT", "START_GAME"]);
  });

  it("其他阶段不允许管理台投递", () => {
    expect(rpgActionOptions("CHAR_CREATE")).toEqual([]);
    expect(rpgActionOptions(null)).toEqual([]);
  });
});
