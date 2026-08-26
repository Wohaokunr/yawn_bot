import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Suspense } from "react";
import { MemoryRouter, Outlet, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  lazyLoads: [] as string[],
  api: vi.fn(),
}));

vi.mock("./api", () => ({
  api: state.api,
  setCsrfToken: vi.fn(),
}));

vi.mock("./app-shell", () => ({
  Shell: () => (
    <Suspense fallback={<div>route loading</div>}>
      <Outlet />
    </Suspense>
  ),
}));

vi.mock("./login", () => ({ Login: () => <div>login page</div> }));
vi.mock("./guest-home", () => ({ GuestHome: () => <div>guest home</div> }));
vi.mock("./overview", () => ({
  OverviewPage: () => <div>overview page</div>,
  AI_OUTCOME_META: {},
  aiOutcomeMeta: vi.fn(),
  fanqieSummary: vi.fn(),
  formatLatency: vi.fn(),
  formatRate: vi.fn(),
  formatUptime: vi.fn(),
}));

vi.mock("./access-pages", () => {
  state.lazyLoads.push("access-pages");
  return {
    GroupsPage: () => <div>groups page</div>,
    GroupDetailPage: () => <div>group detail page</div>,
    UsersPage: () => <div>users page</div>,
  };
});
vi.mock("./agent", () => {
  state.lazyLoads.push("agent");
  return {
    AgentGroupsPage: () => <div>agent groups page</div>,
    AgentDetailPage: () => <div>agent detail page</div>,
  };
});
vi.mock("./games", () => {
  state.lazyLoads.push("games");
  return { GamesPage: () => <div>games page</div> };
});
vi.mock("./modules", () => {
  state.lazyLoads.push("modules");
  return { ModulesPage: () => <div>modules page</div> };
});
vi.mock("./fanqie", () => {
  state.lazyLoads.push("fanqie");
  return { FanqiePage: () => <div>fanqie page</div> };
});
vi.mock("./environment", () => {
  state.lazyLoads.push("environment");
  return { EnvironmentPage: () => <div>environment page</div> };
});
vi.mock("./audits", () => {
  state.lazyLoads.push("audits");
  return { WebAuditsPage: () => <div>audits page</div> };
});

import App from "./App";

function RouteJump(): React.JSX.Element {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/groups")}>open groups</button>;
}

describe("route lazy loading", () => {
  beforeEach(() => {
    state.lazyLoads.length = 0;
    state.api.mockReset();
    state.api.mockResolvedValue({
      data: {
        authenticated: true,
        role: "admin",
        csrfToken: "csrf",
        expiresAt: 123,
        capabilities: {
          adminConsole: true,
          adminWrite: true,
          realtimeAdminStream: true,
          guestGroupRead: false,
        },
      },
    });
  });

  it("loads only the route module that becomes active", async () => {
    render(
      <MemoryRouter initialEntries={["/overview"]}>
        <RouteJump />
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("overview page")).toBeInTheDocument();
    expect(state.lazyLoads).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "open groups" }));
    expect(await screen.findByText("groups page")).toBeInTheDocument();
    await waitFor(() => expect(state.lazyLoads).toEqual(["access-pages"]));
  });

  it("keeps guest sessions out of admin routes and lazy modules", async () => {
    state.api.mockResolvedValueOnce({
      data: {
        authenticated: true,
        role: "guest",
        csrfToken: "guest-csrf",
        expiresAt: 123,
        capabilities: {
          adminConsole: false,
          adminWrite: false,
          realtimeAdminStream: false,
          guestGroupRead: false,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/environment"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByText("guest home")).toBeInTheDocument();
    expect(screen.queryByText("environment page")).not.toBeInTheDocument();
    expect(state.lazyLoads).toEqual([]);
  });
});
