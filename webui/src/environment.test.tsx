import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EnvironmentEntry } from "./types";
import { EnvironmentPage, filterEnvironmentEntries, groupEnvironmentEntries } from "./environment";

function entry(overrides: Partial<EnvironmentEntry>): EnvironmentEntry {
  return {
    key: "AI_MODEL",
    section: "AI 配置",
    description: "默认模型",
    value: "model",
    defaultValue: "default",
    configured: true,
    effectiveConfigured: true,
    secret: false,
    kind: "string",
    options: [],
    source: "env",
    overridden: false,
    ...overrides,
  };
}

describe("environment helpers", () => {
  const entries = [
    entry({ key: "AI_MODEL" }),
    entry({ key: "AI_API_KEY", description: "API 密钥", secret: true }),
    entry({ key: "WEBUI_ENABLED", section: "管理界面", description: "启用管理页" }),
  ];

  it("按配置名、分组和说明搜索", () => {
    expect(filterEnvironmentEntries(entries, "api 密钥").map((item) => item.key)).toEqual(["AI_API_KEY"]);
    expect(filterEnvironmentEntries(entries, "管理界面").map((item) => item.key)).toEqual(["WEBUI_ENABLED"]);
    expect(filterEnvironmentEntries(entries, "  ")).toEqual(entries);
  });

  it("保持首次出现顺序并按 section 分组", () => {
    expect(groupEnvironmentEntries(entries)).toEqual([
      { section: "AI 配置", entries: entries.slice(0, 2) },
      { section: "管理界面", entries: entries.slice(2) },
    ]);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("页面隐藏敏感值并且只提交发生变化的配置项", async () => {
  vi.stubGlobal("ResizeObserver", class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  });
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
  const entries = [
    entry({ key: "AI_MODEL", value: "old-model" }),
    entry({ key: "AI_LIGHT_MODEL", value: "light-model" }),
    entry({ key: "AI_VISION_MODEL", value: "vision-model" }),
    entry({
      key: "AI_DEFAULT_THINKING",
      value: "auto",
      kind: "enum",
      options: ["auto", "enabled", "disabled"],
    }),
    entry({
      key: "AGENT_DIALOGUE_LLM_PROFILE",
      value: "default",
      kind: "enum",
      options: ["default", "light", "vision"],
    }),
    entry({
      key: "AGENT_DIALOGUE_THINKING",
      value: "inherit",
      kind: "enum",
      options: ["inherit", "auto", "enabled", "disabled"],
    }),
    entry({
      key: "AI_API_KEY",
      value: null,
      defaultValue: null,
      secret: true,
      effectiveConfigured: true,
    }),
  ];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PATCH") {
      return {
        ok: true,
        json: async () => ({
          data: { version: "b".repeat(64), restartRequired: true, updatedKeys: ["AI_MODEL"] },
          meta: {},
        }),
      };
    }
    return {
      ok: true,
      json: async () => ({
        data: {
          file: ".env",
          version: "a".repeat(64),
          environment: "prod",
          environmentFile: null,
          entries,
        },
        meta: {},
      }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 模型档位")).toBeInTheDocument();
  expect(screen.getByText("子插件任务路由")).toBeInTheDocument();
  expect(screen.getByText("普通对话 / 工具")).toBeInTheDocument();
  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "new-model" } });
  expect(screen.queryByDisplayValue("secret-value")).not.toBeInTheDocument();
  expect(screen.getByPlaceholderText("已配置，输入新值以替换")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: /保存 1 项/ }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    version: "a".repeat(64),
    changes: [{ key: "AI_MODEL", value: "new-model" }],
  });
});
