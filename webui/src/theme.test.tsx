import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { THEME_STORAGE_KEY, ThemeProvider, ThemeToggle, readThemeMode, resolveTheme, useThemeMode } from "./theme";

function Probe() {
  const { mode, resolved } = useThemeMode();
  return <span data-testid="probe">{`${mode}/${resolved}`}</span>;
}

// antd 的 Dropdown 依赖 ResizeObserver，jsdom 没有
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

afterEach(() => {
  window.localStorage.removeItem(THEME_STORAGE_KEY);
  delete document.documentElement.dataset.theme;
});

describe("theme", () => {
  it("跟随系统时按系统偏好解析，显式选择优先", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("未知或缺失的存储值回退为跟随系统", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "purple");
    expect(readThemeMode()).toBe("system");
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    expect(readThemeMode()).toBe("dark");
  });

  it("把生效主题写到 html 上，并记住用户选择", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    render(
      <ThemeProvider>
        <Probe />
        <ThemeToggle />
      </ThemeProvider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("dark/dark");
    expect(document.documentElement.dataset.theme).toBe("dark");

    fireEvent.click(screen.getByRole("button", { name: /外观/ }));
    fireEvent.click(await screen.findByText("浅色"));
    expect(screen.getByTestId("probe").textContent).toBe("light/light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });
});
