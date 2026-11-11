// 表情包页的接口封装。上传走原始请求体（和代码沙箱上传一样，不依赖 multipart）。
import { api, ApiError, getCsrfToken } from "../api";
import type {
  ArchiveInfo,
  ImportResult,
  LabelScope,
  StickerBulkExtra,
  StickerHealth,
  StickerInsights,
  StickerPack,
  StickerResolve,
  StickerTrash,
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
  meta: { name: string; tags: string[]; description?: string; auto?: boolean; fallback?: string; pack?: string },
): Promise<UploadResult> {
  const params = new URLSearchParams({
    name: meta.name,
    tags: meta.tags.join(","),
    description: meta.description ?? "",
    auto: meta.auto === false ? "false" : "true",
    fallback: meta.fallback ?? "",
  });
  if (meta.pack) params.set("pack", meta.pack);
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
  /** 默认进回收站；purge 彻底删，block 彻底删并拉黑。 */
  remove: (id: string, how: "trash" | "purge" | "block" = "trash") =>
    api<{ deleted: string; trashed: boolean; blocked: boolean }>(
      `${BASE}/${encodeURIComponent(id)}${how === "trash" ? "" : `?${how}=true`}`,
      { method: "DELETE" },
    ).then((r) => r.data),
  bulk: (ids: string[], action: StickerBulkAction, extra: StickerBulkExtra = {}) =>
    api<{ done: string[]; missing: string[] }>(`${BASE}/bulk`, { method: "POST", body: JSON.stringify({ ids, action, ...extra }) }).then((r) => r.data),
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
  startJob: (body: { scope: LabelScope; ids?: string[]; mode: LabelMode; fields?: LabelField[]; retryOf?: string }) =>
    api<{ job: LabelJobSummary }>(`${BASE}/label-jobs`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data.job),
  cancelJob: (id: string) =>
    api<{ job: LabelJobSummary }>(`${BASE}/label-jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }).then((r) => r.data.job),
  promptPreview: (groupId: string, scene = "conversation") =>
    api<StickerPromptPreview>(`${BASE}/prompt-preview?groupId=${encodeURIComponent(groupId)}&scene=${encodeURIComponent(scene)}`).then((r) => r.data),
  resolve: (groupId: string, name: string) =>
    api<StickerResolve>(`${BASE}/resolve?groupId=${encodeURIComponent(groupId)}&name=${encodeURIComponent(name)}`).then((r) => r.data),
  insights: (days: number, signal?: AbortSignal) => api<StickerInsights>(`${BASE}/insights?days=${days}`, { signal }).then((r) => r.data),
  resolveMiss: (body: { name?: string; action: "alias" | "dismiss" | "clear"; stickerId?: string }) =>
    api<{ removed: number }>(`${BASE}/misses/resolve`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data),
  health: (signal?: AbortSignal) => api<StickerHealth>(`${BASE}/health`, { signal }).then((r) => r.data),
  fixHealth: (kind: string) =>
    api<{ kind: string; fixed: number; health: StickerHealth }>(`${BASE}/health/fix`, { method: "POST", body: JSON.stringify({ kind }) }).then((r) => r.data),
  createPack: (body: { name: string; description?: string; groups?: string[]; enabled?: boolean }) =>
    api<{ id: string; packs: StickerPack[] }>(`${BASE}/packs`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.data),
  updatePack: (id: string, body: { name?: string; description?: string; groups?: string[]; enabled?: boolean }) =>
    api<{ packs: StickerPack[] }>(`${BASE}/packs/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }).then((r) => r.data),
  deletePack: (id: string) => api<{ packs: StickerPack[] }>(`${BASE}/packs/${encodeURIComponent(id)}`, { method: "DELETE" }).then((r) => r.data),
  packMembers: (id: string, ids: string[], op: "add" | "remove") =>
    api<{ done: string[]; missing: string[] }>(`${BASE}/packs/${encodeURIComponent(id)}/members`, { method: "POST", body: JSON.stringify({ ids, op }) }).then((r) => r.data),
  trash: (signal?: AbortSignal) => api<StickerTrash>(`${BASE}/trash`, { signal }).then((r) => r.data),
  restore: (id: string) => api<{ sticker: Sticker }>(`${BASE}/trash/${encodeURIComponent(id)}/restore`, { method: "POST" }).then((r) => r.data.sticker),
  emptyTrash: () => api<{ removed: number }>(`${BASE}/trash/empty`, { method: "POST" }).then((r) => r.data),
  unblock: (digest: string) => api<{ blocked: string[] }>(`${BASE}/blocked/${encodeURIComponent(digest)}`, { method: "DELETE" }).then((r) => r.data),
  renameTag: (from: string, to: string) =>
    api<{ changed: number }>(`${BASE}/tags/rename`, { method: "POST", body: JSON.stringify({ from, to }) }).then((r) => r.data),
  deleteTag: (from: string) => api<{ changed: number }>(`${BASE}/tags/delete`, { method: "POST", body: JSON.stringify({ from }) }).then((r) => r.data),
};

/** 换一张图（名字、标签、统计都留着）。 */
export function replaceStickerImage(id: string, file: Blob): Promise<{ sticker: Sticker }> {
  return postRaw(`/${encodeURIComponent(id)}/replace`, new URLSearchParams(), file, "换图失败");
}

/** 导出成 zip；返回文件内容和里面有几张。 */
export async function exportStickers(body: { ids?: string[]; pack?: string; includePrivate?: boolean }): Promise<{ blob: Blob; count: number; skipped: number }> {
  const response = await fetch(`/webui/api/v1${BASE}/export`, {
    method: "POST",
    body: JSON.stringify(body),
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrfToken() },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { error?: { message?: string }; detail?: string };
    if (response.status === 401) window.dispatchEvent(new Event("yawnbot-auth-lost"));
    throw new ApiError(response.status, payload.error?.message ?? payload.detail ?? `导出失败 (${response.status})`);
  }
  return {
    blob: await response.blob(),
    count: Number(response.headers.get("X-Sticker-Count") ?? 0),
    skipped: Number(response.headers.get("X-Sticker-Skipped") ?? 0),
  };
}

/** 导入一个导出的包；apply=false 只看包里有什么。 */
export function importStickers(
  file: Blob,
  options: { apply: boolean; updateExisting?: boolean; asPending?: boolean; keepPrivate?: boolean },
): Promise<{ applied: boolean; info?: ArchiveInfo; result?: ImportResult }> {
  const params = new URLSearchParams({
    apply: String(options.apply),
    updateExisting: String(Boolean(options.updateExisting)),
    asPending: String(Boolean(options.asPending)),
    keepPrivate: String(Boolean(options.keepPrivate)),
  });
  return postRaw("/import", params, file, "导入失败");
}
