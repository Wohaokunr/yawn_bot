import { api } from "../api";
import type { WwGameDetail, WwHistoryGame, WwMeta, WwReplay, WwStats } from "./types";

export interface HistoryQuery {
  page: number;
  pageSize: number;
  search: string;
  status: string;
  board: string;
  winner: string;
  groupId: number | null;
  userId: number | null;
}

function qs(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const wwApi = {
  meta: () => api<WwMeta>("/games/werewolf/meta").then((r) => r.data),
  events: (groupId: number, afterSeq = 0) => api<WwGameDetail>(`/games/werewolf/${groupId}/events${qs({ after_seq: afterSeq || null })}`).then((r) => r.data),
  stop: (groupId: number) => api(`/games/werewolf/${groupId}/stop`, { method: "POST" }),
  history: (query: HistoryQuery) => api<WwHistoryGame[]>(`/games/werewolf/history${qs({
    page: query.page,
    pageSize: query.pageSize,
    search: query.search,
    status: query.status === "all" ? null : query.status,
    board: query.board,
    winner: query.winner,
    groupId: query.groupId,
    userId: query.userId,
  })}`).then((r) => ({ rows: r.data, total: Number(r.meta.total ?? 0) })),
  historyGame: (rowId: number) => api<WwHistoryGame>(`/games/werewolf/history/${rowId}`).then((r) => r.data),
  stats: (days: number, board: string) => api<WwStats>(`/games/werewolf/stats${qs({ days: days || null, board })}`).then((r) => r.data),
  replay: (replayId: string) => api<WwReplay>(`/games/werewolf/replay/${encodeURIComponent(replayId)}`).then((r) => r.data),
};
