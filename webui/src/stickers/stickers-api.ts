// 表情包页的接口封装。上传走原始请求体（和代码沙箱上传一样，不依赖 multipart）。
import { api, ApiError, getCsrfToken } from "../api";
import type {
  LabelField,
  LabelJobDetail,
  LabelJobSummary,
  LabelMode,
  Sticker,
  StickerBulkAction,
  StickerLabel,
  StickerOverview,
  StickerPatch,
  StickerPromptPreview,
  StickerSettings,
  UploadResult,
  VisionStatus,
} from "./types";

const BASE = "/stickers";

export function stickerImageUrl(sticker: Pick<Sticker, "id" | "updatedAt">): string {
  const version = sticker.updatedAt ? `?v=${encodeURIComponent(sticker.updatedAt)}` : "";
  return `/webui/api/v1${BASE}/${encodeURIComponent(sticker.id)}/image${version}`;
}

async function postRaw<T>(path: string, params: URLSearchParams, file: Blob, fallback: string): Promise<T> {
  const response = await fetch(`/webui/api/v1${BASE}${path}?${params.toString()}`, {
    method: "POST",
    body: file,
    credentials: "same-origin",
    headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": getCsrfToken() },
  });
  const payload = (await response.json().catch(() => ({}))) as { data?: T; error?: { message?: string } };
  if (!response.ok || !payload.data) {
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    throw new ApiError(response.status, payload.error?.message ?? `${fallback} (${response.status})`);
  }
  return payload.data;
}

/** 上传一张；名字/标签留空且 auto=true 时由识图模型补，识图不可用时名字用 fallback。 */
export function uploadSticker(
  file: Blob,
  meta: { name: string; tags: string[]; description?: string; auto?: boolean; fallback?: string },
): Promise<UploadResult> {
  const params = new URLSearchParams({
    name: meta.name,
    tags: meta.tags.join(","),
    description: meta.description ?? "",
    auto: meta.auto === false ? "false" : "true",
    fallback: meta.fallback ?? "",
  });
  return postRaw<UploadResult>("/upload", params, file, "上传失败");
}

/** 上传前预览：识图模型会怎么起名打标签（不入库）；库里已有同一张图时带 duplicate。 */
export function previewLabel(file: Blob, hint = ""): Promise<{ label: StickerLabel; duplicate: { id: string; name: string } | null }> {
  return postRaw("/label-preview", new URLSearchParams({ hint: hint.slice(0, 60) }), file, "识别失败");
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
  label: (id: string, body: { apply?: boolean; mode?: LabelMode; fields?: LabelField[] }) =>
    api<{ label?: StickerLabel; changes?: Partial<Pick<Sticker, "name" | "tags" | "description">>; sticker?: Sticker; result?: unknown }>(
      `${BASE}/${encodeURIComponent(id)}/label`,
      { method: "POST", body: JSON.stringify(body) },
    ).then((r) => r.data),
  undoLabel: (id: string) =>
    api<{ sticker: Sticker }>(`${BASE}/${encodeURIComponent(id)}/undo-label`, { method: "POST" }).then((r) => r.data.sticker),
  jobs: () => api<{ jobs: LabelJobSummary[]; vision: VisionStatus }>(`${BASE}/label-jobs`).then((r) => r.data),
  job: (id: string) => api<{ job: LabelJobDetail }>(`${BASE}/label-jobs/${encodeURIComponent(id)}`).then((r) => r.data.job),
  startJob: (body: { scope: "selected" | "unlabeled" | "all"; ids?: string[]; mode: LabelMode; fields?: LabelField[] }) =>
    api<{ job: LabelJobSummary }>(`${BASE}/label-jobs`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data.job),
  cancelJob: (id: string) =>
    api<{ job: LabelJobSummary }>(`${BASE}/label-jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }).then((r) => r.data.job),
  promptPreview: (groupId: string) =>
    api<StickerPromptPreview>(`${BASE}/prompt-preview?groupId=${encodeURIComponent(groupId)}`).then((r) => r.data),
};
