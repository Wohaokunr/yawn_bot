import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("./api", () => ({ api: apiMock }));

import { CommandPalette } from "./command-palette";
import { ADMIN_NAV, GUEST_NAV, selectedNavKey } from "./nav-config";

function renderPalette(searchGroups = true) {
  const onNavigate = vi.fn();
  const onClose = vi.fn();
  render(
    <CommandPalette
      open
      entries={searchGroups ? ADMIN_NAV : GUEST_NAV}
      searchGroups={searchGroups}
      onClose={onClose}
      onNavigate={onNavigate}
    />,
  );
  return { onNavigate, onClose, input: screen.getByLabelText("搜索页面或群组") };
}

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue({
    data: [{ groupId: "10086", groupName: "摸鱼研究所", memberCount: 3, agentEnabled: true, firstSeenAt: "" }],
    meta: {},
  });
});

describe("CommandPalette", () => {
  it("按关键词过滤页面，回车跳到高亮项", () => {
    const { input, onNavigate, onClose } = renderPalette(false);
    fireEvent.change(input, { target: { value: "访客" } });
    expect(screen.getByText("可查看群聊")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onClose).toHaveBeenCalled();
    expect(onNavigate).toHaveBeenCalledWith("/guest");
  });

  it("管理员输入群名时查群，并提供 Agent 工作台与功能权限两个入口", async () => {
    const { input, onNavigate } = renderPalette(true);
    fireEvent.change(input, { target: { value: "摸鱼" } });
    expect(await screen.findByText("10086 · Agent 工作台")).toBeInTheDocument();
    expect(screen.getByText("10086 · 功能权限")).toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledWith(
      "/groups?page=1&pageSize=5&search=%E6%91%B8%E9%B1%BC",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    // 没有页面匹配“摸鱼”，第一项就是 Agent 工作台
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledWith("/agent/10086");
  });

  it("访客不会触发群组查询", async () => {
    const { input } = renderPalette(false);
    fireEvent.change(input, { target: { value: "摸鱼" } });
    await new Promise((resolve) => setTimeout(resolve, 260));
    expect(apiMock).not.toHaveBeenCalled();
    expect(screen.getByText("没有找到匹配的页面或群组")).toBeInTheDocument();
  });

  it("方向键循环移动高亮", () => {
    const { input, onNavigate } = renderPalette(true);
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledWith(ADMIN_NAV[ADMIN_NAV.length - 1].key);
  });
});

describe("selectedNavKey", () => {
  it("Agent 工作台高亮“群组”入口", () => {
    expect(selectedNavKey("/agent/1", false)).toBe("/groups");
    expect(selectedNavKey("/groups/1", false)).toBe("/groups");
    expect(selectedNavKey("/", false)).toBe("/overview");
    expect(selectedNavKey("/", true)).toBe("/guest");
  });
});

describe("group search debounce", () => {
  it("清空输入后不再保留群组结果", async () => {
    const { input } = renderPalette(true);
    fireEvent.change(input, { target: { value: "摸鱼" } });
    expect(await screen.findByText("10086 · Agent 工作台")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "" } });
    await waitFor(() => expect(screen.queryByText("10086 · Agent 工作台")).not.toBeInTheDocument());
  });
});
