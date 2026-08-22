import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, getCsrfToken, setCsrfToken } from "./api";

describe("api client", () => {
  afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(""); });

  it("adds the CSRF header to mutations", async () => {
    setCsrfToken("csrf-value");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {}, meta: {} }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/groups/1/features/rpg", { method: "PATCH", body: "{}" });
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect((request.headers as Headers).get("X-CSRF-Token")).toBe("csrf-value");
    expect(getCsrfToken()).toBe("csrf-value");
  });

  it("preserves conflict status and message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: "配置已更新", fields: {} } }), { status: 409, headers: { "Content-Type": "application/json" } })));
    await expect(api("/agent/groups/1/config")).rejects.toMatchObject({ status: 409, message: "配置已更新" } satisfies Partial<ApiError>);
  });
});
