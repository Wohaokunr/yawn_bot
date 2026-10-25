import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AgentDeliveryPreview } from "../types";
import { DeliveryPreview } from "./ResultView";

const settings = { mode: "auto", max_parts: 3, pace: 1, max_spread_seconds: 10 };

describe("DeliveryPreview", () => {
  it("把分条按顺序画成气泡，并标出每条前的停顿", () => {
    const delivery: AgentDeliveryPreview = {
      rhythm: "burst",
      rhythmLabel: "连发",
      declared: "burst",
      source: "model",
      spreadMs: 1400,
      parts: [
        { text: "啊？", delayMs: 0 },
        { text: "不是吧", delayMs: 600 },
        { text: "真的假的", delayMs: 800 },
      ],
      settings,
    };
    render(<DeliveryPreview delivery={delivery} />);
    const list = screen.getByRole("list", { name: "分条发送预览" });
    expect(list.querySelectorAll("li")).toHaveLength(3);
    expect(screen.getByText("连发")).toBeTruthy();
    expect(screen.getByText("停 0.6s")).toBeTruthy();
    expect(screen.getByText("停 0.8s")).toBeTruthy();
    expect(screen.getByText(/共 3 条/)).toBeTruthy();
    expect(screen.getByText("模型用 ||| 标的")).toBeTruthy();
  });

  it("一条发完时不显示停顿；分条关闭时给出提示", () => {
    render(<DeliveryPreview delivery={{
      rhythm: "block",
      rhythmLabel: "整段",
      declared: "auto",
      source: "disabled",
      spreadMs: 0,
      parts: [{ text: "认真说一整段", delayMs: 0 }],
      settings: { ...settings, mode: "off" },
    }} />);
    expect(screen.queryByText(/^停 /)).toBeNull();
    expect(screen.getByText(/AGENT_REPLY_SPLIT=off/)).toBeTruthy();
    expect(screen.getByText("分条已关闭")).toBeTruthy();
  });
});
