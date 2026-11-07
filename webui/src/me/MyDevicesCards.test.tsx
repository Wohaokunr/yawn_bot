import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn(), setCsrfToken: vi.fn() }));

vi.mock("../api", () => ({
  api: state.api,
  setCsrfToken: state.setCsrfToken,
  ApiError: class ApiError extends Error {},
}));

import { loginNoticeText } from "../App";
import { MyDevicesCards } from "./MyDevicesCards";

function security(devices = DEVICES) {
  return {
    devices,
    activeCount: devices.length,
    logins: [
      { at: "2026-11-07T09:00:00", device: "Windows · Chrome", location: "198.51.100.*", method: "password", methodLabel: "密码登录", newLocation: true, remember: true },
      { at: "2026-11-06T20:00:00", device: "iPhone · 微信", location: "203.0.113.*", method: "password", methodLabel: "密码登录", newLocation: false, remember: false },
    ],
    alertMode: "new_location",
    alertModes: [
      { value: "new_location", label: "仅新地点登录时提醒" },
      { value: "always", label: "每次登录都提醒" },
      { value: "off", label: "不提醒" },
    ],
    alertAvailable: true,
    rememberDays: 30,
    sessionRemember: true,
  };
}

const DEVICES = [
  { id: "aaaa", device: "Windows · Chrome", location: "198.51.100.*", lastLocation: "198.51.100.*", createdAt: "2026-11-07T09:00:00", lastSeenAt: "2026-11-07T09:30:00", expiresAt: null, remember: true, method: "password", current: true },
  { id: "bbbb", device: "iPhone · 微信", location: "203.0.113.*", lastLocation: "203.0.113.*", createdAt: "2026-11-06T20:00:00", lastSeenAt: "2026-11-06T21:00:00", expiresAt: null, remember: false, method: "password", current: false },
];

function renderCards(onLoggedOut = vi.fn()) {
  render(<AntApp><MyDevicesCards onLoggedOut={onLoggedOut} /></AntApp>);
  return onLoggedOut;
}

describe("登录设备与登录记录", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    vi.stubGlobal("ResizeObserver", class { observe(): void {} unobserve(): void {} disconnect(): void {} });
    state.api.mockReset();
    state.setCsrfToken.mockReset();
  });

  it("lists devices with the current one marked and flags new-location logins", async () => {
    state.api.mockResolvedValue({ data: security(), meta: {} });
    renderCards();
    expect(await screen.findByText(/2 台在线/)).toBeInTheDocument();
    expect(screen.getByText("这台设备")).toBeInTheDocument();
    expect(screen.getAllByText("iPhone · 微信").length).toBeGreaterThan(0);
    expect(screen.getByText("新地点")).toBeInTheDocument();
    expect(screen.getByText(/30 天内打开免登录/)).toBeInTheDocument();
  });

  it("logs out another device without leaving the page", async () => {
    state.api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/me/devices/bbbb" && init?.method === "DELETE") {
        return Promise.resolve({ data: { ...security([DEVICES[0]]), current: false }, meta: {} });
      }
      return Promise.resolve({ data: security(), meta: {} });
    });
    const onLoggedOut = renderCards();
    fireEvent.click(await screen.findByRole("button", { name: /^下\s?线$/ }));
    // 弹出确认框后，确认按钮同样叫“下线”，是最后一个
    await waitFor(() => expect(screen.getAllByRole("button", { name: /^下\s?线$/ }).length).toBe(2));
    fireEvent.click(screen.getAllByRole("button", { name: /^下\s?线$/ })[1]);
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/devices/bbbb", expect.objectContaining({ method: "DELETE" })));
    expect(onLoggedOut).not.toHaveBeenCalled();
  });

  it("changes the login alert preference", async () => {
    state.api.mockResolvedValue({ data: security(), meta: {} });
    renderCards();
    await screen.findByText(/2 台在线/);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "登录提醒" }));
    fireEvent.click(await screen.findByText("不提醒"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/me/security/login-alert", expect.objectContaining({ method: "PUT", body: JSON.stringify({ mode: "off" }) })));
  });
});

describe("登录提示", () => {
  it("warns about a new location and greets returning users", () => {
    const warn = loginNoticeText({ newLocation: true, location: "198.51.100.*", device: "Windows · Chrome", activeCount: 2, previous: null });
    expect(warn?.warn).toBe(true);
    expect(warn?.description).toContain("198.51.100.*");
    const back = loginNoticeText({
      newLocation: false, location: "203.0.113.*", device: "iPhone · 微信", activeCount: 1,
      previous: { at: "2026-11-06T20:00:00", device: "iPhone · 微信", location: "203.0.113.*", method: "password", methodLabel: "密码登录", newLocation: false, remember: false },
    });
    expect(back?.warn).toBe(false);
    expect(back?.description).toContain("2026-11-06 20:00");
    expect(loginNoticeText({ newLocation: false, location: "x", device: "y", activeCount: 1, previous: null })).toBeNull();
  });
});
