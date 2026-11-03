import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(async () => {
  cleanup();
  // React 19 may schedule passive work with Node's setImmediate after unmount.
  // antd Form's error-list debounce (useDelayState) also sets an uncancelled
  // 10ms timer on unmount. Wait past it before Vitest destroys the jsdom window,
  // otherwise a busy CI runner can fire it after teardown ("window is not defined").
  await new Promise<void>((resolve) => setTimeout(resolve, 20));
});
