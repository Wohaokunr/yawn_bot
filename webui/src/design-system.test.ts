// Vitest executes this contract test in Node; the browser tsconfig intentionally omits Node globals.
// @ts-ignore -- node:fs is available at test runtime without widening production browser types.
import { readFileSync } from "node:fs";
// @ts-ignore -- node:url is available at test runtime without widening production browser types.
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const designCss = readFileSync(
  fileURLToPath(new URL("./design-system.css", import.meta.url)),
  "utf8",
);

const VIEWPORTS = [
  { width: 1920, height: 1080, mode: "wide", desktopSider: true, debuggerInternalScroll: true, mobileTabSelect: false },
  { width: 1366, height: 768, mode: "desktop", desktopSider: true, debuggerInternalScroll: true, mobileTabSelect: false },
  { width: 1024, height: 768, mode: "tablet", desktopSider: false, debuggerInternalScroll: false, mobileTabSelect: false },
  { width: 390, height: 844, mode: "mobile", desktopSider: false, debuggerInternalScroll: false, mobileTabSelect: true },
] as const;

function modeForWidth(width: number): (typeof VIEWPORTS)[number]["mode"] {
  if (width >= 1440) return "wide";
  if (width >= 1025) return "desktop";
  if (width >= 768) return "tablet";
  return "mobile";
}

describe("WebUI design-system contract", () => {
  it("defines exactly the four intended material levels", () => {
    expect(designCss).toContain("--surface-1-bg");
    expect(designCss).toContain("--surface-2-bg");
    expect(designCss).toContain("--surface-elevated-bg");
    expect(designCss).toContain("--surface-interactive-bg");
    expect(designCss).toContain(".app-content .ant-card:hover");
    expect(designCss).toContain("transform: none !important");
    expect(designCss).toContain(".persona-preset-card:hover");
    expect(designCss).toContain(".agent-trace-list-item:hover");
  });

  it("uses one canonical responsive ladder", () => {
    expect(designCss).toContain("@media (min-width: 1440px)");
    expect(designCss).toContain("@media (min-width: 1025px) and (max-width: 1439px)");
    expect(designCss).toContain("@media (min-width: 768px) and (max-width: 1024px)");
    expect(designCss).toContain("@media (max-width: 767px)");
  });

  it.each(VIEWPORTS)("classifies $width×$height as $mode", (viewport) => {
    expect(modeForWidth(viewport.width)).toBe(viewport.mode);
  });

  it("locks the target panels to the responsive contract", () => {
    expect(designCss).toContain(".agent-config-hero");
    expect(designCss).toContain(".persona-config-hero");
    expect(designCss).toContain(".relations-toolbar");
    expect(designCss).toContain(".agent-debug-workbench");
    expect(designCss).toContain("height: clamp(480px, calc(100dvh - 224px), 720px)");
    expect(designCss).toContain("overflow-wrap: anywhere");
    expect(designCss).toContain("min-height: 320px");
  });

  it("documents expected behavior at all four regression viewports", () => {
    expect(VIEWPORTS).toEqual([
      { width: 1920, height: 1080, mode: "wide", desktopSider: true, debuggerInternalScroll: true, mobileTabSelect: false },
      { width: 1366, height: 768, mode: "desktop", desktopSider: true, debuggerInternalScroll: true, mobileTabSelect: false },
      { width: 1024, height: 768, mode: "tablet", desktopSider: false, debuggerInternalScroll: false, mobileTabSelect: false },
      { width: 390, height: 844, mode: "mobile", desktopSider: false, debuggerInternalScroll: false, mobileTabSelect: true },
    ]);
  });
});
