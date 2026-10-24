// 代码沙箱页的接口封装。上传走原始请求体（后端不依赖 multipart）。
import { api, getCsrfToken, ApiError } from "../api";
import type {
  SandboxFileListing,
  SandboxPackages,
  SandboxRunDetail,
  SandboxRunResult,
  SandboxRunsPage,
  SandboxSettings,
  SandboxStats,
  SandboxStatus,
  SandboxUsage,
  SandboxWorkspace,
} from "./types";

const BASE = "/sandbox";

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "" || value === false) continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const ws = (workspace: string) => encodeURIComponent(workspace);

export const sandboxApi = {
  status: (refresh = false, signal?: AbortSignal) =>
    api<SandboxStatus>(`${BASE}/status${query({ refresh })}`, { signal }).then((r) => r.data),
  stats: (signal?: AbortSignal) => api<SandboxStats>(`${BASE}/stats`, { signal }).then((r) => r.data),
  packages: (signal?: AbortSignal) => api<SandboxPackages>(`${BASE}/packages`, { signal }).then((r) => r.data),
  runs: (
    filters: { workspace?: string; status?: string; q?: string; before?: number | null; limit?: number },
    signal?: AbortSignal,
  ) => api<SandboxRunsPage>(`${BASE}/runs${query(filters)}`, { signal }).then((r) => r.data),
  run: (runId: string, signal?: AbortSignal) =>
    api<SandboxRunDetail>(`${BASE}/runs/${encodeURIComponent(runId)}`, { signal }).then((r) => r.data),
  cancel: (runId: string) =>
    api<{ cancelled: boolean }>(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }).then((r) => r.data),
  workspaces: (signal?: AbortSignal) =>
    api<{ items: SandboxWorkspace[] }>(`${BASE}/workspaces`, { signal }).then((r) => r.data.items),
  files: (workspace: string, prefix = "", signal?: AbortSignal) =>
    api<SandboxFileListing>(`${BASE}/workspaces/${ws(workspace)}/files${query({ prefix })}`, { signal }).then((r) => r.data),
  preview: (workspace: string, path: string, signal?: AbortSignal) =>
    api<{ text: string; truncated: boolean; kind: string }>(
      `${BASE}/workspaces/${ws(workspace)}/file${query({ path, preview: 1 })}`,
      { signal },
    ).then((r) => r.data),
  deleteFile: (workspace: string, path: string) =>
    api<{ removed: number }>(`${BASE}/workspaces/${ws(workspace)}/file${query({ path })}`, { method: "DELETE" }).then((r) => r.data),
  resetWorkspace: (workspace: string, forgetHistory: boolean) =>
    api<{ removed: number; cancelled: number; historyRemoved: number }>(
      `${BASE}/workspaces/${ws(workspace)}${query({ forgetHistory })}`,
      { method: "DELETE" },
    ).then((r) => r.data),
  consoleRun: (body: { code: string; workspace: string; timeoutSeconds?: number | null }) =>
    api<SandboxRunResult>(`${BASE}/console/run`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data),
  settings: (signal?: AbortSignal) =>
    api<{ settings: SandboxSettings; defaults: SandboxSettings }>(`${BASE}/settings`, { signal }).then((r) => r.data),
  saveSettings: (settings: SandboxSettings) =>
    api<{ settings: SandboxSettings }>(`${BASE}/settings`, { method: "PUT", body: JSON.stringify(settings) }).then((r) => r.data.settings),
  usage: (signal?: AbortSignal) => api<SandboxUsage>(`${BASE}/usage`, { signal }).then((r) => r.data),
  prune: () =>
    api<{ at: number; removed: number; retentionDays: number }>(`${BASE}/maintenance/prune`, { method: "POST" }).then((r) => r.data),
};

/** 文件直链：图片/音频可内联，其余由后端强制作为附件下载。 */
export function sandboxFileUrl(workspace: string, path: string, download = false): string {
  return `/webui/api/v1${BASE}/workspaces/${ws(workspace)}/file${query({ path, download })}`;
}

/** 上传一个本地文件到工作区（原始请求体，最大 20MB）。 */
export async function uploadSandboxFile(workspace: string, path: string, file: Blob): Promise<void> {
  const send = () => fetch(`/webui/api/v1${BASE}/workspaces/${ws(workspace)}/upload${query({ path })}`, {
    method: "POST",
    body: file,
    credentials: "same-origin",
    headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": getCsrfToken() },
  });
  const response = await send();
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { error?: { message?: string } };
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    throw new ApiError(response.status, payload.error?.message ?? `上传失败 (${response.status})`);
  }
}
