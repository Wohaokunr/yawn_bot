import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
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

function TabHarness(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") ?? "overview";
  return <div>
    <div data-testid="tab">{tab}</div>
    <button type="button" onClick={() => setSearchParams({ tab: "config" })}>config</button>
    <button type="button" onClick={() => setSearchParams({ tab: "persona" })}>persona</button>
  </div>;
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

  it("restores an independent scroll position for each tab", async () => {
    render(
      <MemoryRouter initialEntries={["/agent/100?tab=config"]}>
        <Routes>
          <Route element={<Shell session={session("guest", false)} onLogout={vi.fn()} />}>
            <Route path="agent/:groupId" element={<TabHarness />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const region = document.querySelector<HTMLElement>(".app-content-scroll");
    expect(region).not.toBeNull();
    await waitFor(() => expect(screen.getByTestId("tab")).toHaveTextContent("config"));

    region!.scrollTop = 320;
    fireEvent.click(screen.getByRole("button", { name: "persona" }));
    await waitFor(() => expect(screen.getByTestId("tab")).toHaveTextContent("persona"));
    await waitFor(() => expect(region!.scrollTop).toBe(0));

    region!.scrollTop = 77;
    fireEvent.click(screen.getByRole("button", { name: "config" }));
    await waitFor(() => expect(screen.getByTestId("tab")).toHaveTextContent("config"));
    await waitFor(() => expect(region!.scrollTop).toBe(320));
  });
});
