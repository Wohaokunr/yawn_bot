export interface Overview {
  bots: string[];
  plugins: { name: string; state: "loaded" | "missing" | "failed"; detail?: string | null }[];
  counts: { groups: number; users: number; enabledAgents: number };
  recentAgentActions: AgentAudit[];
  metrics: { counters: unknown[]; histograms: unknown[] };
  generatedAt: string;
}

export interface GroupSummary {
  groupId: string;
  groupName?: string | null;
  firstSeenAt: string;
  lastActiveAt?: string | null;
  memberCount: number;
  agentEnabled: boolean;
}

export interface UserSummary {
  userId: string;
  nickname?: string | null;
  firstInteractionAt: string;
  lastInteractionAt?: string | null;
  affinity: number;
}

export interface FeatureState {
  key: string;
  name: string;
  override: boolean | null;
  effective: boolean;
  source: "default" | "group" | "user" | "global_user";
}

export interface Member {
  userId: string;
  nickname?: string | null;
  groupNickname?: string | null;
  role: string;
  title?: string | null;
  lastSeenAt?: string | null;
  active: boolean;
}

export interface AgentConfig {
  groupId: string;
  enabled: boolean;
  triggerMode: string;
  proactiveProbability: number;
  proactiveActiveEnabled: boolean;
  proactiveActiveProbability: number;
  proactiveActiveWindowMinutes: number;
  idleThresholdMinutes: number;
  cooldownMinutes: number;
  dailyLimit: number;
  rawRetentionDays: number;
  mediaCacheEnabled: boolean;
  adminToolDailyLimit: number;
  toolAllowlist: string[];
  proactiveToday: number;
  adminToolsToday: number;
  version: string | null;
}

export interface Persona {
  groupId: string;
  enabled: boolean;
  resolved: Record<string, string>;
  overrides: Record<string, string>;
  fields: string[];
  version: string | null;
}

export interface MemoryItem {
  id: string;
  groupId: string;
  subjectUserId?: string | null;
  scope: string;
  type: string;
  key: string;
  content: string;
  salience: number;
  confidence: number;
  visibility: string;
  createdAt?: string | null;
  updatedAt: string;
  expiresAt?: string | null;
}

// 记忆治理状态(口径见后端 webui/service.py agent_memory_status)
export interface AgentMemoryStatus {
  groupId: string;
  pendingMessages: number;
  lastCompactedMessageId: number | null;
  lastCompactedAt: string | null;
  countsByType: Record<string, number>;
  total: number;
  oldestUpdatedAt: string | null;
  newestUpdatedAt: string | null;
}

export interface AgentRelationItem {
  id: string;
  groupId: string;
  subjectUserId: string;
  objectUserId: string;
  type: string;
  confidence: number;
  evidenceCount: number;
  lastSeenAt: string | null;
}

export interface AgentMessageItem {
  id: string;
  groupId: string;
  userId: string;
  senderName: string | null;
  role: string;
  title: string | null;
  text: string;
  receivedAt: string | null;
  expiresAt: string | null;
}

export interface PrivacyItem {
  groupId: string;
  userId: string;
  optedOut: boolean;
  updatedAt: string;
}

export interface AgentAudit {
  id: string;
  groupId: string;
  actorUserId?: string | null;
  toolName: string;
  arguments: Record<string, unknown>;
  result: string;
  detail?: string | null;
  createdAt: string;
}

export interface WebAudit {
  id: string;
  requestId: string;
  actorSession: string;
  action: string;
  resourceType: string;
  resourceId?: string | null;
  result: string;
  detail: Record<string, unknown>;
  createdAt: string;
}

// ── 对局中心:狼人杀 / 跑团(字段口径见后端 webui/games.py) ──

export interface WerewolfLivePlayer {
  seat: number;
  userId: number;
  name: string;
  isAi: boolean;
  alive: boolean;
  isSheriff: boolean;
  role: string | null;
  faction: string | null;
  deathRound: number | null;
  deathCause: string | null;
}

export interface WerewolfLiveGame {
  groupId: number;
  groupName?: string | null;
  hostUserId: number;
  board: string | null;
  phase: string | null;
  phaseLabel: string;
  roundNo: number;
  currentSpeaker: number | null;
  signupCount: number;
  playerCount: number;
  aiCount: number;
  aliveCount: number;
  queueDepth: number;
  pendingCount: number;
  workerAlive: boolean;
  players: WerewolfLivePlayer[];
  signup: { userId: number; name: string; isAi: boolean }[];
}

// 可视化对局事件(内存日志,管理员全可见;口径见 yawn_werewolf/game_log.py)
export interface WerewolfGameEvent {
  seq: number;
  ts: string;
  type: "phase" | "announce" | "death" | "speech" | "vote_tally" | "ai_decision" | "ai_speech" | "system" | string;
  roundNo: number | null;
  phase: string | null;
  seat: number | null;
  userId: number | null;
  name: string | null;
  text: string | null;
  extra: {
    instruction?: string;
    context?: string;
    action?: { kind: string; value: number | null } | null;
    attempt?: number;
    isAi?: boolean;
    scene?: string;
    role?: string;
    deathCause?: string;
    winner?: string;
    votes?: { voterSeat: number; targetSeat: number | null; isSheriff: boolean }[];
    counts?: Record<string, number>;
    [key: string]: unknown;
  };
}

export interface WerewolfGameDetail {
  game: WerewolfLiveGame;
  events: WerewolfGameEvent[];
}

export interface RpgLivePlayer {
  seat: number;
  userId: number;
  charName: string | null;
  confirmed: boolean;
  incapped: boolean;
}

export interface RpgLiveGame {
  groupId: number;
  groupName?: string | null;
  hostUserId: number;
  moduleId: string | null;
  moduleName: string | null;
  phase: string | null;
  phaseLabel: string;
  sceneId: string | null;
  clockText: string;
  exploreRound: number;
  combatRound: number | null;
  currentActorUserId: number | null;
  signupCount: number;
  playerCount: number;
  queueDepth: number;
  pendingCount: number;
  toolsBroken: number;
  workerAlive: boolean;
  players: RpgLivePlayer[];
}

export interface LiveGames {
  werewolf: { available: boolean; games: WerewolfLiveGame[] };
  rpg: { available: boolean; games: RpgLiveGame[] };
}

export interface WerewolfHistoryPlayer {
  seat: number;
  userId: number;
  isAi: boolean;
  role: string;
  faction: string;
  isWinner: boolean | null;
  isSheriff: boolean;
  deathRound: number | null;
  deathCause: string | null;
}

export interface WerewolfHistoryGame {
  id: number;
  groupId: number;
  groupName?: string | null;
  hostUserId: number;
  board: string | null;
  playerCount: number;
  startedAt: string | null;
  endedAt: string | null;
  winnerFaction: string | null;
  endRound: number | null;
  status: "running" | "finished";
  players: WerewolfHistoryPlayer[];
}

export interface RpgHistoryPlayer {
  userId: number;
  charName: string;
  startHp: number;
  startSan: number;
  finalHp: number | null;
  finalSan: number | null;
  isIncapped: boolean;
  survived: boolean | null;
}

export interface RpgHistoryGame {
  id: number;
  groupId: number;
  groupName?: string | null;
  hostUserId: number;
  moduleId: string;
  moduleName: string;
  playerCount: number;
  startedAt: string | null;
  endedAt: string | null;
  endingId: string | null;
  outcome: string | null;
  terminationReason: string | null;
  status: "running" | "finished";
  players: RpgHistoryPlayer[];
}

// ── 番茄小说(字段口径见后端 webui/fanqie.py) ──

export interface FanqieStatus {
  available: boolean;
  limits: {
    maxChapters: number;
    userActiveMax: number;
    groupActiveMax: number;
    queueMax: number;
    searchLimit: number;
    rankLimit: number;
    fileRetentionHours: number;
  } | null;
  active: { queued: number; running: number } | null;
}

export interface FanqieBookSummary {
  bookId: string;
  title: string;
  author: string;
  description: string;
  url: string;
  rank: number | null;
  readCount: number | null;
  wordCount: number | null;
}

export interface FanqieChapterRef {
  index: number;
  itemId: string;
  title: string;
  isLocked: boolean;
}

export interface FanqieRankCategoryGroup {
  gender: string;
  categories: { categoryId: string; name: string }[];
}

export type FanqieJobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface FanqieJob {
  id: number;
  bookId: string | null;
  title: string | null;
  author: string | null;
  requesterUserId: string;
  groupId: string | null;
  groupName: string | null;
  startChapter: number;
  endChapter: number;
  totalChapters: number;
  completedChapters: number;
  status: FanqieJobStatus;
  cancelRequested: boolean;
  outputName: string | null;
  sendStatus: string;
  lastError: string | null;
  sendError: string | null;
  createdAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
}

export interface FanqieJobChapter {
  chapterIndex: number;
  itemId: string;
  title: string;
  isLocked: boolean;
  status: string;
  lastError: string | null;
  completedAt: string | null;
}

export interface FanqieJobDetail {
  job: FanqieJob;
  chapters: FanqieJobChapter[];
}

