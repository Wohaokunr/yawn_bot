import { describe, expect, it } from "vitest";
import {
  categoryOf,
  compactJson,
  dangerousChanges,
  diagnosticKeySet,
  generateToken,
  humanizeBytes,
  humanizeSeconds,
  humanizeValue,
  matchesFilter,
  parseCsvList,
  parseJsonStringList,
  searchEntries,
  serializeCsvList,
  serializeJsonStringList,
  tokenStrength,
  tokenWarnings,
  validateDraft,
} from "./environment-fields";
import { describeRest } from "./environment-editors";
import type { EnvironmentEntry } from "./types";

function entry(overrides: Partial<EnvironmentEntry>): EnvironmentEntry {
  return {
    key: "X",
    section: "其他",
    description: "",
    value: null,
    defaultValue: null,
    configured: false,
    effectiveConfigured: false,
    secret: false,
    kind: "string",
    options: [],
    source: "default",
    overridden: false,
    ...overrides,
  };
}

const meta = (patch: Partial<NonNullable<EnvironmentEntry["meta"]>>) => ({
  label: "",
  min: null,
  max: null,
  format: null,
  suggestions: [],
  deprecated: false,
  ...patch,
});

describe("分类", () => {
  it("把目录 section 归到大类，未知的落到其他", () => {
    expect(categoryOf("跑团 RPG")).toBe("games");
    expect(categoryOf("游戏 AI 开关")).toBe("games");
    expect(categoryOf("Core / Agent 管理 WebUI")).toBe("webui");
    expect(categoryOf("自定义配置")).toBe("other");
  });
});

describe("校验与后端一致", () => {
  it("整数、小数与范围", () => {
    const port = entry({ kind: "integer", meta: meta({ min: 1, max: 65535 }) });
    expect(validateDraft(port, "80x")).toBe("必须是整数");
    expect(validateDraft(port, "0")).toBe("不能小于 1");
    expect(validateDraft(port, "70000")).toBe("不能大于 65535");
    expect(validateDraft(port, "")).toMatch(/不能为空/);
    expect(validateDraft(port, "8080")).toBeNull();
    const rate = entry({ kind: "number", meta: meta({ min: 0, max: 1 }) });
    expect(validateDraft(rate, "0.5")).toBeNull();
    expect(validateDraft(rate, "abc")).toBe("必须是数字");
  });

  it("布尔、枚举、JSON 与 Token", () => {
    expect(validateDraft(entry({ kind: "boolean" }), "yes")).toBe("只能是 true 或 false");
    expect(validateDraft(entry({ kind: "enum", options: ["a", "b"] }), "c")).toBe("仅支持 a, b");
    const list = entry({ kind: "json", defaultValue: "[]", meta: meta({ format: "json-string-list" }) });
    expect(validateDraft(list, "{}")).toBe("必须是 JSON 数组");
    expect(validateDraft(list, "[1]")).toMatch(/字符串/);
    expect(validateDraft(list, '["1"]')).toBeNull();
    const token = entry({ key: "WEBUI_ADMIN_TOKEN", secret: true });
    expect(validateDraft(token, " x ")).toBe("Token 首尾不能有空格");
    expect(validateDraft(token, "")).toBe("Token 不能为空");
    expect(validateDraft(token, "Whk@12345")).toBeNull();
    expect(validateDraft(token, null)).toBeNull();
  });
});

describe("列表与 JSON", () => {
  it("逗号列表与 JSON 字符串数组往返", () => {
    expect(parseCsvList(" a.test, ,b.test ")).toEqual(["a.test", "b.test"]);
    expect(serializeCsvList(["a", " b "])).toBe("a,b");
    expect(parseJsonStringList('["1", 2]')).toEqual(["1", "2"]);
    expect(parseJsonStringList("{}")).toBeNull();
    expect(serializeJsonStringList(["1", ""])).toBe('["1"]');
    expect(compactJson('{\n  "a": 1\n}')).toBe('{"a":1}');
    expect(compactJson("{bad")).toBeNull();
  });
});

describe("人类可读数值", () => {
  it("时长与大小", () => {
    expect(humanizeSeconds(86400)).toBe("1 天");
    expect(humanizeSeconds(90)).toBe("1.5 分钟");
    expect(humanizeBytes(67108864)).toBe("64 MiB");
    const ttl = entry({ kind: "integer", meta: meta({ format: "seconds" }) });
    expect(humanizeValue(ttl, "30")).toBeNull();
    expect(humanizeValue(ttl, "7200")).toBe("2 小时");
  });
});

describe("运维 Token", () => {
  it("提示偏弱但不回显，生成的 Token 足够强", () => {
    expect(tokenWarnings("Whk@12345")).toEqual(["长度少于建议的 16 个字符", "包含常见弱口令片段"]);
    expect(tokenStrength("Whk@12345").label).toBe("偏弱");
    const generated = generateToken();
    expect(generated).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(generateToken()).not.toBe(generated);
    expect(tokenStrength(generated).label).toBe("强");
  });

  it("危险变更说明", () => {
    expect(dangerousChanges({ WEBUI_ADMIN_TOKEN: "x" })[0]).toMatch(/新 Token/);
    expect(dangerousChanges({ WEBUI_ENABLED: "false" })[0]).toMatch(/关闭/);
    expect(dangerousChanges({ AI_MODEL: "m" })).toEqual([]);
  });
});

describe("搜索与筛选", () => {
  const rows = [
    entry({ key: "PORT", description: "FastAPI 监听端口", configured: true, meta: meta({ label: "监听端口" }) }),
    entry({ key: "SECRET_KEY", secret: true, issue: "必须是整数" }),
    entry({ key: "HOST", source: "process", overridden: true }),
    entry({ key: "PINNED", source: "pinned", overridden: true }),
  ];
  const context = { changes: { PORT: "1" }, errors: {}, diagnosticKeys: new Set<string>() };

  it("按中文名和键名搜索", () => {
    expect(searchEntries(rows, "监听").map((item) => item.key)).toEqual(["PORT"]);
    expect(searchEntries(rows, "host").map((item) => item.key)).toEqual(["HOST"]);
  });

  it("筛选", () => {
    const keys = (filter: Parameters<typeof matchesFilter>[1]) => rows.filter((item) => matchesFilter(item, filter, context)).map((item) => item.key);
    expect(keys("customized")).toEqual(["PORT"]);
    expect(keys("dirty")).toEqual(["PORT"]);
    expect(keys("issues")).toEqual(["SECRET_KEY"]);
    expect(keys("overridden")).toEqual(["HOST"]);
    expect(keys("secret")).toEqual(["SECRET_KEY"]);
  });

  it("汇总类提示不把每个键都算成有问题", () => {
    const set = diagnosticKeySet([
      { level: "info", title: "3 项被外部环境覆盖", detail: "", keys: ["A", "B", "C"] },
      { level: "warning", title: "Token", detail: "", keys: ["WEBUI_ADMIN_TOKEN"] },
    ]);
    expect([...set]).toEqual(["WEBUI_ADMIN_TOKEN"]);
  });
});

describe("说明去重", () => {
  it("中文名之后只显示剩余说明", () => {
    expect(describeRest("FastAPI 监听端口", "FastAPI 监听端口")).toBeNull();
    expect(describeRest("登录会话有效时长（小时，1-168）", "登录会话有效时长")).toBe("小时，1-168");
    expect(describeRest("媒体缓存目录，应位于持久化数据盘", "媒体缓存目录")).toBe("应位于持久化数据盘");
    expect(describeRest("另一段说明", "PORT")).toBe("另一段说明");
    expect(describeRest("建卡窗口（秒），超时未确认的角色卡自动确认", "建卡窗口")).toBe("秒 · 超时未确认的角色卡自动确认");
  });
});
