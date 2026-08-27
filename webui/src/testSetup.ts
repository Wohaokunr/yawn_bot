import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(async () => {
  cleanup();
  // React 19 may schedule passive work with Node's setImmediate after unmount.
  // Give that work one event-loop turn before Vitest destroys the jsdom window.
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
});
