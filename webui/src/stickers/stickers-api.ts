// 表情包页的接口封装。上传走原始请求体（和代码沙箱上传一样，不依赖 multipart）。
import { api, ApiError, getCsrfToken } from "../api";
import type {
  Sticker,
  StickerBulkAction,
  StickerOverview,
  StickerPatch,
  StickerPromptPreview,
  StickerSettings,
} from "./types";

const BASE = "/stickers";

export function stickerImageUrl(sticker: Pick<Sticker, "id" | "updatedAt">): string {
  const version = sticker.updatedAt ? `?v=${encodeURIComponent(sticker.updatedAt)}` : "";
  return `/webui/api/v1${BASE}/${encodeURIComponent(sticker.id)}/image${version}`;
}

export async function uploadSticker(
  file: Blob,
  meta: { name: string; tags: string[]; description?: string },
): Promise<{ sticker: Sticker; created: boolean }> {
  const params = new URLSearchParams({ name: meta.name, tags: meta.tags.join(","), description: meta.description ?? "" });
  const response = await fetch(`/webui/api/v1${BASE}/upload?${params.toString()}`, {
    method: "POST",
    body: file,
    credentials: "same-origin",
    headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": getCsrfToken() },
  });
  const payload = (await response.json().catch(() => ({}))) as { data?: { sticker: Sticker; created: boolean }; error?: { message?: string } };
  if (!response.ok || !payload.data) {
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    throw new ApiError(response.status, payload.error?.message ?? `上传失败 (${response.status})`);
  }
  return payload.data;
}

export const stickersApi = {
  overview: (signal?: AbortSignal) => api<StickerOverview>(BASE, { signal }).then((r) => r.data),
  update: (id: string, patch: StickerPatch) =>
    api<{ sticker: Sticker }>(`${BASE}/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) }).then((r) => r.data.sticker),
  remove: (id: string) => api<{ deleted: string }>(`${BASE}/${encodeURIComponent(id)}`, { method: "DELETE" }).then((r) => r.data),
  bulk: (ids: string[], action: StickerBulkAction) =>
    api<{ done: string[]; missing: string[] }>(`${BASE}/bulk`, { method: "POST", body: JSON.stringify({ ids, action }) }).then((r) => r.data),
  saveSettings: (settings: StickerSettings) =>
    api<{ settings: StickerSettings }>(`${BASE}/settings`, { method: "PUT", body: JSON.stringify(settings) }).then((r) => r.data.settings),
  promptPreview: (groupId: string) =>
    api<StickerPromptPreview>(`${BASE}/prompt-preview?groupId=${encodeURIComponent(groupId)}`).then((r) => r.data),
};
