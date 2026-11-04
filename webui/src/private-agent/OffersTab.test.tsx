import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
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

import { PrivateAgentPage } from "./PrivateAgentPage";

const settings = {
  enabled: true, default_on: true, interval_hours: 3, user_min_interval_hours: 1, jitter_minutes: 15, max_unanswered: 2, backoff_factor: 2,
  max_backoff_hours: 24, daily_max: 3, quiet_hours_enabled: true, quiet_start: "22:30", quiet_end: "09:00", active_within_days: 7, min_idle_minutes: 30,
  item_cooldown_hours: 24, decline_mute_days: 3, max_candidates: 4, max_items_per_offer: 2, min_score: 30, allow_model_skip: true, only_friends: true,
  per_tick: 2, answer_window_hours: 12, user_mode: "all", allowed_users: [], blocked_users: [], kinds: { todo_overdue: true, mood_care: false },
};
const kinds = [
  { kind: "todo_overdue", label: "逾期的待办", description: "过了截止日还没做完的待办" },
  { kind: "mood_care", label: "最近心情不好", description: "最近几次记的心情偏低" },
];
const stats = { sent: 4, skipped: 1, accepted: 2, declined: 1, later: 0, replied: 1, ignored: 0 };
const log = { id: "o3", at: "2026-11-03 15:00", status: "sent", status_label: "问了", by: "auto", items: [{ kind: "todo_overdue", key: "todo:1:overdue", what: "交实验报告" }], considered: ["todo_overdue"], text: "报告拖两天啦，要我帮你拆一下吗？", outcome: "accepted", outcome_label: "他答应了", replied_at: "2026-11-03 15:10" };

function respond(path: string, init?: RequestInit) {
  if (path === "/private-agent/offers") return { data: {
    env_on: true, agent_on: true, settings, kinds, reasons: { ok: "可以问" }, outcomes: {},
    usage: { day: "2026-11-03", results: { sent: 5, accepted: 2, declined: 1, failed: 1 }, kinds: {} },
    users: [{ user_id: 12345, prefs: { on: true, own_choice: false, every_hours: 3 }, last: log, last_check: { reason: "not_due", label: "还没到下一次的时间", unanswered: 0, interval_hours: 3, today: 1 }, stats, today: 1, pending: true }],
    total_users: 1,
  } };
  if (path === "/private-agent/offers/settings" && init?.method === "PUT") return { data: { settings: JSON.parse(String(init.body)) } };
  if (path === "/private-agent/offers/users/12345") return { data: {
    env_on: true, agent_on: true, user_id: 12345,
    gate: { reason: "ok", label: "可以问", unanswered: 1, interval_hours: 6, today: 1 },
    facts: { last_user_at: "2026-11-03 10:00", last_bot_at: "2026-11-03 10:01", friend: true, paused: false, busy: false },
    candidates: [{ kind: "todo_overdue", key: "todo:1:overdue", what: "交实验报告", could_do: "帮他拆成几小步", score: 84, details: {} }],
    dropped: [{ kind: "mood_care", key: "mood:x", what: "心情", could_do: "陪聊", score: 50, details: {}, dropped: "kind_off" }],
    payload: { things: [{ n: 1, what: "交实验报告" }] },
    prefs: { on: true, own_choice: false, every_hours: 3, muted: [{ kind: "mood_care", label: "最近心情不好", until: "一直" }] },
    own: { enabled: null, interval_hours: null, snooze_until: null, muted_kinds: { mood_care: "forever" } },
    log: [log], stats, cooling: [{ key: "todo:1:overdue", at: "2026-11-03 15:00" }],
  } };
  if (path === "/private-agent/offers/users/12345/prefs" && init?.method === "PUT") return { data: { prefs: { on: false, own_choice: true, every_hours: 3 } } };
  if (path === "/private-agent/offers/users/12345/run" && init?.method === "POST") return { data: { status: "sent", text: "要我帮你拆一下报告吗？", picks: [1] } };
  throw new Error(`unexpected ${path}`);
}

function renderAt(url: string) {
  return render(<MemoryRouter initialEntries={[url]}><AntApp><PrivateAgentPage /></AntApp></MemoryRouter>);
}

describe("OffersTab", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })));
    state.api.mockReset();
    state.api.mockImplementation((path: string, init?: RequestInit) => Promise.resolve(respond(path, init)));
  });

  it("shows today's numbers and each user's gate", async () => {
    renderAt("/private-agent?pa.tab=offers");
    expect(await screen.findByText("今天问了 5")).toBeTruthy();
    expect(screen.getByText("答应 2")).toBeTruthy();
    expect(screen.getByText("没发出去 1")).toBeTruthy();
    expect(screen.getByText("还没到下一次的时间")).toBeTruthy();
    expect(screen.getByText("等他回应")).toBeTruthy();
    expect(screen.getByText(/连着 2 次没回就停到他先开口/)).toBeTruthy();
  });

  it("edits and saves the site policy", async () => {
    renderAt("/private-agent?pa.tab=offers");
    fireEvent.click(await screen.findByText("策略"));
    const interval = await screen.findByLabelText("每隔几小时问一次");
    fireEvent.change(interval, { target: { value: "4" } });
    fireEvent.blur(interval);
    fireEvent.click(screen.getByRole("checkbox", { name: /最近心情不好/ }));
    fireEvent.click(screen.getByRole("button", { name: /保存策略/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith(
      "/private-agent/offers/settings",
      expect.objectContaining({ method: "PUT", body: expect.stringContaining("\"interval_hours\":4") }),
    ));
    const body = JSON.parse(String(state.api.mock.calls.find((call) => call[0] === "/private-agent/offers/settings")?.[1]?.body));
    expect(body.kinds.mood_care).toBe(true);
  });

  it("shows a user's candidates, dropped reasons and log, and changes his switch", async () => {
    renderAt("/private-agent?pa.tab=offers&pa.user=12345");
    expect(await screen.findByText("帮他拆成几小步", { exact: false })).toBeTruthy();
    expect(screen.getAllByText("交实验报告").length).toBeGreaterThan(0);
    expect(screen.getByText("“报告拖两天啦，要我帮你拆一下吗？”")).toBeTruthy();
    expect(screen.getByText("他答应了")).toBeTruthy();
    expect(screen.getByText(/不问「最近心情不好」/)).toBeTruthy();
    expect(screen.getByText("答应 2/4（50%）")).toBeTruthy();
    fireEvent.click(screen.getByText("关"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith(
      "/private-agent/offers/users/12345/prefs",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ enabled: false }) }),
    ));
  });

  it("runs an offer now after confirming", async () => {
    renderAt("/private-agent?pa.tab=offers&pa.user=12345");
    fireEvent.click(await screen.findByRole("button", { name: /现在试一次/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确\s?定|OK/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/private-agent/offers/users/12345/run", expect.objectContaining({ method: "POST" })));
  });
});
