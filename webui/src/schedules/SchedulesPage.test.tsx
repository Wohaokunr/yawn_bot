import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
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

import { SchedulesPage } from "./SchedulesPage";
import type { ScheduleSettings, ScheduleStatusData, ScheduleTask, ScheduleTaskDetail } from "./types";

const settings: ScheduleSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  whoCanCreate: "members",
  allowAgentMode: true,
  maxActivePerGroup: 20,
  maxActivePerMember: 5,
  minIntervalMinutes: 10,
  maxHorizonDays: 366,
  groupDailyRuns: 60,
  misfireGraceMinutes: 10,
  missedPolicy: "run_once",
  catchUpMaxHours: 6,
  maxConsecutiveFailures: 3,
  quietHoursEnabled: false,
  quietStart: "01:00",
  quietEnd: "07:00",
  quietPolicy: "defer",
  concurrency: 2,
  runTimeoutSeconds: 300,
  notifyPrefix: "⏰ ",
  historyLimit: 20,
};

const task: ScheduleTask = {
  id: 7,
  groupId: "123456",
  groupName: "摸鱼群",
  botId: "9",
  creatorUserId: "20001",
  creatorName: "爱丽丝",
  title: "喝水提醒",
  instruction: "提醒大家喝水",
  mode: "notify",
  kind: "daily",
  kindLabel: "每天",
  description: "每天 09:00、15:00",
  status: "active",
  statusReason: null,
  mentionCreator: true,
  maxRuns: null,
  runCount: 3,
  failureCount: 0,
  skipCount: 1,
  nextRunAt: "2026-10-26T09:00",
  lastRunAt: "2026-10-25T15:00",
  lastResult: "sent",
  lastResultLabel: "已发送提醒",
  lastError: null,
  lastOutput: "⏰ 提醒大家喝水",
  lastTraceId: null,
  sourceKind: "agent",
  revision: 4,
  createdAt: "2026-10-20T10:00",
  updatedAt: "2026-10-25T15:00",
};

const detail: ScheduleTaskDetail = {
  ...task,
  schedule: { kind: "daily", times: ["09:00", "15:00"] },
  upcoming: ["2026-10-26T09:00", "2026-10-26T15:00"],
  history: [{ at: "2026-10-25 15:00", planned: "2026-10-25 15:00", result: "sent", label: "已发送提醒", ms: 120, preview: "⏰ 提醒大家喝水" }],
  cancelledAt: null,
  cancelledBy: null,
};

function status(overrides: Partial<ScheduleStatusData> = {}): ScheduleStatusData {
  return {
    ready: true,
    enabled: true,
    now: "2026-10-26T08:20",
    counts: { active: 1, paused: 0, completed: 2, cancelled: 0, failed: 0 },
    today: { day: "2026-10-26", runs: 4, results: { sent: 3, failed: 1 } },
    upcoming: [task],
    groups: 1,
    ...overrides,
  };
}

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

function renderPage(search = ""): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[`/schedules${search}`]}>
        <LocationProbe />
        <Routes>
          <Route path="schedules" element={<SchedulesPage />} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

function mockApi(extra: (path: string, init?: RequestInit) => Promise<unknown> | undefined = () => undefined): void {
  state.api.mockImplementation((path: string, init?: RequestInit) => {
    const handled = extra(path, init);
    if (handled) return handled;
    if (path === "/schedules/status") return Promise.resolve({ data: status(), meta: {} });
    if (path === "/schedules/settings") return Promise.resolve({ data: { settings, defaults: settings }, meta: {} });
    if (path.startsWith("/schedules/tasks?")) return Promise.resolve({ data: { ready: true, items: [task], total: 1, page: 1, pageSize: 20 }, meta: {} });
    if (path === "/schedules/tasks/7") return Promise.resolve({ data: detail, meta: {} });
    if (path === "/schedules/preview") {
      return Promise.resolve({ data: { valid: true, description: "每天 09:00", kind: "daily", schedule: { kind: "daily", times: ["09:00"] }, upcoming: ["2026-10-26T09:00", "2026-10-27T09:00"] }, meta: {} });
    }
    return Promise.reject(new Error(`unexpected API: ${path}`));
  });
}

describe("SchedulesPage", () => {
  beforeEach(() => {
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
    state.api.mockReset();
  });

  it("状态条显示计数、下一个任务与今日失败，列表显示任务", async () => {
    mockApi();
    renderPage();
    expect(await screen.findByText("定时任务 · 运行中")).toBeTruthy();
    expect(screen.getByText(/下一个：10月26日 周一 09:00（40 分钟后）/)).toBeTruthy();
    expect(screen.getByText("今日失败 1")).toBeTruthy();
    expect(await screen.findByText("每天 09:00、15:00")).toBeTruthy();
    expect(state.api.mock.calls.some(([path]) => String(path).startsWith("/schedules/tasks?status=open"))).toBe(true);
  });

  it("未迁移时提示执行 nb orm upgrade", async () => {
    mockApi((path) => (path === "/schedules/status" ? Promise.resolve({ data: status({ ready: false, upcoming: [] }), meta: {} }) : undefined));
    renderPage();
    expect(await screen.findByText("定时任务 · 数据表未迁移")).toBeTruthy();
    expect(screen.getByText("定时任务的数据表还没有创建")).toBeTruthy();
  });

  it("点击任务打开详情抽屉，暂停走动作接口", async () => {
    const actions: string[] = [];
    mockApi((path, init) => {
      if (path === "/schedules/tasks/7/pause" && init?.method === "POST") {
        actions.push("pause");
        return Promise.resolve({ data: { changed: true, task: { ...detail, status: "paused" } }, meta: {} });
      }
      return undefined;
    });
    renderPage("?schedules.task=7");
    expect(await screen.findByText("#7 喝水提醒")).toBeTruthy();
    expect(screen.getByText("群聊里让 Agent 创建")).toBeTruthy();
    expect(screen.getAllByText("已发送提醒").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /^pause 暂\s*停$/ }));
    await waitFor(() => expect(actions).toEqual(["pause"]));
  });

  it("新建表单实时预览并提交规则与创建者", async () => {
    let created: Record<string, unknown> | null = null;
    mockApi((path, init) => {
      if (path === "/schedules/tasks" && init?.method === "POST") {
        created = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve({ data: { ...detail, id: 8 }, meta: {} });
      }
      return undefined;
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /新建任务/ }));
    fireEvent.change(await screen.findByLabelText("群号"), { target: { value: "123456" } });
    fireEvent.change(screen.getByLabelText("创建者 QQ"), { target: { value: "20001" } });
    fireEvent.change(screen.getByLabelText("指令"), { target: { value: "该喝水啦" } });
    expect(await screen.findByText("每天 09:00", {}, { timeout: 2000 })).toBeTruthy();
    expect(screen.getByText("10月27日 周二 09:00")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /创\s*建/ }));
    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({
      groupId: "123456",
      creatorUserId: "20001",
      instruction: "该喝水啦",
      mode: "notify",
      schedule: { kind: "daily", times: ["09:00"] },
      mentionCreator: true,
    });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("schedules.task=8"));
  });

  it("策略页校验后保存", async () => {
    let saved: ScheduleSettings | null = null;
    mockApi((path, init) => {
      if (path === "/schedules/settings" && init?.method === "PUT") {
        saved = JSON.parse(String(init.body)) as ScheduleSettings;
        return Promise.resolve({ data: { settings: saved }, meta: {} });
      }
      return undefined;
    });
    renderPage("?schedules.tab=policy");
    const member = await screen.findByLabelText("每位成员进行中的任务上限");
    fireEvent.change(member, { target: { value: "30" } });
    expect(await screen.findByText("成员上限不应超过群上限")).toBeTruthy();
    expect((screen.getByRole("button", { name: /保\s*存/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(member, { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("同时执行的群数"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => expect(saved?.concurrency).toBe(4));
  });
});
