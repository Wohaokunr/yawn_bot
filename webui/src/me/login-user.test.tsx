import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("../api", () => ({ api: state.api }));

import { initialLoginMode, Login } from "../login";

describe("QQ 账号登录", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() })));
    window.localStorage.clear();
    state.api.mockReset();
    state.api.mockResolvedValue({ data: { authenticated: true, role: "user", csrfToken: "c", expiresAt: 1, capabilities: { adminConsole: false, adminWrite: false, realtimeAdminStream: false, guestGroupRead: false, selfService: true }, userId: "10001" }, meta: {} });
  });

  it("keeps operator login as the default and opens QQ mode from the private-chat link", () => {
    expect(initialLoginMode("")).toBe("admin");
    expect(initialLoginMode("?login=qq")).toBe("user");
    window.localStorage.setItem("yawnbot.login.mode", "user");
    expect(initialLoginMode("")).toBe("user");
  });

  it("logs in with QQ number and password", async () => {
    const onSuccess = vi.fn();
    window.localStorage.setItem("yawnbot.login.mode", "user");
    render(<Login onSuccess={onSuccess} />);
    fireEvent.change(screen.getByPlaceholderText("你的 QQ 号"), { target: { value: "10001" } });
    fireEvent.change(screen.getByPlaceholderText("私聊里设的密码"), { target: { value: "Mao-mao-2026" } });
    fireEvent.click(screen.getByRole("button", { name: /登录我的空间/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(state.api).toHaveBeenCalledWith("/auth/user", expect.objectContaining({ method: "POST", body: JSON.stringify({ qq: "10001", password: "Mao-mao-2026" }) }));
  });

  it("sets a password with a one-time setup code", async () => {
    const onSuccess = vi.fn();
    window.localStorage.setItem("yawnbot.login.mode", "user");
    render(<Login onSuccess={onSuccess} />);
    fireEvent.click(screen.getByText(/用设置码设密码/));
    fireEvent.change(screen.getByPlaceholderText("你的 QQ 号"), { target: { value: "10001" } });
    fireEvent.change(screen.getByPlaceholderText("8 位设置码"), { target: { value: "abcd-2345" } });
    fireEvent.change(screen.getByPlaceholderText("设置一个新密码"), { target: { value: "Mao-mao-2026" } });
    fireEvent.change(screen.getByPlaceholderText("确认新密码"), { target: { value: "Mao-mao-2026" } });
    fireEvent.click(screen.getByRole("button", { name: /设置密码并登录/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(state.api).toHaveBeenCalledWith("/auth/user/setup", expect.objectContaining({ body: JSON.stringify({ qq: "10001", code: "ABCD2345", password: "Mao-mao-2026" }) }));
  });
});
