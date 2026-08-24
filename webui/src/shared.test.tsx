import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useApiQuery } from "./shared";

describe("useApiQuery", () => {
  it("resolves data and clears loading", async () => {
    // loader 引用必须稳定(与页面里的 useCallback 约定一致),否则会触发重取循环。
    const load = () => Promise.resolve({ value: 1 });
    const { result } = renderHook(() => useApiQuery(load));
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ value: 1 });
    expect(result.current.error).toBe("");
  });

  it("captures rejection as error message", async () => {
    const load = () => Promise.reject(new Error("boom"));
    const { result } = renderHook(() => useApiQuery(load));
    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.data).toBeNull();
  });

  it("falls back to a generic message for non-Error rejections", async () => {
    const load = () => Promise.reject("nope");
    const { result } = renderHook(() => useApiQuery(load));
    await waitFor(() => expect(result.current.error).toBe("加载失败"));
  });

  it("keeps the newest response when an older request resolves late", async () => {
    let resolveFirst!: (value: number) => void;
    const loads = [
      () => new Promise<number>((resolve) => { resolveFirst = resolve; }),
      () => Promise.resolve(2),
    ];
    const { result, rerender } = renderHook(({ index }) => useApiQuery(loads[index]), { initialProps: { index: 0 } });
    rerender({ index: 1 });
    await waitFor(() => expect(result.current.data).toBe(2));
    await act(async () => { resolveFirst(1); });
    expect(result.current.data).toBe(2);
    expect(result.current.error).toBe("");
  });

  it("reloads on demand and only for subscribed entity.changed resources", async () => {
    let calls = 0;
    const load = () => { calls += 1; return Promise.resolve(calls); };
    const { result } = renderHook(() => useApiQuery(load, { resources: ["agent_config"] }));
    await waitFor(() => expect(result.current.data).toBe(1));
    await act(async () => { result.current.reload(); });
    await waitFor(() => expect(result.current.data).toBe(2));
    act(() => {
      window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
        detail: { resource: "agent_memory", resourceId: "100" },
      }));
    });
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(result.current.data).toBe(2);
    act(() => {
      window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
        detail: { resource: "agent_config", resourceId: "100" },
      }));
    });
    await waitFor(() => expect(result.current.data).toBe(3));
  });
});
