import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useApiQuery, useDraftSafeServerData } from "./shared";

describe("useApiQuery", () => {
  it("resolves data and clears initial loading", async () => {
    const { result } = renderHook(() => useApiQuery({
      queryKey: ["one"],
      fetcher: () => Promise.resolve({ value: 1 }),
    }));
    expect(result.current.initialLoading).toBe(true);
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ value: 1 });
    expect(result.current.error).toBe("");
  });

  it("captures rejection as error message", async () => {
    const { result } = renderHook(() => useApiQuery({
      queryKey: ["error"],
      fetcher: () => Promise.reject(new Error("boom")),
    }));
    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.data).toBeNull();
  });

  it("aborts an obsolete request immediately when queryKey changes", async () => {
    let firstRequestAborted = false;
    const { result, rerender } = renderHook(({ keyValue }) => useApiQuery({
      queryKey: ["page", keyValue],
      fetcher: (signal) => {
        if (keyValue === 1) {
          return new Promise<number>((_resolve, reject) => {
            signal.addEventListener("abort", () => {
              firstRequestAborted = true;
              reject(new DOMException("aborted", "AbortError"));
            });
          });
        }
        return Promise.resolve(2);
      },
    }), { initialProps: { keyValue: 1 } });

    rerender({ keyValue: 2 });
    expect(firstRequestAborted).toBe(true);
    await waitFor(() => expect(result.current.data).toBe(2));
    expect(result.current.error).toBe("");
  });

  it("only invalidates the matching resource and group scope", async () => {
    let calls = 0;
    const { result } = renderHook(() => useApiQuery({
      queryKey: ["agent-config", "100"],
      fetcher: () => Promise.resolve(++calls),
      invalidation: { resources: ["agent_config"], scope: { groupId: "100" } },
    }));
    await waitFor(() => expect(result.current.data).toBe(1));

    act(() => {
      window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
        detail: { resource: "agent_config", scope: { groupId: "200" }, entityId: "200" },
      }));
    });
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(result.current.data).toBe(1);

    act(() => {
      window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
        detail: { resource: "agent_memory", scope: { groupId: "100" }, entityId: "1" },
      }));
    });
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(result.current.data).toBe(1);

    act(() => {
      window.dispatchEvent(new CustomEvent("yawnbot-entity-changed", {
        detail: { resource: "agent_config", scope: { groupId: "100" }, entityId: "100" },
      }));
    });
    await waitFor(() => expect(result.current.data).toBe(2));
  });
});

describe("useDraftSafeServerData", () => {
  it("keeps a dirty draft intact when a newer server version arrives", async () => {
    const hydrate = vi.fn();
    const v1 = { version: "v1", value: "old" };
    const v2 = { version: "v2", value: "remote" };
    const { result, rerender } = renderHook(
      ({ data, dirty }) => useDraftSafeServerData(data, dirty, hydrate),
      { initialProps: { data: v1, dirty: false } },
    );

    await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(1));
    expect(hydrate).toHaveBeenLastCalledWith(v1);

    rerender({ data: v2, dirty: true });
    await waitFor(() => expect(result.current.remoteUpdate).toEqual(v2));
    expect(hydrate).toHaveBeenCalledTimes(1);

    act(() => result.current.keepDraft());
    expect(result.current.remoteUpdate).toBeNull();
    expect(hydrate).toHaveBeenCalledTimes(1);
  });

  it("only overwrites the form after the user explicitly reloads the remote version", async () => {
    const hydrate = vi.fn();
    const v1 = { version: "v1", value: "old" };
    const v2 = { version: "v2", value: "remote" };
    const { result, rerender } = renderHook(
      ({ data, dirty }) => useDraftSafeServerData(data, dirty, hydrate),
      { initialProps: { data: v1, dirty: false } },
    );
    await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(1));

    rerender({ data: v2, dirty: true });
    await waitFor(() => expect(result.current.remoteUpdate).toEqual(v2));
    act(() => result.current.reloadRemote());

    expect(hydrate).toHaveBeenCalledTimes(2);
    expect(hydrate).toHaveBeenLastCalledWith(v2);
    expect(result.current.remoteUpdate).toBeNull();
  });
});
