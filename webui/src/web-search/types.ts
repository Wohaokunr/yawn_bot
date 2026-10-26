// 「网页搜索」页的接口类型，与后端 webui/web_search.py 的 camelCase 输出一致。

export type WebSearchState = "ready" | "no_key" | "paused";

export interface WebSearchSettings {
  enabled: boolean;
  groupMode: "all" | "allowlist";
  allowedGroups: string[];
  blockedGroups: string[];
  adminOnly: boolean;
  memberDailyCalls: number;
  groupDailyCalls: number;
  globalDailyCalls: number;
  memberCooldownSeconds: number;
  maxCallsPerTurn: number;
  defaultCount: number;
  maxCount: number;
  allowFullContent: boolean;
  fetchEnabled: boolean;
  maxFetchChars: number;
  cacheTtlMinutes: number;
  blockedDomains: string[];
}

export interface WebSearchUsageRow {
  groupId: string;
  userId?: string;
  calls: number;
  groupName?: string | null;
}

export interface WebSearchUsage {
  day: string | null;
  totals: Record<string, number>;
  billed: number;
  costYuan: number;
  pricePerCallYuan: number;
  globalLimit: number;
  globalLeft: number;
  groups: WebSearchUsageRow[];
  members: WebSearchUsageRow[];
  groupLimit: number;
  memberLimit: number;
}

export interface WebSearchCacheStats {
  size: number;
  hits: number;
  misses: number;
}

export interface WebSearchStatus {
  state: WebSearchState;
  configured: boolean;
  keySource: string | null;
  keyMasked: string | null;
  baseUrl: string;
  settings: WebSearchSettings;
  defaults: WebSearchSettings;
  usage: WebSearchUsage;
  cache: { search: WebSearchCacheStats; fetch: WebSearchCacheStats };
  upstream: {
    calls: number;
    failures: number;
    avgMs: number;
    lastError: string;
    lastErrorAt: number | null;
    lastOkAt: number | null;
    byStatus: Record<string, number>;
  };
}

export interface WebSearchRecentCall {
  at: number;
  groupId: string | null;
  groupName?: string | null;
  userId: string | null;
  tool: "web_search" | "web_fetch";
  query?: string | null;
  host?: string | null;
  status: "ok" | "empty" | "cached" | "error";
  error?: string | null;
  results?: number | null;
  chars?: number | null;
  ms?: number | null;
}

export interface WebSearchTestHit {
  title: string;
  url: string;
  site: string;
  date: string | null;
  summary: string;
  score: number | null;
  authority: number | null;
}

export interface WebSearchTestResult {
  query: string;
  durationMs: number;
  attempts: number;
  billed: boolean;
  results: WebSearchTestHit[];
}

export interface WebFetchTestResult {
  url: string;
  title: string | null;
  chars: number;
  durationMs: number;
  preview: string;
  truncated: boolean;
}
