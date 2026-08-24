import { App as AntApp } from "antd";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function stubBrowserApis(): void {
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
}

function stubEnvironmentFetch(entries: EnvironmentEntry[]) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/llm/test")) {
      return {
        ok: true,
        json: async () => ({ data: { success: true, latencyMs: 12.5 }, meta: {} }),
      };
    }
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
          llmProviders: [{
            id: "default",
            baseUrl: "https://example.test/v1",
            builtIn: true,
            apiKeyConfigured: entries.some((item) => item.key === "AI_API_KEY" && item.effectiveConfigured),
            apiKeyRootConfigured: entries.some((item) => item.key === "AI_API_KEY" && item.configured),
            baseUrlSource: "env",
            apiKeySource: "env",
            overridden: false,
          }],
        },
        meta: {},
      }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
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
  cleanup();
  vi.unstubAllGlobals();
});

it("页面隐藏敏感值并且只提交发生变化的配置项", async () => {
  stubBrowserApis();
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
  const fetchMock = stubEnvironmentFetch(entries);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 模型档位")).toBeInTheDocument();
  expect(screen.getByText("子插件任务路由")).toBeInTheDocument();
  expect(screen.getByText("普通对话 / 工具")).toBeInTheDocument();
  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "new-model" } });
  expect(screen.queryByDisplayValue("secret-value")).not.toBeInTheDocument();
  // 敏感项位于默认收起的分组面板内,先全部展开再断言
  fireEvent.click(screen.getByRole("button", { name: "全部展开" }));
  expect(await screen.findByPlaceholderText("已配置，输入新值以替换")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: /保存 1 项/ }));
  expect(await screen.findByText("保存前差异预览")).toBeInTheDocument();
  expect(screen.getByText("old-model")).toBeInTheDocument();
  expect(screen.getByText("new-model")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    version: "a".repeat(64),
    changes: [{ key: "AI_MODEL", value: "new-model" }],
  });
});

it("折叠配置块，未保存角标在折叠时仍然可见", async () => {
  stubBrowserApis();
  stubEnvironmentFetch([entry({ key: "AI_MODEL", value: "old-model" })]);

  render(<AntApp><EnvironmentPage /></AntApp>);
  const header = await screen.findByRole("button", { name: /LLM 模型档位/ });
  expect(header).toHaveAttribute("aria-expanded", "true");

  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "new-model" } });
  expect(screen.getByText("1 项未保存")).toBeInTheDocument();

  fireEvent.click(header);
  expect(header).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("1 项未保存")).toBeInTheDocument();

  fireEvent.click(header);
  expect(header).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByDisplayValue("new-model")).toBeInTheDocument();
});

it("搜索命中的分组自动展开", async () => {
  stubBrowserApis();
  stubEnvironmentFetch([
    entry({ key: "AI_MODEL", value: "model" }),
    entry({
      key: "WEBUI_ENABLED",
      section: "管理界面",
      description: "启用管理页",
      kind: "boolean",
      value: "true",
    }),
  ]);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 模型档位")).toBeInTheDocument();
  expect(screen.queryByText("启用管理页")).not.toBeInTheDocument();

  fireEvent.change(screen.getByPlaceholderText("搜索配置名、分组或说明"), {
    target: { value: "管理界面" },
  });

  expect(await screen.findByText("启用管理页")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /管理界面/ })).toHaveAttribute("aria-expanded", "true");
});

it("浮动保存条支持撤销全部与保存全部修改", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([entry({ key: "AI_MODEL", value: "old-model" })]);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 模型档位")).toBeInTheDocument();

  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "new-model" } });
  const bar = screen.getByText(/未保存修改 1 项/).closest(".env-save-bar");
  expect(bar).not.toBeNull();

  fireEvent.click(within(bar as HTMLElement).getByRole("button", { name: /撤销全部/ }));
  expect(screen.queryByText("未保存修改 1 项")).not.toBeInTheDocument();
  expect(screen.getByDisplayValue("old-model")).toBeInTheDocument();

  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "newer-model" } });
  fireEvent.click(screen.getByRole("button", { name: /预览并保存/ }));
  expect(await screen.findByText("保存前差异预览")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
    expect(patchCall).toBeDefined();
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      version: "a".repeat(64),
      changes: [{ key: "AI_MODEL", value: "newer-model" }],
    });
  });
});

it("新增命名提供商时统一保存脱敏密钥配置", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([
    entry({ key: "AI_MODEL", value: "default-model" }),
    entry({ key: "AI_DEFAULT_PROVIDER", value: "default" }),
    entry({
      key: "AI_API_KEY",
      value: null,
      defaultValue: null,
      secret: true,
      effectiveConfigured: true,
    }),
  ]);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 提供商")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /添加提供商/ }));
  fireEvent.change(screen.getByDisplayValue("provider1"), { target: { value: "fast" } });
  fireEvent.change(screen.getByDisplayValue("https://example.com/v1"), {
    target: { value: "https://fast.test/v1" },
  });
  fireEvent.change(screen.getByPlaceholderText("输入 API Key"), {
    target: { value: "draft-secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: /保存 1 项/ }));
  expect(await screen.findByText("保存前差异预览")).toBeInTheDocument();
  expect(screen.queryByText("draft-secret")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      version: "a".repeat(64),
      changes: [],
      providers: [
        { id: "default", baseUrl: "https://example.test/v1" },
        { id: "fast", baseUrl: "https://fast.test/v1", apiKey: "draft-secret" },
      ],
    });
  });
});

it("模型档位连接测试使用当前草稿路由且不回填已有密钥", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([
    entry({ key: "AI_MODEL", value: "default-model" }),
    entry({ key: "AI_DEFAULT_PROVIDER", value: "default" }),
  ]);

  render(<AntApp><EnvironmentPage /></AntApp>);
  expect(await screen.findByText("LLM 模型档位")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: /测试此档位/ })[0]);

  await waitFor(() => {
    const testCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/llm/test"));
    expect(JSON.parse(String(testCall?.[1]?.body))).toEqual({
      providerId: "default",
      baseUrl: "https://example.test/v1",
      model: "default-model",
    });
  });
  expect(await screen.findByText(/最近测试成功/)).toBeInTheDocument();
  expect(screen.getAllByText(/default \/ default-model/).length).toBeGreaterThan(0);
});
