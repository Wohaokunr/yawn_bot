import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthSessionData } from "./auth-session";

const state = vi.hoisted(() => ({
  api: vi.fn(),
  openStatusStream: vi.fn(),
  setCsrfToken: vi.fn(),
}));

vi.mock("./api", () => ({
  api: state.api,
  openStatusStream: state.openStatusStream,
  setCsrfToken: state.setCsrfToken,
}));

import { Shell } from "./app-shell";

function session(role: "admin" | "guest", realtimeAdminStream: boolean): AuthSessionData {
  const isAdmin = role === "admin";
  return {
    authenticated: true,
    role,
    csrfToken: `${role}-csrf`,
    expiresAt: 123,
    capabilities: {
      adminConsole: isAdmin,
      adminWrite: isAdmin,
      realtimeAdminStream,
      guestGroupRead: !isAdmin,
    },
  };
}

describe("Shell realtime stream isolation", () => {
  beforeEach(() => {
    state.api.mockReset();
    state.openStatusStream.mockReset();
    state.setCsrfToken.mockReset();
    state.openStatusStream.mockReturnValue(vi.fn());
  });

  it("never opens the admin status stream for a guest, even if capability data is malformed", async () => {
    render(
      <MemoryRouter initialEntries={["/guest"]}>
        <Shell session={session("guest", true)} onLogout={vi.fn()} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("访客 · 只读")).toBeInTheDocument();
    await waitFor(() => expect(state.openStatusStream).not.toHaveBeenCalled());
    expect(screen.queryByText("实时连接")).not.toBeInTheDocument();
    expect(screen.queryByText("正在重连")).not.toBeInTheDocument();
  });

  it("keeps the existing admin realtime stream behavior", async () => {
    render(
      <MemoryRouter initialEntries={["/overview"]}>
        <Shell session={session("admin", true)} onLogout={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() => expect(state.openStatusStream).toHaveBeenCalledTimes(1));
    expect(screen.getByText("正在重连")).toBeInTheDocument();
  });
});
