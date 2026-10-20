import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, getCsrfToken, setCsrfToken } from "./api";

describe("api client", () => {
  afterEach(() => { vi.unstubAllGlobals(); setCsrfToken("", null); });

  it("adds the CSRF header to mutations", async () => {
    setCsrfToken("csrf-value");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {}, meta: {} }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/groups/1/features/rpg", { method: "PATCH", body: "{}" });
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect((request.headers as Headers).get("X-CSRF-Token")).toBe("csrf-value");
    expect(getCsrfToken()).toBe("csrf-value");
  });

  it("refreshes a stale CSRF token from the session once and retries the write", async () => {
    setCsrfToken("stale", "admin");
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ error: { message: "CSRF 校验失败", fields: {} } }, 403))
      .mockResolvedValueOnce(json({ data: { role: "admin", csrfToken: "fresh" }, meta: {} }))
      .mockResolvedValueOnce(json({ data: { ok: true }, meta: {} }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api("/groups/1/features/rpg", { method: "PATCH", body: "{}" })).resolves.toMatchObject({ data: { ok: true } });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe("/webui/api/v1/auth/session");
    expect(((fetchMock.mock.calls[2][1] as RequestInit).headers as Headers).get("X-CSRF-Token")).toBe("fresh");
    expect(getCsrfToken()).toBe("fresh");
  });

  it("does not retry a non-CSRF 403", async () => {
    setCsrfToken("token", "admin");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: "需要管理员权限", fields: {} } }), { status: 403, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api("/groups/1/features/rpg", { method: "PATCH", body: "{}" })).rejects.toMatchObject({ status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("preserves conflict status and message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: "配置已更新", fields: {} } }), { status: 409, headers: { "Content-Type": "application/json" } })));
    await expect(api("/agent/groups/1/config")).rejects.toMatchObject({ status: 409, message: "配置已更新" } satisfies Partial<ApiError>);
  });
});
