import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
import { fixtureCounts, fixtureOverview, fixtureSettings, fixtureSticker } from "./sticker-fixtures";
import { emptyOverride } from "./stickers-meta";

const sticker = fixtureSticker;
const overview = fixtureOverview;

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
    const select = (await screen.findAllByRole("combobox"))[0];
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("测试群（123456）"));
    fireEvent.click(screen.getByRole("button", { name: /预览/ }));
    expect(await screen.findByLabelText("提示词预览")).toHaveTextContent("可用表情包：狗头。");
  });
  it("starts a batch AI labeling job for unlabeled stickers", async () => {
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path === "/stickers/label-jobs" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ scope: "unlabeled", mode: "fill" });
        return { data: { job: { id: "j1", status: "running", mode: "fill", modeLabel: "只补空的", fields: ["name"], total: 2, done: 0, changed: 0, unchanged: 0, failed: 0, held: 0, createdAt: 0, finishedAt: null, elapsedMs: 0 } }, meta: {} };
      }
      if (path === "/stickers/label-jobs") return { data: { jobs: [], vision: overview().vision }, meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=ai");
    fireEvent.click(await screen.findByRole("button", { name: /标注没标过的 2 张/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/label-jobs", expect.objectContaining({ method: "POST" })));
  });

  it("lists stickers the bot stole with where they came from", async () => {
    const stolen = sticker({ id: "s3", name: "偷来的猫", source: "stolen", origin: { via: "steal", groupId: "123456", senderId: "42", senderName: "小明", messageId: null, qqSticker: true } });
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview({ stickers: [stolen], counts: { ...fixtureCounts, total: 1, active: 1, pending: 0, stolen: 1, unlabeled: 1 } }), meta: {} };
      if (path === "/stickers/label-jobs") return { data: { jobs: [], vision: overview().vision }, meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=ai");
    expect(await screen.findByText("Bot 偷来的表情包（1）")).toBeInTheDocument();
    expect(screen.getAllByText(/小明（42） 发的/).length).toBeGreaterThan(0);
  });

  it("keeps library filters in the URL and sorts pinned stickers first", async () => {
    const rows = [sticker({ id: "s1", name: "狗头" }), sticker({ id: "s2", name: "置顶猫", pinned: true, aliases: ["喵"] }), sticker({ id: "s3", name: "动图", animated: true, frames: 8 })];
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview({ stickers: rows, counts: { ...fixtureCounts, total: 3, active: 3, pending: 0, pinned: 1, animated: 1 } }), meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.flag=animated");
    expect(await screen.findByLabelText("编辑 动图")).toBeInTheDocument();
    expect(screen.queryByLabelText("编辑 狗头")).not.toBeInTheDocument();
    expect(screen.getByText("筛出 1 张")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清掉筛选" }));
    const cards = await screen.findAllByLabelText(/^编辑 /);
    expect(cards.map((card) => card.getAttribute("aria-label"))).toEqual(["编辑 置顶猫", "编辑 狗头", "编辑 动图"]);
    expect(screen.getByLabelText("取消置顶 置顶猫")).toHaveAttribute("aria-pressed", "true");
  });

  it("pins a sticker from its card and selects everything that is filtered", async () => {
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path === "/stickers/s2" && init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toEqual({ pinned: true });
        return { data: { sticker: sticker({ id: "s2", pinned: true }) }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.f=all");
    fireEvent.click(await screen.findByLabelText("置顶 白眼"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/s2", expect.objectContaining({ method: "PATCH" })));
    fireEvent.click(screen.getByRole("button", { name: "全选这 2 张" }));
    expect(await screen.findByText("已选 2 张")).toBeInTheDocument();
  });

  it("opens the edit dialog from the URL with the new fields and saves them", async () => {
    const row = sticker({ id: "s1", name: "狗头", aliases: ["doge"], note: "群主最爱", weight: 2, dimensions: "240×240", sentToday: 2, groupUses: [{ groupId: "123456", uses: 3 }] });
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview({ stickers: [row], counts: { ...fixtureCounts, total: 1, active: 1, pending: 0 } }), meta: {} };
      if (path === "/stickers/s1" && init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toMatchObject({ name: "狗头", aliases: ["doge"], note: "群主最爱", weight: 2, pinned: true, dailyCap: 0, packs: [] });
        return { data: { sticker: row }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.edit=s1");
    expect(await screen.findByText(/编辑表情包 · 狗头/)).toBeInTheDocument();
    expect(screen.getByLabelText("备注")).toHaveValue("群主最爱");
    expect(screen.getByText(/测试群 3/)).toBeInTheDocument();
    expect(screen.getByText(/今天已经发了 2 次/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "置顶" }));
    fireEvent.click(screen.getByRole("button", { name: /保存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/s1", expect.objectContaining({ method: "PATCH" })));
  });

  it("warns when the index file was recovered from a backup", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview({ index: { corrupt: true, recoveredFromBackup: true, hasBackup: true } }), meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage();
    expect(await screen.findByText("表情包索引文件坏过")).toBeInTheDocument();
    expect(screen.getByText("索引坏过")).toBeInTheDocument();
  });

  it("shows why each menu entry is there and which sticker a name resolves to", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview({ settings: { ...fixtureSettings, groupOverrides: { "123456": { ...emptyOverride(), menuSize: 5 } } } }), meta: {} };
      if (path.startsWith("/stickers/prompt-preview")) {
        expect(path).toContain("scene=conversation");
        return { data: { open: true, reason: null, reasonText: null, available: 2, leftToday: 50, leftHour: 3, cooldownSeconds: 0, text: "可用表情包：狗头。", chars: 9, menu: [{ id: "s1", name: "狗头", hint: "调侃", why: "pinned" }], overridden: ["menuSize"] }, meta: {} };
      }
      if (path.startsWith("/stickers/resolve")) {
        return { data: { match: "alias", matchText: "按别名", candidates: [{ id: "s1", name: "狗头" }], count: 1, gateOpen: true, reasonText: null }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=preview&stk.group=123456");
    fireEvent.click(await screen.findByRole("button", { name: /预览/ }));
    expect(await screen.findByText(/这个群有单独设置：菜单大小/)).toBeInTheDocument();
    expect(screen.getByText(/这个小时还能发 3 张/)).toBeInTheDocument();
    expect(within(screen.getByLabelText("菜单里的每一项")).getByText("置顶")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("要试的名字"), { target: { value: "doge" } });
    fireEvent.click(screen.getByRole("button", { name: /试一下/ }));
    expect(await screen.findByText("按别名对上了 1 张")).toBeInTheDocument();
  });

  it("turns a name the model wrote but the library lacks into an alias", async () => {
    const insights = {
      days: 30,
      history: [{ day: "2026-10-26", sent: 1, dropped: 0, collected: 0, stolen: 0, labeled: 0, private_sent: 0 }, { day: "2026-10-27", sent: 4, dropped: 1, collected: 1, stolen: 0, labeled: 0, private_sent: 2 }],
      trend: { sent: { recent: 5, previous: 0, changePercent: null }, stolen: { recent: 0, previous: 0, changePercent: null }, collected: { recent: 1, previous: 0, changePercent: null }, dropped: { recent: 1, previous: 0, changePercent: null } },
      totals: { uses: 5, usable: 1, everywhere: 1, neverUsed: 0, animated: 0, bytes: 2048, concentration: 100 },
      top: [{ id: "s1", name: "狗头", uses: 5, lastUsed: null, sentToday: 4 }],
      neverUsed: [],
      groups: [{ groupId: "123456", private: false, uses: 5, today: 4, favorite: "狗头" }],
      moods: [{ mood: "开心", count: 1 }, { mood: "无语", count: 0 }],
      moodGaps: ["无语"],
      moodUnknown: 1,
      sources: [{ source: "upload", label: "上传", count: 2 }],
      tags: [{ tag: "调侃", count: 1, uses: 5 }],
      drops: [{ reason: "unknown", reasonText: "模型写的名字在本群可用的库里对不上", count: 1 }],
      misses: [{ name: "doge", count: 3, last: "2026-10-27 10:00:00", groups: ["123456"], suggestions: [{ id: "s1", name: "狗头" }] }],
      missCount: 1,
      recent: [],
      growth: [{ week: "10-26", total: 2, upload: 2 }],
      contributors: [],
    };
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path.startsWith("/stickers/insights")) return { data: insights, meta: {} };
      if (path === "/stickers/misses/resolve") {
        expect(JSON.parse(String(init?.body))).toEqual({ name: "doge", action: "alias", stickerId: "s1" });
        return { data: { removed: 1 }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=insights");
    expect(await screen.findByText("模型想发但库里没有的名字（1）")).toBeInTheDocument();
    expect(screen.getByText(/这些情绪一张都没有：无语/)).toBeInTheDocument();
    expect(screen.getByText("老发那几张")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /加成别名/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/misses/resolve", expect.objectContaining({ method: "POST" })));
  });

  it("runs a health check and fixes what can be fixed in one click", async () => {
    const health = {
      score: 72,
      checkedAt: "2026-10-27 10:00:00",
      total: 2,
      issues: [
        { kind: "orphan_file", severity: "warn", title: "没人引用的图片文件", advice: "可以删掉腾地方", count: 2, ids: [], fixable: true, files: ["a.png", "b.png"] },
        { kind: "never_used", severity: "info", title: "入库超过 30 天一次没发过", advice: "可能名字起得不好", count: 1, ids: ["s2"], fixable: false },
      ],
      index: { corrupt: false, recoveredFromBackup: false, hasBackup: true },
      trash: { count: 0, bytes: 0 },
      bytes: 4096,
    };
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview(), meta: {} };
      if (path === "/stickers/health") return { data: health, meta: {} };
      if (path === "/stickers/health/fix") {
        expect(JSON.parse(String(init?.body))).toEqual({ kind: "orphan_file" });
        return { data: { kind: "orphan_file", fixed: 2, health: { ...health, issues: [], score: 100 } }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=health");
    expect(await screen.findByLabelText("体检得分 72")).toBeInTheDocument();
    const list = screen.getByLabelText("体检结果");
    expect(within(list).getByText("白眼")).toBeInTheDocument();
    expect(within(list).getAllByRole("button", { name: /一键修/ })).toHaveLength(1);
    fireEvent.click(within(list).getByRole("button", { name: /一键修/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^确\s?定$|^OK$/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/health/fix", expect.objectContaining({ method: "POST" })));
  });

  it("creates a pack and toggles an existing one", async () => {
    const pack = { id: "p1", name: "猫猫", description: "全是猫", enabled: true, groups: ["123456"], createdAt: null, count: 1, uses: 3, cover: ["s1"] };
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview({ packs: [pack], counts: { ...fixtureCounts, packs: 1 } }), meta: {} };
      if (path === "/stickers/packs/p1" && init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toEqual({ enabled: false });
        return { data: { packs: [{ ...pack, enabled: false }] }, meta: {} };
      }
      if (path === "/stickers/packs" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ name: "本群梗图", description: "", groups: [] });
        return { data: { id: "p2", packs: [pack] }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=packs");
    expect(await screen.findByText("只在 测试群用")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "启用合集 猫猫" }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/packs/p1", expect.objectContaining({ method: "PATCH" })));
    fireEvent.click(screen.getByRole("button", { name: /新建合集/ }));
    fireEvent.change(await screen.findByLabelText("合集名字"), { target: { value: "本群梗图" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/packs", expect.objectContaining({ method: "POST" })));
  });

  it("restores a sticker from the trash and lifts a block", async () => {
    const trashed = sticker({ id: "s9", name: "删掉的", deletedAt: "2026-10-27 10:00:00" });
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview({ counts: { ...fixtureCounts, trash: 1, blocked: 1 } }), meta: {} };
      if (path === "/stickers/trash") return { data: { stickers: [trashed], trashDays: 14, autoPurged: 1, blocked: ["abcdef0123456789abcdef"] }, meta: {} };
      if (path === "/stickers/trash/s9/restore") return { data: { sticker: trashed }, meta: {} };
      if (path.startsWith("/stickers/blocked/") && init?.method === "DELETE") return { data: { blocked: [] }, meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=trash");
    expect(await screen.findByText(/刚刚自动清掉了 1 张/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("恢复 删掉的"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/trash/s9/restore", expect.objectContaining({ method: "POST" })));
    fireEvent.click(screen.getByLabelText("解除拉黑 abcdef01"));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/blocked/abcdef0123456789abcdef", expect.objectContaining({ method: "DELETE" })));
  });

  it("edits a single group's override in the policy tab and saves it", async () => {
    state.api.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/stickers") return { data: overview({ settings: { ...fixtureSettings, groupOverrides: { "123456": { ...emptyOverride(), frequency: "often" } } } }), meta: {} };
      if (path === "/stickers/settings" && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        expect(body.groupOverrides["123456"]).toMatchObject({ frequency: "often", note: "梗图群" });
        expect(body.quietEnabled).toBe(true);
        return { data: { settings: body }, meta: {} };
      }
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=policy");
    expect(await screen.findByText("按群单独设置（1）")).toBeInTheDocument();
    expect(screen.getByText(/123456 · 改了性格/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /测试群/ }));
    fireEvent.change(await screen.findByLabelText("群 123456 的备注"), { target: { value: "梗图群" } });
    fireEvent.click(screen.getByRole("switch", { name: "安静时段" }));
    expect(await screen.findByLabelText("安静时段开始")).toHaveValue("00:00");
    fireEvent.click(screen.getByRole("button", { name: /保\s?存/ }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith("/stickers/settings", expect.objectContaining({ method: "PUT" })));
  });

  it("warns when no vision model is configured", async () => {
    state.api.mockImplementation(async (path: string) => {
      if (path === "/stickers") return { data: overview({ vision: { ready: false, model: "", provider: "", stats: { requests: 0, ok: 0, failed: 0, cached: 0, cacheSize: 0, lastError: null } } }), meta: {} };
      if (path === "/stickers/label-jobs") return { data: { jobs: [], vision: overview().vision }, meta: {} };
      throw new Error(`unexpected ${path}`);
    });
    renderPage("/stickers?stk.tab=ai");
    expect((await screen.findAllByText(/AI_VISION_MODEL/)).length).toBeGreaterThan(0);
  });
});
