import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig } from "../types";

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

const apiMock = vi.hoisted(() => vi.fn());

vi.mock("../api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status: number;
    fields: Record<string, string>;

    constructor(status: number, message: string, fields: Record<string, string> = {}) {
      super(message);
      this.status = status;
      this.fields = fields;
    }
  },
}));

import { ApiError } from "../api";
import { AgentConfigPanel } from "./AgentConfigPanel";

const baseConfig: AgentConfig = {
  groupId: "100",
  enabled: true,
  replyTriggerEnabled: true,
  explicitWakeupEnabled: true,
  proactiveEnabled: true,
  proactiveProbability: 0.35,
  proactiveActiveEnabled: true,
  shortConversationEnabled: true,
  proactiveActiveProbability: 0.25,
  proactiveActiveWindowMinutes: 10,
  idleThresholdMinutes: 30,
  cooldownMinutes: 15,
  dailyLimit: 5,
  rawRetentionDays: 30,
  crossGroupVisibility: "isolated",
  mediaCacheEnabled: true,
  adminToolDailyLimit: 10,
  criticalToolDailyLimit: 2,
  toolAllowlist: [],
  proactiveToday: 1,
  adminToolsToday: 0,
  criticalToolsToday: 0,
  version: "v1",
};

function renderPanel(): void {
  render(<AntApp><AgentConfigPanel groupId="100" /></AntApp>);
}

function dispatchConfigChange(groupId: string): void {
  window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
    detail: {
      resource: "agent_config",
      scope: { groupId },
      entityId: groupId,
    },
  }));
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    matches: false,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
});

afterAll(() => vi.unstubAllGlobals());

beforeEach(() => apiMock.mockReset());

describe("AgentConfigPanel regression safety", () => {
  it("does not refresh group A for an entity event from group B", async () => {
    apiMock.mockResolvedValue({ data: baseConfig, meta: {} });
    renderPanel();
    await screen.findByText("配置已同步");

    const getCalls = () => apiMock.mock.calls.filter(([path, init]) =>
      path === "/agent/groups/100/config" && !init?.method,
    ).length;
    expect(getCalls()).toBe(1);

    dispatchConfigChange("200");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(getCalls()).toBe(1);

    dispatchConfigChange("100");
    await waitFor(() => expect(getCalls()).toBe(2));
  });

  it("keeps a dirty form untouched when SSE brings a newer server version", async () => {
    let server = baseConfig;
    apiMock.mockImplementation(() => Promise.resolve({ data: server, meta: {} }));
    renderPanel();

    const dailyLimit = await screen.findByLabelText("每日参与上限");
    fireEvent.change(dailyLimit, { target: { value: "9" } });
    await screen.findByText("有未保存的修改");

    server = { ...baseConfig, dailyLimit: 2, version: "v2" };
    dispatchConfigChange("100");

    await screen.findByText("服务器配置已更新");
    expect((dailyLimit as HTMLInputElement).value).toBe("9");
    expect(screen.getByText("有未保存的修改")).toBeTruthy();
  });

  it("preserves the local draft after a 409 and reloads the conflict only as a remote update", async () => {
    let getCount = 0;
    apiMock.mockImplementation((path: string, init?: RequestInit) => {
      if (path !== "/agent/groups/100/config") throw new Error(`unexpected path ${path}`);
      if (init?.method === "PATCH") return Promise.reject(new ApiError(409, "配置已被其他管理员修改"));
      getCount += 1;
      return Promise.resolve({
        data: getCount === 1 ? baseConfig : { ...baseConfig, dailyLimit: 2, version: "v2" },
        meta: {},
      });
    });
    renderPanel();

    const dailyLimit = await screen.findByLabelText("每日参与上限");
    fireEvent.change(dailyLimit, { target: { value: "9" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

    await screen.findByText("服务器配置已更新");
    expect(getCount).toBe(2);
    expect((dailyLimit as HTMLInputElement).value).toBe("9");
    expect(screen.getByText("有未保存的修改")).toBeTruthy();
  });
});
