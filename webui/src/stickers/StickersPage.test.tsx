import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App as AntApp } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

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

import { StickersPage } from "./StickersPage";
import type { Sticker, StickerOverview, StickerSettings } from "./types";

const settings: StickerSettings = {
  enabled: true,
  groupMode: "all",
  allowedGroups: [],
  blockedGroups: [],
  frequency: "normal",
  groupCooldownSeconds: 60,
  groupDailyLimit: 60,
  maxPerMessage: 1,
  menuSize: 12,
  allowProactive: true,
  collectMode: "review",
  collectAdminOnly: false,
  memberDailyCollect: 10,
  maxBytes: 3145728,
  privateEnabled: true,
  privateDailyLimit: 20,
  privateCooldownSeconds: 20,
};

function sticker(overrides: Partial<Sticker>): Sticker {
  return {
    id: "s1",
    name: "狗头",
    tags: ["调侃"],
    description: "",
    enabled: true,
    status: "active",
    groups: [],
    source: "upload",
    addedBy: null,
    addedGroup: null,
    size: 2048,
    mime: "image/png",
    createdAt: "2026-10-27 10:00:00",
    updatedAt: "2026-10-27 10:00:00",
    uses: 3,
    lastUsed: null,
    fileMissing: false,
    ...overrides,
  };
}

function overview(overrides: Partial<StickerOverview> = {}): StickerOverview {
  return {
    settings,
    defaults: settings,
    frequencyLabels: { rare: "偶尔", normal: "适中", often: "爱发" },
    usage: { day: "2026-10-27", totals: { sent: 4, dropped: 1, collected: 1 }, groups: [{ groupId: "123456", sent: 4, groupName: "测试群" }], groupLimit: 60 },
    counts: { total: 2, active: 1, pending: 1, disabled: 0 },
    stickers: [
      sticker({ id: "s1", name: "狗头" }),
      sticker({ id: "s2", name: "白眼", tags: ["无语"], status: "pending", source: "member", addedBy: "42", addedGroup: "123456", uses: 0 }),
    ],
    tags: [{ tag: "调侃", count: 1 }, { tag: "无语", count: 1 }],
    groupNames: { "123456": "测试群" },
    ...overrides,
  };
}

function renderPage(path = "/stickers") {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={[path]}>
        <StickersPage />
      </MemoryRouter>
    </AntApp>,
  );
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }),
  });
});

beforeEach(() => {
  state.api.mockReset();
});

describe("StickersPage", () => {
  it("shows the library, starting on pending stickers when any wait for review", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage();
    expect(await screen.findByText("表情包 · 适中发")).toBeInTheDocument();
    expect(await screen.findByLabelText("编辑 白眼")).toBeInTheDocument();
    expect(screen.queryByLabelText("编辑 狗头")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("全部 2"));
    expect(await screen.findByLabelText("编辑 狗头")).toBeInTheDocument();
  });

  it("approves selected stickers in bulk", async () => {
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path === "/stickers/bulk") {
        expect(JSON.parse(String(init?.body))).toEqual({ ids: ["s2"], action: "approve" });
        return { data: { done: ["s2"], missing: [] }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage();
    fireEvent.click(await screen.findByLabelText("选择 白眼"));
    fireEvent.click(screen.getByRole("button", { name: /审核通过/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/bulk", expect.objectContaining({ method: "POST" })));
  });

  it("previews the prompt menu for a group", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path.startsWith("/stickers/prompt-preview")) {
        return { data: { open: true, reason: null, reasonText: null, available: 1, leftToday: 56, cooldownSeconds: 0, text: "可用表情包：狗头。" }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=preview");
    const select = await screen.findByRole("combobox");
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("测试群（123456）"));
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));
    expect(await screen.findByLabelText("提示词预览")).toHaveTextContent("可用表情包：狗头。");
  });
});
