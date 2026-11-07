import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/webui/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      // ws: true 让 /webui/api/v1/stream 的 WebSocket 也走代理，开发时顶栏的“实时连接”才会亮
      "/webui/api": { target: "http://127.0.0.1:8080", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/testSetup.ts",
    testTimeout: 10_000,
  },
});
