// 网页搜索页的接口封装。
import { api } from "../api";
import type {
  WebFetchTestResult,
  WebSearchRecentCall,
  WebSearchSettings,
  WebSearchStatus,
  WebSearchTestResult,
} from "./types";

const BASE = "/web-search";

export const webSearchApi = {
  status: (signal?: AbortSignal) => api<WebSearchStatus>(`${BASE}/status`, { signal }).then((r) => r.data),
  saveSettings: (settings: WebSearchSettings) =>
    api<{ settings: WebSearchSettings }>(`${BASE}/settings`, { method: "PUT", body: JSON.stringify(settings) }).then((r) => r.data.settings),
  recent: (groupId: string | null, signal?: AbortSignal) =>
    api<{ items: WebSearchRecentCall[] }>(`${BASE}/recent?limit=200${groupId ? `&groupId=${encodeURIComponent(groupId)}` : ""}`, { signal }).then((r) => r.data.items),
  test: (body: { query: string; count: number; site?: string; lang?: string; after?: string }) =>
    api<WebSearchTestResult>(`${BASE}/test`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data),
  fetchTest: (url: string) =>
    api<WebFetchTestResult>(`${BASE}/fetch-test`, { method: "POST", body: JSON.stringify({ url }) }).then((r) => r.data),
  clearCache: () => api<{ cleared: number }>(`${BASE}/cache/clear`, { method: "POST" }).then((r) => r.data),
};
