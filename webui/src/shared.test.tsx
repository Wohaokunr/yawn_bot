import { MemoryRouter, Route, Routes, Link, useLocation } from "react-router-dom";
import { act, render, screen, fireEvent, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useApiQuery, RefreshErrorAlert, useListLocation } from "./shared";

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

  it("queues the newest load instead of overlapping a slow request", async () => {
    let resolveFirst!: (value: number) => void;
    let active = 0;
    let maxActive = 0;
    const loads = [
      () => new Promise<number>((resolve) => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        resolveFirst = (value) => { active -= 1; resolve(value); };
      }),
      () => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        active -= 1;
        return Promise.resolve(2);
      },
    ];
    const { result, rerender } = renderHook(({ index }) => useApiQuery(loads[index]), { initialProps: { index: 0 } });
    rerender({ index: 1 });
    expect(maxActive).toBe(1);
    await act(async () => { resolveFirst(1); });
    await waitFor(() => expect(result.current.data).toBe(2));
    expect(result.current.data).toBe(2);
    expect(result.current.error).toBe("");
    expect(maxActive).toBe(1);
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
