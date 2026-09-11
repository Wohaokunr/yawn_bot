import { MemoryRouter, Route, Routes, Link, useLocation } from "react-router-dom";
import { act, render, screen, fireEvent, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useApiQuery, useDraftSafeServerData, RefreshErrorAlert, useListLocation } from "./shared";

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

  it("ignores a late stale completion even when the obsolete fetcher does not reject on abort", async () => {
    let resolveFirst: ((value: number) => void) | null = null;
    let firstRequestAborted = false;
    const { result, rerender } = renderHook(({ keyValue }) => useApiQuery({
      queryKey: ["page", keyValue],
      fetcher: (signal) => {
        if (keyValue === 1) {
          return new Promise<number>((resolve) => {
            resolveFirst = resolve;
            signal.addEventListener("abort", () => { firstRequestAborted = true; });
          });
        }
        return Promise.resolve(2);
      },
    }), { initialProps: { keyValue: 1 } });

    rerender({ keyValue: 2 });
    expect(firstRequestAborted).toBe(true);
    await waitFor(() => expect(result.current.data).toBe(2));

    act(() => resolveFirst?.(1));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(result.current.data).toBe(2);
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
        detail: { resource: "agent_config", scope: { groupId: "200" }, entityId: "100" },
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

it("retains last successful data and exposes an actionable refresh error", async () => {
  const load = vi.fn().mockResolvedValueOnce(["saved row"]).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(["new row"]);
  function Page() {
    const query = useApiQuery<string[]>(load);
    return <><RefreshErrorAlert query={query} /><div>{query.data?.join()}</div><button onClick={query.reload}>refresh</button></>;
  }
  render(<Page />);
  await screen.findByText("saved row");
  fireEvent.click(screen.getByText("refresh"));
  await screen.findByText("更新失败，当前为上次成功数据");
  expect(screen.getByText("saved row")).toBeInTheDocument();
  expect(screen.getByText(/上次成功更新/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
  await screen.findByText("new row");
  expect(screen.queryByText("更新失败，当前为上次成功数据")).not.toBeInTheDocument();
});
it("preserves submitted list filters through a detail return and resets page on search", () => {
  function Page() {
    const { page, search, listSuffix, setSearch } = useListLocation();
    const location = useLocation();
    return <><div>{page}:{search}</div><Link to={`${location.pathname === "/list" ? "/detail" : "/list"}${listSuffix}`}>switch</Link><button onClick={() => setSearch("new")}>search</button></>;
  }
  render(<MemoryRouter initialEntries={["/list?page=3&search=hello"]}><Routes><Route path="*" element={<Page />} /></Routes></MemoryRouter>);
  expect(screen.getByText("3:hello")).toBeInTheDocument();
  fireEvent.click(screen.getByText("switch"));
  fireEvent.click(screen.getByText("switch"));
  expect(screen.getByText("3:hello")).toBeInTheDocument();
  fireEvent.click(screen.getByText("search"));
  expect(screen.getByText("1:new")).toBeInTheDocument();
});
