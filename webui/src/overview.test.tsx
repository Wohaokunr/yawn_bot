import { describe, expect, it } from "vitest";
import { aiOutcomeMeta, fanqieSummary, formatLatency, formatRate, formatUptime } from "./App";

describe("formatUptime", () => {
  it("按秒/分/时/天分级展示", () => {
    expect(formatUptime(0)).toBe("0 秒");
    expect(formatUptime(59)).toBe("59 秒");
    expect(formatUptime(60)).toBe("1 分 0 秒");
    expect(formatUptime(3725)).toBe("1 小时 2 分");
    expect(formatUptime(90061)).toBe("1 天 1 小时");
  });

  it("负数按 0 处理，不出现负时长", () => {
    expect(formatUptime(-5)).toBe("0 秒");
  });
});

describe("formatRate", () => {
  it("无数据时显示占位符", () => {
    expect(formatRate(null)).toBe("—");
  });

  it("百分比保留一位小数", () => {
    expect(formatRate(1)).toBe("100.0%");
    expect(formatRate(2 / 3)).toBe("66.7%");
    expect(formatRate(0)).toBe("0.0%");
  });
});

describe("formatLatency", () => {
  it("无数据时显示占位符", () => {
    expect(formatLatency(null)).toBe("—");
  });

  it("毫秒与秒分级展示", () => {
    expect(formatLatency(123.4)).toBe("123 ms");
    expect(formatLatency(999)).toBe("999 ms");
    expect(formatLatency(1500)).toBe("1.5 s");
    expect(formatLatency(20333)).toBe("20.3 s");
  });
});

describe("aiOutcomeMeta", () => {
  it("覆盖后端已知的全部 AI outcome 取值", () => {
    for (const outcome of [
      "success",
      "error",
      "timeout",
      "empty",
      "not_configured",
      "cancelled",
      "unsupported_multimodal",
    ]) {
      const meta = aiOutcomeMeta(outcome);
      expect(meta.label.length).toBeGreaterThan(0);
      expect(meta.color.length).toBeGreaterThan(0);
    }
  });

  it("未知取值回退为原值展示", () => {
    expect(aiOutcomeMeta("weird_future").label).toBe("weird_future");
  });
});

describe("fanqieSummary", () => {
  it("已知状态按固定顺序摘要，忽略未知状态", () => {
    expect(fanqieSummary({ queued: 2, running: 1, failed: 0, completed: 12 })).toBe(
      "排队 2 · 进行 1 · 失败 0 · 完成 12",
    );
    expect(fanqieSummary({ weird: 3, queued: 1 })).toBe("排队 1");
  });

  it("空任务显示占位符", () => {
    expect(fanqieSummary({})).toBe("暂无任务");
  });
});
