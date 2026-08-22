import { describe, expect, it } from "vitest";
import type { FanqieJob } from "./types";
import { coverThemeIndex, fanqieJobActions, fanqieRangeError, FANQIE_JOB_STATUS_META } from "./fanqie";

function job(overrides: Partial<FanqieJob>): FanqieJob {
  return {
    id: 1,
    bookId: "7123456789012345678",
    title: "测试书",
    author: "作者",
    requesterUserId: "10001",
    groupId: null,
    groupName: null,
    startChapter: 1,
    endChapter: 10,
    totalChapters: 10,
    completedChapters: 0,
    status: "queued",
    cancelRequested: false,
    outputName: null,
    sendStatus: "pending",
    lastError: null,
    sendError: null,
    createdAt: null,
    startedAt: null,
    completedAt: null,
    ...overrides,
  };
}

describe("fanqieRangeError", () => {
  const max = 500;

  it("合法范围返回 null", () => {
    expect(fanqieRangeError(1, 10, 100, max)).toBeNull();
    expect(fanqieRangeError(91, 100, 100, max)).toBeNull();
  });

  it("缺项、倒序与越界都有明确提示", () => {
    expect(fanqieRangeError(null, 10, 100, max)).toBe("请填写起止章节");
    expect(fanqieRangeError(1, null, 100, max)).toBe("请填写起止章节");
    expect(fanqieRangeError(0, 10, 100, max)).toBe("起始章必须从 1 开始");
    expect(fanqieRangeError(5, 4, 100, max)).toBe("结束章不能小于起始章");
    expect(fanqieRangeError(1, 101, 100, max)).toBe("本书共 100 章,结束章超出范围");
  });

  it("超过单次上限时提示上限值", () => {
    expect(fanqieRangeError(1, 501, 1000, max)).toBe("单次最多下载 500 章");
    expect(fanqieRangeError(1, 500, 1000, max)).toBeNull();
  });
});

describe("fanqieJobActions", () => {
  it("排队与下载中可取消,不可重试/发送", () => {
    expect(fanqieJobActions(job({ status: "queued" }))).toEqual({ cancel: true, retry: false, send: false });
    expect(fanqieJobActions(job({ status: "running" }))).toEqual({ cancel: true, retry: false, send: false });
  });

  it("已打取消标记的任务不能重复取消", () => {
    expect(fanqieJobActions(job({ status: "running", cancelRequested: true })).cancel).toBe(false);
  });

  it("失败与已取消可重试;已完成可发送", () => {
    expect(fanqieJobActions(job({ status: "failed" }))).toEqual({ cancel: false, retry: true, send: false });
    expect(fanqieJobActions(job({ status: "cancelled" }))).toEqual({ cancel: false, retry: true, send: false });
    expect(fanqieJobActions(job({ status: "completed" }))).toEqual({ cancel: false, retry: false, send: true });
  });
});

describe("coverThemeIndex", () => {
  it("同一 bookId 结果稳定且落在主题范围内", () => {
    for (const id of ["7123456789012345678", "abc", "12345", ""]) {
      const first = coverThemeIndex(id);
      expect(first).toBeGreaterThanOrEqual(0);
      expect(first).toBeLessThan(8);
      expect(coverThemeIndex(id)).toBe(first);
    }
  });
});

describe("FANQIE_JOB_STATUS_META", () => {
  it("覆盖后端全部任务状态", () => {
    for (const status of ["queued", "running", "completed", "failed", "cancelled"]) {
      expect(FANQIE_JOB_STATUS_META[status]).toBeDefined();
    }
  });
});
