import { App as AntApp } from "antd";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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

function renderPage(path = "/environment?env.tab=profiles") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AntApp><EnvironmentPage /></AntApp>
    </MemoryRouter>,
  );
}

function stubEnvironmentFetch(entries: EnvironmentEntry[], extra: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/llm/test")) {
      return {
        ok: true,
        json: async () => ({ data: { success: true, latencyMs: 12.5 }, meta: {} }),
      };
    }
    if (url.endsWith("/system/restart")) {
      return { ok: true, json: async () => ({ data: { scheduled: true, startedAt: 1 }, meta: {} }) };
    }
    if (url.endsWith("/system/status")) {
      return { ok: true, json: async () => ({ data: { restartSupported: true, restartScheduled: false, pendingRestart: false, startedAt: 2, uptimeSeconds: 1 }, meta: {} }) };
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
          ...extra,
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
    entry({ key: "AI_DEFAULT_THINKING", value: "auto", kind: "enum", options: ["auto", "enabled", "disabled"] }),
    entry({ key: "AI_API_KEY", value: null, defaultValue: null, secret: true, effectiveConfigured: true }),
    entry({ key: "ONEBOT_V11_ACCESS_TOKEN", section: "OneBot V11 连接", value: null, defaultValue: null, secret: true, effectiveConfigured: true }),
  ];
  const fetchMock = stubEnvironmentFetch(entries);

  renderPage();
  fireEvent.change(await screen.findByDisplayValue("old-model"), { target: { value: "new-model" } });
  expect(screen.queryByDisplayValue("secret-value")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /保存 1 项/ }));
  expect(await screen.findByText("保存 1 项修改")).toBeInTheDocument();
  expect(screen.getByText("old-model")).toBeInTheDocument();
  expect(screen.getByText("new-model")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      version: "a".repeat(64),
      changes: [{ key: "AI_MODEL", value: "new-model" }],
    });
  });
  expect(await screen.findByText("有已保存的修改尚未生效")).toBeInTheDocument();
});

it("按分类浏览，搜索跨分类并写进 URL", async () => {
  stubBrowserApis();
  stubEnvironmentFetch([
    entry({ key: "AI_MODEL", value: "model", section: "OpenAI-compatible 服务" }),
    entry({ key: "WEBUI_ENABLED", section: "Core / Agent 管理 WebUI", description: "是否启用 /webui 管理端", kind: "boolean", value: "true", defaultValue: "false" }),
    entry({ key: "RPG_MAX_PLAYERS", section: "跑团 RPG", description: "房间人数上限", kind: "integer", value: null, defaultValue: "6", configured: false }),
  ]);

  renderPage("/environment");
  const nav = await screen.findByRole("navigation", { name: "配置分类" });
  expect(screen.queryByText("房间人数上限")).not.toBeInTheDocument();
  fireEvent.click(within(nav).getByRole("button", { name: /游戏/ }));
  expect(await screen.findByText("房间人数上限")).toBeInTheDocument();
  expect(screen.queryByText("是否启用 /webui 管理端")).not.toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("搜索配置"), { target: { value: "webui" } });
  expect(await screen.findByText("找到 1 项")).toBeInTheDocument();
  expect(screen.getByText("是否启用 /webui 管理端")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "清除搜索和筛选" }));
  expect(await screen.findByText("房间人数上限")).toBeInTheDocument();
});

it("非法值即时报错并禁止保存", async () => {
  stubBrowserApis();
  stubEnvironmentFetch([
    entry({
      key: "SQLALCHEMY_ENGINE_OPTIONS",
      section: "本地数据与 SQLite/ORM",
      description: "SQLAlchemy 引擎选项（JSON 对象）",
      kind: "json",
      value: null,
      configured: false,
      defaultValue: '{"connect_args":{"timeout":30}}',
      meta: { label: "SQLAlchemy 引擎选项", min: null, max: null, format: null, suggestions: [], deprecated: false },
    }),
  ]);

  renderPage("/environment?env.cat=runtime");
  const input = await screen.findByLabelText("SQLAlchemy 引擎选项");
  fireEvent.change(input, { target: { value: "[1]" } });
  expect(await screen.findByText("必须是 JSON 对象")).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "{bad" } });
  expect(await screen.findByText("JSON 格式错误")).toBeInTheDocument();
  expect(screen.getByText(/1 项有误/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /预览并保存/ })).toBeDisabled();
});

it("体检结果可以定位到对应字段", async () => {
  stubBrowserApis();
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  stubEnvironmentFetch(
    [
      entry({ key: "WEBUI_ADMIN_TOKEN", section: "Core / Agent 管理 WebUI", description: "运维管理员登录令牌", value: null, defaultValue: null, secret: true, effectiveConfigured: true }),
      entry({ key: "RPG_MAX_PLAYERS", section: "跑团 RPG", description: "房间人数上限", kind: "integer" }),
    ],
    {
      diagnostics: [{ level: "warning", title: "运维 Token 偏弱", detail: "长度少于建议的 16 个字符", keys: ["WEBUI_ADMIN_TOKEN"] }],
    },
  );

  renderPage("/environment?env.cat=games");
  expect(await screen.findByText("运维 Token 偏弱")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /定\s?位/ }));
  expect(await screen.findByText("运维管理员登录令牌")).toBeInTheDocument();
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
});

it("生成随机 Token 并在保存预览中提示需要重新登录", async () => {
  stubBrowserApis();
  stubEnvironmentFetch([
    entry({ key: "WEBUI_ADMIN_TOKEN", section: "Core / Agent 管理 WebUI", description: "运维管理员登录令牌", value: null, defaultValue: null, secret: true, effectiveConfigured: true }),
  ]);

  renderPage("/environment?env.cat=webui");
  fireEvent.click(await screen.findByRole("button", { name: /生成/ }));
  const input = screen.getByLabelText("新的运维 Token") as HTMLInputElement;
  expect(input.value).toMatch(/^[A-Za-z0-9_-]{32}$/);
  expect(screen.getByText(/强度：强/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /预览并保存/ }));
  expect(await screen.findByText(/需要用新 Token 登录/)).toBeInTheDocument();
  expect(screen.queryByText(input.value)).not.toBeInTheDocument();
});

it("浮动保存条支持撤销全部与保存全部修改", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([entry({ key: "AI_MODEL", value: "old-model" })]);

  renderPage();
  fireEvent.change(await screen.findByDisplayValue("old-model"), { target: { value: "new-model" } });
  const bar = screen.getByText(/未保存修改 1 项/).closest(".env-save-bar");
  expect(bar).not.toBeNull();

  fireEvent.click(within(bar as HTMLElement).getByRole("button", { name: /撤销全部/ }));
  fireEvent.click(await screen.findByRole("button", { name: /确\s?定|OK/ }));
  await waitFor(() => expect(screen.queryByText(/未保存修改 1 项/)).not.toBeInTheDocument());
  expect(screen.getByDisplayValue("old-model")).toBeInTheDocument();

  fireEvent.change(screen.getByDisplayValue("old-model"), { target: { value: "newer-model" } });
  fireEvent.keyDown(window, { key: "s", ctrlKey: true });
  expect(await screen.findByText("保存 1 项修改")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      version: "a".repeat(64),
      changes: [{ key: "AI_MODEL", value: "newer-model" }],
    });
  });
});

it("保存后可以从页面重启并等待恢复", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([entry({ key: "AI_MODEL", value: "m" })], { pendingRestart: true, restartSupported: true });

  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: "重启 YawnBot" }));
  fireEvent.click(await screen.findByRole("button", { name: "立即重启" }));
  await waitFor(() => expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/system/restart"))).toBe(true));
  expect(await screen.findByText("正在重启 YawnBot…")).toBeInTheDocument();
});

it("新增命名提供商时统一保存脱敏密钥配置", async () => {
  stubBrowserApis();
  const fetchMock = stubEnvironmentFetch([
    entry({ key: "AI_MODEL", value: "default-model" }),
    entry({ key: "AI_DEFAULT_PROVIDER", value: "default" }),
    entry({ key: "AI_API_KEY", value: null, defaultValue: null, secret: true, effectiveConfigured: true }),
  ]);

  renderPage("/environment");
  fireEvent.click(await screen.findByRole("button", { name: /添加提供商/ }));
  fireEvent.change(screen.getByDisplayValue("provider1"), { target: { value: "fast" } });
  fireEvent.change(screen.getByDisplayValue("https://example.com/v1"), { target: { value: "https://fast.test/v1" } });
  fireEvent.change(screen.getByPlaceholderText("输入 API Key"), { target: { value: "draft-secret" } });
  fireEvent.click(screen.getByRole("button", { name: /保存 1 项/ }));
  expect(await screen.findByText("保存 1 项修改")).toBeInTheDocument();
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

  renderPage();
  fireEvent.click((await screen.findAllByRole("button", { name: /测\s?试/ }))[0]);

  await waitFor(() => {
    const testCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/llm/test"));
    expect(JSON.parse(String(testCall?.[1]?.body))).toEqual({
      providerId: "default",
      baseUrl: "https://example.test/v1",
      model: "default-model",
    });
  });
  expect(await screen.findByText(/测试成功/)).toBeInTheDocument();
  expect(screen.getAllByText(/default \/ default-model/).length).toBeGreaterThan(0);
});
