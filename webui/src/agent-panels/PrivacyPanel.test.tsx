import { App as AntApp } from "antd";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { api } from "../api";
import { PrivacyPanel } from "./PrivacyPanel";

vi.mock("../api", () => ({ api: vi.fn() }));

it("loads the last server page instead of paginating only the first 100 records", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.mocked(api).mockImplementation(async (path: string) => {
    const page = Number(new URL(path, "http://test").searchParams.get("page"));
    return {
      data: page === 6 ? [{ userId: "101", optedOut: true }] : Array.from({ length: 20 }, (_, i) => ({ userId: String(i + 1), optedOut: false })),
      meta: { page, pageSize: 20, total: 101 },
    } as never;
  });
  render(<AntApp><PrivacyPanel groupId="1" /></AntApp>);
  fireEvent.click(await screen.findByTitle("6"));
  expect(await screen.findByText("101")).toBeInTheDocument();
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining("page=6&pageSize=20"), expect.anything());
});
