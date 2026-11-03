import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  setCsrfToken: vi.fn(),
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

vi.mock("../agent-panels/RelationsPanel", () => ({
  RelationsPanel: ({ groupId, readOnly }: { groupId: string; readOnly?: boolean }) => <div>relations {groupId} {readOnly ? "readonly" : "editable"}</div>,
}));

vi.mock("../agent-panels/AgentConfigPanel", () => ({
  AgentConfigPanel: ({ groupId, manager }: { groupId: string; manager?: boolean }) => <div>agent-config {groupId} {manager ? "manager" : "admin"}</div>,
}));

vi.mock("../agent-panels/PersonaPanel", () => ({
  PersonaPanel: ({ groupId, manager }: { groupId: string; manager?: boolean }) => <div>persona {groupId} {manager ? "manager" : "admin"}</div>,
}));

import { MeHomePage } from "./MeHomePage";
import { MyGroupPage } from "./MyGroupPage";

const POLICY = {
  enabled: true, privateEnabled: true, webuiEnabled: true, notifyGroup: false, allowRelay: true, maxMuteMinutes: 1440, maxMuteCeiling: 43200,
  disabledActions: [], adminActions: ["mute", "unmute"], confirmActions: ["mute"], updatedAt: null, updatedBy: null,
};
const MANAGE = {
  available: true, problem: null, yourRole: "admin", botRole: "admin", isOwner: false,
  allowedActions: ["mute", "unmute"],
  actions: [
    { key: "mute", label: "禁言", risk: "mid", target: true, botOwner: false, confirm: true, adminAllowed: true, disabled: false, blockedReason: null },
    { key: "unmute", label: "解除禁言", risk: "low", target: true, botOwner: false, confirm: false, adminAllowed: true, disabled: false, blockedReason: null },
    { key: "kick", label: "移出群聊", risk: "high", target: true, botOwner: false, confirm: true, adminAllowed: false, disabled: false, blockedReason: "这个群只允许群主用「移出群聊」" },
  ],
  policy: POLICY, usedToday: 2, dailyLimit: 60, sent: [],
};
const MEMBERS = [
  { userId: "10002", name: "小红", nickname: "u10002", card: "小红", role: "member", title: null, mutedUntil: null },
  { userId: "10003", name: "小刚", nickname: "u10003", card: "小刚", role: "member", title: null, mutedUntil: "11-03 12:00" },
];

const FEATURES = [
  { key: "checkin", name: "签到", override: null, effective: true, source: "default" },
  { key: "group_agent", name: "群聊 Agent", override: null, effective: true, source: "default" },
];

function renderAt(path: string): void {
  render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="me" element={<MeHomePage />} />
          <Route path="me/groups/:groupId" element={<MyGroupPage />} />
        </Routes>
      </MemoryRouter>
    </AntApp>,
  );
}

describe("我的空间", () => {
  beforeEach(() => {
    vi.stubGlobal("ResizeObserver", class { observe(): void {} unobserve(): void {} disconnect(): void {} });
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    state.api.mockReset();
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/me") return Promise.resolve({ data: { userId: "10001", nickname: "小明", account: { userId: "10001", hasPassword: true, disabled: false, createdAt: "2026-11-01T10:00:00", passwordChangedAt: "2026-11-01T10:00:00", lastLoginAt: null, setupPending: false, passwordSource: "private_command" }, managedGroupCount: 1, groupCount: 3, persona: { sourceLabel: "默认", summary: "温和", name: "Yawn", locked: false }, sessionExpiresAt: 0 }, meta: {} });
      if (path === "/me/groups") return Promise.resolve({ data: [{ groupId: "900001", groupName: "猫猫群", role: "admin", memberCount: 3, lastActiveAt: null, enabledFeatures: 8, featureCount: 8, agentEnabled: true }], meta: {} });
      if (path === "/me/groups/900001") return Promise.resolve({ data: { groupId: "900001", groupName: "猫猫群", memberCount: 3, lastActiveAt: null, role: "owner", verified: true, features: FEATURES, featureHints: {} }, meta: {} });
      if (path === "/me/groups/900002") return Promise.reject(new Error("你不是这个群的群主或管理员"));
      if (path === "/me/groups/900003") return Promise.resolve({ data: { groupId: "900003", groupName: "旧群", memberCount: 3, lastActiveAt: null, role: "admin", verified: true, features: FEATURES, featureHints: {}, groupManage: { enabled: true, webui: false } }, meta: {} });
      if (path === "/me/groups/900001/manage") return Promise.resolve({ data: MANAGE, meta: {} });
      if (path.startsWith("/me/groups/900001/manage/members")) return Promise.resolve({ data: MEMBERS, meta: { total: 2, muted: 1 } });
      if (path === "/me/groups/900001/manage/preview") return Promise.resolve({ data: { summary: "在「猫猫群」禁言 小红（10002） 10 分钟", risk: "mid", label: "禁言" }, meta: {} });
      if (path === "/me/groups/900001/manage/actions") return Promise.resolve({ data: { summary: "在「猫猫群」禁言 小红（10002） 10 分钟", result: { done: true } }, meta: {} });
      if (path === "/me/groups/900001/features/checkin" && init?.method === "PATCH") {
        return Promise.resolve({ data: { ...FEATURES[0], override: false, effective: false, source: "group" }, meta: {} });
      }
      return Promise.reject(new Error(`unexpected API: ${path}`));
    });
  });

  it("lists only the groups the user manages", async () => {
    renderAt("/me");
    expect(await screen.findByText("你好，小明")).toBeInTheDocument();
    expect(await screen.findByText("猫猫群")).toBeInTheDocument();
    expect(screen.getByText("管理员")).toBeInTheDocument();
    expect(screen.getByText("猫猫群").closest("a")).toHaveAttribute("href", "/me/groups/900001");
  });

  it("opens relations read-only and toggles a group feature", async () => {
    renderAt("/me/groups/900001?tab=relations");
    expect(await screen.findByText("relations 900001 readonly")).toBeInTheDocument();
    fireEvent.click(screen.getByText("群功能设置"));
    expect(await screen.findByText("签到")).toBeInTheDocument();
    const row = screen.getByText("签到").closest("tr") ?? screen.getByText("签到").closest("div");
    expect(row).not.toBeNull();
    const off = screen.getAllByText("关")[0];
    fireEvent.click(off);
    await waitFor(() => {
      expect(state.api).toHaveBeenCalledWith(
        "/me/groups/900001/features/checkin",
        expect.objectContaining({ method: "PATCH", body: JSON.stringify({ override: false }) }),
      );
    });
  });

  it("explains when the user is not a manager of the group", async () => {
    renderAt("/me/groups/900002");
    expect(await screen.findByText("没有这个群的管理权限")).toBeInTheDocument();
    expect(state.api).not.toHaveBeenCalledWith(expect.stringContaining("/agent/groups/900002"), expect.anything());
  });

  it("defaults to the manage tab with live roles and blocked reasons", async () => {
    renderAt("/me/groups/900001");
    expect(await screen.findByText("今天已代办")).toBeInTheDocument();
    expect(screen.getByText("2 / 60")).toBeInTheDocument();
    expect(await screen.findByText("小红")).toBeInTheDocument();
    expect(screen.getByText("禁言到 11-03 12:00")).toBeInTheDocument();
    expect(screen.getAllByText(/^移\s?出$/)[0].closest("button")).toBeDisabled();
  });

  it("previews then confirms a mute", async () => {
    renderAt("/me/groups/900001");
    await screen.findByText("小红");
    fireEvent.click(screen.getAllByText(/^禁\s?言$/)[0]);
    fireEvent.click(await screen.findByText("禁言 10 分钟"));
    expect(await screen.findByText("在「猫猫群」禁言 小红（10002） 10 分钟")).toBeInTheDocument();
    expect(state.api).not.toHaveBeenCalledWith("/me/groups/900001/manage/actions", expect.anything());
    fireEvent.click(screen.getByText("确 认"));
    await waitFor(() => {
      expect(state.api).toHaveBeenCalledWith(
        "/me/groups/900001/manage/actions",
        expect.objectContaining({ method: "POST", body: JSON.stringify({ action: "mute", member: "10002", minutes: 10, confirm: true }) }),
      );
    });
  });

  it("embeds agent config and persona in manager mode", async () => {
    renderAt("/me/groups/900001?tab=agent");
    expect(await screen.findByText("agent-config 900001 manager")).toBeInTheDocument();
    fireEvent.click(screen.getByText("群人设"));
    expect(await screen.findByText("persona 900001 manager")).toBeInTheDocument();
  });

  it("hides manager tabs when web group management is off", async () => {
    renderAt("/me/groups/900003?tab=agent");
    expect(await screen.findByText("relations 900003 readonly")).toBeInTheDocument();
    expect(screen.queryByText("Agent 设置")).not.toBeInTheDocument();
    expect(screen.queryByText("群管理")).not.toBeInTheDocument();
  });
});
