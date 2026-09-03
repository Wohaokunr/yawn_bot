export interface Overview {
  bots: string[];
  plugins: { name: string; state: "loaded" | "missing" | "failed"; detail?: string | null }[];
  counts: { groups: number; users: number; enabledAgents: number };
  recentAgentActions: AgentAudit[];
  metrics: { counters: unknown[]; histograms: unknown[] };
  stats: OverviewStats;
  generatedAt: string;
}

export interface OverviewStats {
  ai: {
    requestsTotal: number;
    success: number;
    failed: number;
    successRate: number | null;
    byOutcome: { outcome: string; count: number }[];
    avgDurationMs: number | null;
    p95DurationMs: number | null;
    degradations: number;
    health: {
      operation: string;
      consecutiveFailures: number;
      lastFailureOutcome: string | null;
    }[];
  };
  llm: {
    routes: LLMRouteStatus[];
    unconfiguredProviders: string[];
  };
  activity: {
    messages24h: number;
    activeGroups24h: number;
    agentResponseGroups24h: number;
    proactiveToday: number;
    adminToolToday: number;
  };
  memory: {
    compactingGroups: number;
    rebuildRequired: number;
    failingGroups: number;
    recentError: { groupId: string; error: string; at: string | null } | null;
  };
  games: {
    live: { rpg: LiveGameCount; werewolf: LiveGameCount };
    endedToday: { rpg: number | null; werewolf: number | null };
  };
  jobs: {
    fanqie: { available: boolean; byStatus: Record<string, number> };
    reminderErrors: number;
  };
  uptime: { startedAt: string; uptimeSeconds: number };
}

export interface LiveGameCount {
  available: boolean;
  count: number;
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
  replyTriggerEnabled: boolean;
  explicitWakeupEnabled: boolean;
  proactiveEnabled: boolean;
  proactiveProbability: number;
  proactiveActiveEnabled: boolean;
  shortConversationEnabled: boolean;
  proactiveActiveProbability: number;
  proactiveActiveWindowMinutes: number;
  idleThresholdMinutes: number;
  cooldownMinutes: number;
  dailyLimit: number;
  rawRetentionDays: number;
  crossGroupVisibility: "isolated" | "public_summary";
  mediaCacheEnabled: boolean;
  adminToolDailyLimit: number;
  criticalToolDailyLimit: number;
  toolAllowlist: string[];
  proactiveToday: number;
  adminToolsToday: number;
  criticalToolsToday: number;
  version: string | null;
}

export interface LLMRouteStatus {
  task: string;
  profile: string;
  provider: string;
  model: string;
  thinking: string;
  multimodal: string;
  configured: boolean;
}

export interface AgentDiagnosticBlocker {
  code: string;
  severity: "error" | "warning" | "info";
  title: string;
  detail: string;
}

export interface AgentDiagnostics {
  groupId: string;
  effective: {
    enabled: boolean;
    replyTriggerEnabled: boolean;
    explicitWakeupEnabled: boolean;
    proactiveEnabled: boolean;
    proactiveActiveEnabled: boolean;
    proactiveToday: number;
    dailyLimit: number;
    dailyRemaining: number;
    cooldownMinutes: number;
    cooldownRemainingMinutes: number;
    lastAgentAt: string | null;
    lastProactiveAt: string | null;
    activeTopic: string | null;
    mediaCacheEnabled: boolean;
    shortConversation: {
      enabled: boolean;
      active: boolean;
      sessionId: number | null;
      topic: string | null;
      botTurns: number;
      evaluations: number;
      consecutiveWaits: number;
    };
  };
  memory: AgentMemoryStatus;
  llm: {
    routes: LLMRouteStatus[];
    unconfiguredProviders: string[];
  };
  blockers: AgentDiagnosticBlocker[];
  generatedAt: string;
}

export interface AgentCapabilities {
  botId: string | null;
  groupId: string;
  offline: boolean;
  action: {
    cached: boolean;
    role: string | null;
    canManage: boolean;
    actions: string[];
    degraded: boolean;
    lastError: string | null;
    probedAt: number | null;
    cacheRemainingSeconds: number;
  };
  segments: Array<{
    type: string;
    allowed: boolean;
    supported: boolean;
    exposed: boolean;
    forbidden: boolean;
    runtimeUnsupported: boolean;
    lastFailureReason: string | null;
    retryAfterSeconds: number | null;
  }>;
}

export interface PersonaBehavior {
  source: string;
  sociability: number;
  followupTendency: number;
  reactionTendency: number;
  warmupProbabilityScale: number;
  activeProbabilityScale: number;
  maxFollowupBotTurns: number;
  allowSpontaneousReaction: boolean;
  reactionMode: "off" | "restrained" | "normal" | "expressive" | "high" | string;
}

export interface PersonaEmotion {
  schemaVersion: number;
  label: "neutral" | "warm" | "amused" | "curious" | "concerned" | "guarded" | "irritated" | string;
  displayLabel: string;
  valence: number;
  arousal: number;
  intensity: number;
  expressionIntensity: number;
  expressionHint: string;
  source: string;
  reason: string;
  updatedAt: string | null;
  ageMinutesBucket: number;
  eventCount: number;
}

export interface Persona {
  groupId: string;
  enabled: boolean;
  schemaVersion: number;
  profile: PersonaProfile;
  presets: PersonaPreset[];
  summary: string;
  behavior: PersonaBehavior;
  emotion: PersonaEmotion;
  resolved: Record<string, string>;
  version: string | null;
}

export interface PersonaProfile {
  presetId: string;
  name: string;
  identity: string;
  groupRole: string;
  warmth: number;
  humor: number;
  directness: number;
  verbosity: number;
  expressiveness: number;
  sociability: number;
  followupTendency: number;
  reactionTendency: number;
  customNotes: string;
}

export interface PersonaPreset {
  id: string;
  label: string;
  description: string;
  identity: string;
  groupRole: string;
  warmth: number;
  humor: number;
  directness: number;
  verbosity: number;
  expressiveness: number;
  sociability: number;
  followupTendency: number;
  reactionTendency: number;
}

export interface MemoryItem {
  id: string;
  groupId: string;
  subjectUserId?: string | null;
  scope: string;
  type: string;
  key: string;
  content: string;
  sourceKind: "auto" | "manual";
  evidenceMessageIds: string[];
  provenance: {
    kind: string;
    evidenceCount: number;
    firstObservedAt: string | null;
    lastConfirmedAt: string | null;
  };
  relatedUserIds: string[];
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
  runtimeEnabled: boolean;
  pendingMessages: number;
  lastCompactedMessageId: number | null;
  lastCompactedAt: string | null;
  countsByType: Record<string, number>;
  total: number;
  oldestUpdatedAt: string | null;
  newestUpdatedAt: string | null;
  rebuildRequired: boolean;
  lastAttemptAt: string | null;
  lastSuccessAt: string | null;
  lastError: string | null;
  consecutiveFailures: number;
  inFlight: boolean;
}

// 有画像沉淀的成员概览（口径见后端 app.py get_memory_subjects）
export interface MemorySubjectItem {
  userId: string;
  nickname: string;
  groupNickname: string | null;
  counts: { profile: number; core: number; manual: number };
  total: number;
  updatedAt: string;
}

export interface AgentRelationItem {
  id: string;
  groupId: string;
  subjectUserId: string;
  objectUserId: string;
  type: string;
  sourceKind: string;
  note: string;
  confidence: number;
  evidenceCount: number;
  lastSeenAt: string | null;
}

// 关系图谱节点：linked 表示该成员出现在关系边中，degree 为相关边数。
export interface AgentRelationGraphNode {
  userId: string;
  nickname: string;
  groupNickname: string | null;
  role: string;
  linked: boolean;
  degree: number;
}

export interface AgentRelationGraph {
  nodes: AgentRelationGraphNode[];
  edges: AgentRelationItem[];
  meta: { relationTruncated: boolean; memberTruncated: boolean };
}

export interface AgentMessageItem {
  id: string;
  messageId: string;
  groupId: string;
  userId: string;
  senderName: string | null;
  role: string;
  title: string | null;
  text: string;
  receivedAt: string | null;
  expiresAt: string | null;
}

export type AgentDebugMode = "dialogue" | "active" | "warmup" | "followup";

export interface AgentExecutionTraceEvent {
  id: string;
  phase: string;
  label: string;
  status: "planned" | "success" | "failed" | "degraded" | "unknown" | "skipped" | string;
  offsetMs: number;
  durationMs: number | null;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  detail: string | null;
  round: number | null;
}

export interface AgentExecutionTraceSummary {
  traceId: string;
  groupId: string;
  mode: string;
  source: "debug" | "runtime" | string;
  triggerSource: string | null;
  actorUserId: string | null;
  messageId: string | null;
  startedAt: string;
  status: string;
  outcome: string | null;
  durationMs: number | null;
  eventCount: number;
}

export interface AgentExecutionTrace {
  traceId: string;
  groupId: string;
  mode: string;
  source: "debug" | "runtime" | string;
  triggerSource: string | null;
  actorUserId: string | null;
  messageId: string | null;
  startedAt: string;
  status: string;
  outcome: string | null;
  durationMs: number | null;
  events: AgentExecutionTraceEvent[];
}

export interface AgentDebugResponse {
  promptVersion: string;
  mode: AgentDebugMode;
  persona: {
    source: "draft" | "persisted";
    persistedSummary: string;
    persistedProfile: PersonaProfile;
    appliedProfile: PersonaProfile;
    persistedBehavior: PersonaBehavior;
    appliedBehavior: PersonaBehavior;
    persistedEmotion: PersonaEmotion;
    appliedEmotion: PersonaEmotion;
  };
  currentTurn: Record<string, unknown>;
  context: {
    messages?: Array<Record<string, unknown>>;
    members?: Array<Record<string, unknown>>;
    memories?: Array<Record<string, unknown>>;
    relations?: string[];
    [key: string]: unknown;
  };
  contextSelection: Array<{
    message_id: string | number | null;
    user_id: string | number | null;
    name: string | null;
    role: string;
    title: string | null;
    text: string;
    text_truncated: boolean;
    minutes_ago: number;
    selected: boolean;
    reason: string;
    score?: number;
  }>;
  contextBudget: Array<Record<string, unknown>>;
  promptMessages: Array<{ role: string; content: unknown }>;
  tools: Array<Record<string, unknown>>;
  toolPermissions: Array<{
    name: string;
    permissionLevel:
      | "read"
      | "state_write"
      | "message_send"
      | "privileged"
      | "critical";
    exposed: boolean;
    reason: string;
    actions: string[];
  }>;
  route: {
    task: string;
    profile: string;
    provider: string;
    model: string;
    thinking: string;
    multimodal: string;
    configured: boolean;
  };
  stats: Record<string, unknown>;
  warnings: string[];
  executionTrace: AgentExecutionTrace;
  result: null | {
    outcome: string;
    text: string;
    toolCalls: Array<{ name: string; arguments: unknown }>;
    finishReason: string | null;
    usage: {
      promptTokens: number | null;
      completionTokens: number | null;
      cachedTokens: number | null;
      cacheMissTokens: number | null;
    };
    durationMs: number;
    decision?: Record<string, unknown>;
  };
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

export type EnvironmentValueKind = "string" | "boolean" | "integer" | "number" | "json" | "enum";
export type EnvironmentValueSource = "process" | "environment" | "env" | "default";

export interface EnvironmentEntry {
  key: string;
  section: string;
  description: string;
  value: string | null;
  defaultValue: string | null;
  configured: boolean;
  effectiveConfigured: boolean;
  secret: boolean;
  kind: EnvironmentValueKind;
  options: string[];
  source: EnvironmentValueSource;
  overridden: boolean;
}

export interface LLMProviderSnapshot {
  id: string;
  baseUrl: string;
  builtIn: boolean;
  apiKeyConfigured: boolean;
  apiKeyRootConfigured: boolean;
  baseUrlSource: EnvironmentValueSource;
  apiKeySource: EnvironmentValueSource;
  overridden: boolean;
}

export interface EnvironmentSnapshot {
  file: string;
  version: string;
  environment: string;
  environmentFile: string | null;
  entries: EnvironmentEntry[];
  llmProviders: LLMProviderSnapshot[];
}

export interface EnvironmentPatchResult {
  version: string;
  restartRequired: boolean;
  updatedKeys: string[];
}

export interface LLMConnectionTestResult {
  success: true;
  latencyMs: number;
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

export interface RpgDetailPlayer extends RpgLivePlayer {
  hp: number;
  san: number;
  rerollsLeft: number;
  dmOk: boolean;
}

export interface RpgPlayerPrivate {
  userId: number;
  situationText: string;
  journalText: string;
}

export interface RpgPendingDeduction {
  proposerUserId: number;
  clueIds: string[];
  conclusion: string;
  confirmations: number[];
}

export interface RpgGameDetail {
  game: RpgLiveGame;
  players: RpgDetailPlayer[];
  situationText: string;
  clueBoardText: string;
  groupLog: string[];
  signupUserIds: number[];
  pendingDeduction: RpgPendingDeduction | null;
  completedDeductions: string[];
}

export type RpgActionKind = "SAY" | "WAIT" | "PASS_TURN" | "MODULE_SELECT" | "START_GAME";

export interface RpgReplayEvent {
  sequence: number;
  occurred_at: string;
  event_type: string;
  phase: string | null;
  round: number | null;
  actor_seat: number | null;
  detail: string;
  audience: "public" | "personal";
}

export interface RpgReplay {
  game_id: string;
  game_kind: "rpg" | null;
  view: "public" | "personal";
  viewer_seat: number | null;
  available: boolean;
  reason: string | null;
  title: string;
  started_at: string | null;
  ended_at: string | null;
  summary: Record<string, string>;
  events: RpgReplayEvent[];
  warnings: string[];
}

export interface RpgModuleSummary {
  id: string;
  name: string;
  description: string;
  difficulty: string;
  minPlayers: number;
  maxPlayers: number;
  startScene: string;
  sceneCount: number;
  npcCount: number;
  monsterCount: number;
  clueCount: number;
  deductionCount: number;
  endingCount: number;
  eventCount: number;
  health: RpgModuleHealth;
}

export interface RpgModuleLintIssue {
  severity: "ERROR" | "WARNING" | "INFO";
  section: string;
  path: string;
  message: string;
  hint: string;
}

export interface RpgModuleHealth {
  status: "healthy" | "warning" | "error" | "schema-only";
  schemaValidated: boolean;
  lintAvailable: boolean;
  errorCount: number;
  warningCount: number;
  infoCount: number;
  issues: RpgModuleLintIssue[];
}

export interface RpgModuleDetail extends RpgModuleSummary {
  opening: string;
  genericEndings: boolean;
  time: { start: string; costs: Record<string, number> };
  scenes: {
    id: string;
    name: string;
    narration: string;
    idleNarration: string | null;
    npcs: string[];
    monsters: string[];
    checks: Record<string, unknown>[];
    exits: Record<string, unknown>[];
  }[];
  npcs: {
    id: string;
    name: string;
    publicDesc: string;
    persona: string;
    knows: string[];
    secrets: string[];
    facts: { id: string; name: string; text: string }[];
    socialNodes: Record<string, unknown>[];
    schedule: Record<string, unknown>[];
    [key: string]: unknown;
  }[];
  monsters: Record<string, unknown>[];
  clues: { id: string; name: string; text: string; category: string; sourceHint: string }[];
  deductions: Record<string, unknown>[];
  endings: Record<string, unknown>[];
  events: Record<string, unknown>[];
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
  eventLogId: string | null;
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

