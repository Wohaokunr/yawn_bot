// 狼人杀管理台 2.0 的接口类型。实时快照沿用 ../types 的 WerewolfLiveGame，
// 这里补上后端新增的可选字段（旧后端缺字段时前端一律按“未知”兜底）。
import type { WerewolfGameEvent, WerewolfHistoryGame, WerewolfHistoryPlayer, WerewolfLiveGame, WerewolfLivePlayer } from "../types";

export type WwEvent = WerewolfGameEvent;

export interface WwPlayer extends WerewolfLivePlayer {
  dmOk?: boolean;
  canVote?: boolean;
  idiotRevealed?: boolean;
  sheriffCandidate?: boolean;
  ownerSeat?: number | null;
  saveUsed?: boolean;
  poisonUsed?: boolean;
  silenced?: boolean;
  requestedRole?: string | null;
  pendingAction?: string | null;
}

export interface WwGame extends Omit<WerewolfLiveGame, "players" | "signup"> {
  players: WwPlayer[];
  signup: { userId: number; name: string; isAi: boolean; requestedRole?: string | null }[];
  phaseToken?: number;
  silencedSeat?: number | null;
  sheriffCampaignRound?: number | null;
  voteTargets?: number[];
  voteExclude?: number[];
  eventLogId?: string | null;
  phaseRemaining?: number | null;
  phaseStartedAt?: string | null;
  rules?: Record<string, string | number | boolean | null> | null;
}

export interface WwGameDetail {
  game: WwGame;
  events: WwEvent[];
}

export interface WwBoardRole {
  role: string;
  count: number;
  faction: string;
  isGod: boolean;
}

export interface WwBoard {
  key: string;
  silenceMode: "speech" | "vote" | null;
  isDefault: boolean;
  description?: string | null;
  counts: { players: number; wolves: number; gods: number; villagers: number; roles: WwBoardRole[] }[];
}

export interface WwRoleInfo {
  role: string;
  faction: string;
  isGod: boolean;
  skill: string;
  voteVariantSkill: string | null;
}

export interface WwCommand {
  name: string;
  aliases: string[];
  description: string;
  scope: string | null;
  permission: string | null;
  level: string | null;
}

export interface WwMeta {
  defaultBoard: string | null;
  boards: WwBoard[];
  roles: WwRoleInfo[];
  config: Record<string, string | number | boolean | null>;
  phaseTimeouts: Record<string, number>;
  sheriffFinalSpeechSeconds: number;
  commands?: WwCommand[];
}

export interface WwRecord { seats: number; wins: number; winRate: number | null }

export interface WwStats {
  totals: {
    games: number;
    finished: number;
    unfinished: number;
    wolfWins: number;
    goodWins: number;
    wolfWinRate: number | null;
    avgRounds: number | null;
    avgPlayers: number | null;
    aiSeatShare: number | null;
    humanPlayers: number;
    groups: number;
  };
  boards: { board: string; games: number; finished: number; wolfWins: number; goodWins: number; avgRounds: number | null }[];
  roles: { role: string; faction: string; seats: number; wins: number; survived: number; sheriff: number; winRate: number; survivalRate: number }[];
  deathCauses: { cause: string; count: number }[];
  deathRounds: { round: number; count: number }[];
  rounds: { round: number; count: number }[];
  aiVsHuman: { ai: WwRecord; human: WwRecord };
  daily: { date: string; games: number; wolfWins: number; goodWins: number; unfinished: number }[];
  hours: { hour: number; count: number }[];
  groups: { groupId: number; groupName: string | null; games: number }[];
  leaderboard: { userId: number; name: string | null; games: number; wins: number; winRate: number; survived: number; sheriff: number; wolfGames: number; favoriteRole: string | null }[];
  truncated: boolean;
  window: { days: number; board: string | null; groupId: number | null };
}

export interface WwHistoryPlayer extends WerewolfHistoryPlayer {
  name?: string | null;
}

export interface WwHistoryGame extends Omit<WerewolfHistoryGame, "players"> {
  hostName?: string | null;
  players: WwHistoryPlayer[];
}

export interface WwReplay {
  game_id: string;
  available: boolean;
  reason: string | null;
  title: string;
  started_at: string | null;
  ended_at: string | null;
  summary: Record<string, unknown>;
  events: { sequence: number; occurred_at: string; event_type: string; phase: string | null; round: number | null; detail: string; audience: string }[];
  warnings: string[];
}
