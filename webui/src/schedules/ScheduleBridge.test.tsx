import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  getCsrfToken: () => "csrf",
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { MySchedulesPage } from "../me/MySchedulesPage";
import { selectedNavKey, USER_NAV } from "../nav-config";
import { PrivateTab } from "./PrivateTab";
import { parseRef, parseScheduleTab, privateSourceMeta, privateStatusMeta, SETTING_RANGES, sourceLabel } from "./schedule-meta";
import type { PrivateReminder } from "./types";

const reminder: PrivateReminder = {
  key: "42:3",
  ref: "p3",
  channel: "private",
  userId: "42",
  itemId: 3,
  title: "",
  content: "背单词",
  mode: "notify",
  kindLabel: "每天",
  description: "每天 21:00",
  status: "active",
  nextRunAt: "2026-11-03 21:00",
  runCount: 2,
  maxRuns: null,
  lastResult: "sent",
  lastResultLabel: "已提醒",
  lastRunAt: "2026-11-02 21:00",
  retryAt: null,
  sourceKind: "group",
  sourceLabel: "群「摸鱼群」里设的",
  originGroupId: "123456",
  createdAt: "2026-11-01 10:00",
};

const groupTask = {
  id: 7,
  ref: "g7",
  groupId: "123456",
  groupName: "摸鱼群",
  botId: "9",
  creatorUserId: "42",
  creatorName: "小明",
  title: "",
  instruction: "提醒大家交周报",
  mode: "notify",
  kind: "weekly",
  kindLabel: "每周",
  description: "每周五 17:00",
  status: "active",
  statusReason: null,
  mentionCreator: true,
  maxRuns: null,
  runCount: 0,
  failureCount: 0,
  skipCount: 0,
  nextRunAt: "2026-11-06T17:00",
  lastRunAt: null,
  lastResult: null,
  lastResultLabel: null,
  lastError: null,
  lastOutput: null,
  lastTraceId: null,
  sourceKind: "private",
  revision: 1,
  createdAt: "2026-11-03T10:00",
  updatedAt: "2026-11-03T10:00",
  mine: true,
};

beforeEach(() => {
  state.api.mockReset();
  vi.stubGlobal("ResizeObserver", class { observe(): void {} unobserve(): void {} disconnect(): void {} });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
});

describe("schedule bridge meta", () => {
  it("parses unified refs and labels new sources", () => {
    expect(parseRef("g12")).toEqual({ channel: "group", id: 12 });
    expect(parseRef(" P3 ")).toEqual({ channel: "private", id: 3 });
    expect(parseRef("x1")).toBeNull();
    expect(parseScheduleTab("private")).toBe("private");
    expect(sourceLabel("private")).toContain("私聊");
    expect(sourceLabel("webui_user")).toContain("网页");
    expect(privateStatusMeta("retrying").label).toBe("等待重试");
    expect(privateSourceMeta("group").label).toBe("群里设的");
    expect(privateStatusMeta("weird").label).toBe("weird");
    expect(SETTING_RANGES.privateRetryLimit.min).toBe(0);
  });

  it("adds the personal schedules page to the user nav", () => {
    expect(USER_NAV.some((entry) => entry.key === "/me/schedules")).toBe(true);
    expect(selectedNavKey("/me/schedules", "user")).toBe("/me/schedules");
  });
});

describe("admin private reminders tab", () => {
  it("lists reminders with source and pauses one", async () => {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path.startsWith("/schedules/private?")) {
        return Promise.resolve({ data: { items: [reminder], total: 1, counts: { active: 1, paused: 0, agent: 0, fromGroups: 1, users: 1 } }, meta: {} });
      }
      if (path === "/schedules/private/42/3/pause" && init?.method === "POST") return Promise.resolve({ data: { changed: true, item: reminder }, meta: {} });
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/schedules?schedules.tab=private"]}>
          <Routes><Route path="schedules" element={<PrivateTab now="2026-11-03 20:00" />} /></Routes>
        </MemoryRouter>
      </AntApp>,
    );
    expect(await screen.findByText("背单词")).toBeTruthy();
    expect(screen.getAllByText("群里设的").length).toBeGreaterThan(0);
    expect(screen.getByText("群里设的 1")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("暂停私聊提醒 42:3"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/schedules/private/42/3/pause", { method: "POST" }));
  });
});

describe("my schedules page", () => {
  function mock(extra: (path: string, init?: RequestInit) => Promise<unknown> | undefined = () => undefined): void {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      const handled = extra(path, init);
      if (handled) return handled;
      if (path.startsWith("/me/schedules?")) {
        return Promise.resolve({
          data: {
            items: [reminder, groupTask],
            counts: { private: 1, group: 1 },
            groupReady: true,
            groups: [{ groupId: "123456", groupName: "摸鱼群", role: "member", scheduleOpen: true, canCreate: true }],
            policy: { crossCreate: true, privateAgentMode: true, allowAgentMode: true, privateMinInterval: 5, groupMinInterval: 10 },
          },
          meta: {},
        });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
  }

  function renderMe(): void {
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/me/schedules"]}>
          <Routes><Route path="me/schedules" element={<MySchedulesPage />} /></Routes>
        </MemoryRouter>
      </AntApp>,
    );
  }

  it("shows private reminders and group tasks together", async () => {
    mock();
    renderMe();
    expect(await screen.findByText("背单词")).toBeTruthy();
    expect(screen.getByText("提醒大家交周报")).toBeTruthy();
    expect(screen.getByText("私聊提醒我")).toBeTruthy();
    expect(screen.getByText("群「摸鱼群」")).toBeTruthy();
    expect(screen.getByText(/私聊提醒 1 条 · 群定时任务 1 个/)).toBeTruthy();
  });

  it("cancels a group task through its unified ref", async () => {
    mock((path, init) => (path === "/me/schedules/g7/cancel" && init?.method === "POST" ? Promise.resolve({ data: { changed: true }, meta: {} }) : undefined));
    renderMe();
    fireEvent.click(await screen.findByLabelText("取消 g7"));
    fireEvent.click(await screen.findByRole("button", { name: /确\s?定/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/schedules/g7/cancel", { method: "POST" }));
  });

  it("creates a private reminder from the form", async () => {
    mock((path, init) => {
      if (path === "/me/schedules" && init?.method === "POST") return Promise.resolve({ data: { created: true, item: reminder }, meta: {} });
      return undefined;
    });
    renderMe();
    await screen.findByText("背单词");
    fireEvent.click(screen.getByRole("button", { name: /新建/ }));
    fireEvent.change(await screen.findByLabelText("到点做什么"), { target: { value: "喝水" } });
    fireEvent.click(screen.getByRole("button", { name: /设\s?好/ }));
    await waitFor(() => {
      const call = state.api.mock.calls.find(([path, init]) => path === "/me/schedules" && (init as RequestInit | undefined)?.method === "POST");
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call![1] as RequestInit).body));
      expect(body.target).toBe("private");
      expect(body.content).toBe("喝水");
      expect(body.schedule.kind).toBe("daily");
    });
  });
});
