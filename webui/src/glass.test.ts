import { afterEach, describe, expect, it, vi } from "vitest";
import { installGlassGlow } from "./glass";

function stubMedia({ fine = true, reduced = false }: { fine?: boolean; reduced?: boolean }): void {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: query.includes("prefers-reduced-motion") ? reduced : fine,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
}

function glassHost(): HTMLElement {
  const host = document.createElement("div");
  host.className = "liquid-glass";
  const inner = document.createElement("span");
  host.appendChild(inner);
  document.body.appendChild(host);
  host.getBoundingClientRect = () => ({
    left: 100,
    top: 50,
    width: 400,
    height: 300,
    right: 500,
    bottom: 350,
    x: 100,
    y: 50,
    toJSON: () => ({}),
  }) as DOMRect;
  return host;
}

async function flushFrames(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 40));
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("installGlassGlow", () => {
  it("粗指针(触屏)不安装监听", async () => {
    stubMedia({ fine: false });
    installGlassGlow();
    const host = glassHost();
    host.dispatchEvent(
      new MouseEvent("pointermove", { bubbles: true, clientX: 220, clientY: 110 }),
    );
    await flushFrames();
    expect(host.style.getPropertyValue("--glass-mx")).toBe("");
  });

  it("prefers-reduced-motion 下不安装跟手光斑", async () => {
    stubMedia({ fine: true, reduced: true });
    installGlassGlow();
    const host = glassHost();
    host.dispatchEvent(
      new MouseEvent("pointermove", { bubbles: true, clientX: 220, clientY: 110 }),
    );
    await flushFrames();
    expect(host.style.getPropertyValue("--glass-mx")).toBe("");
  });

  it("指针划过玻璃内任意子元素时写入相对光斑坐标", async () => {
    stubMedia({ fine: true, reduced: false });
    installGlassGlow();
    const host = glassHost();
    const inner = host.querySelector("span") as HTMLElement;
    inner.dispatchEvent(
      new MouseEvent("pointermove", { bubbles: true, clientX: 260, clientY: 200 }),
    );
    await flushFrames();
    expect(host.style.getPropertyValue("--glass-mx")).toBe("160px");
    expect(host.style.getPropertyValue("--glass-my")).toBe("150px");
  });
});
