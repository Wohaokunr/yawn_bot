// 狼人杀管理台的纯逻辑：阶段/角色/死因字典、局势推算、事件筛选与计票汇总。
// 不依赖 React，便于单测；未知的阶段、角色、死因一律原样兜底显示。
import type { WwEvent, WwGame, WwPlayer } from "./types";

// ── 阶段 ──────────────────────────────────────────────

export type WwStage = "signup" | "night" | "dawn" | "sheriff" | "day" | "vote" | "ended";

export interface PhaseInfo {
  key: string;
  label: string;
  stage: WwStage;
  /** 管理员视角的一句话说明（上帝视角，可含夜间子阶段） */
  hint: string;
}

const PHASES: Record<string, Omit<PhaseInfo, "key">> = {
  SIGNUP: { label: "报名中", stage: "signup", hint: "等待玩家报名，满员或房主发车后开局" },
  DEALING: { label: "发牌中", stage: "signup", hint: "正在私聊下发身份卡" },
  NIGHT_HALFBLOOD: { label: "混血儿认主", stage: "night", hint: "首夜混血儿率先睁眼选择主人" },
  NIGHT_GUARD: { label: "守卫守人", stage: "night", hint: "守卫选择今晚守护的玩家" },
  NIGHT_WOLVES: { label: "狼人行动", stage: "night", hint: "狼队讨论并选择刀口" },
  NIGHT_WITCH: { label: "女巫用药", stage: "night", hint: "女巫决定救人或毒人" },
  NIGHT_SEER: { label: "预言家查验", stage: "night", hint: "预言家查验一名玩家" },
  NIGHT_ELDER: { label: "长老禁言", stage: "night", hint: "禁言/禁票长老选择目标" },
  NIGHT_GRAVEKEEPER: { label: "守墓人验尸", stage: "night", hint: "守墓人得知昨日放逐者阵营" },
  DAY_ANNOUNCE: { label: "天亮结算", stage: "dawn", hint: "公布昨夜死讯与禁言情况" },
  LAST_WORDS: { label: "遗言", stage: "dawn", hint: "出局玩家发表遗言" },
  HUNTER_SHOT: { label: "死亡技能", stage: "dawn", hint: "猎人/狼王决定是否开枪" },
  BADGE_TRANSFER: { label: "警徽移交", stage: "dawn", hint: "出局警长移交或撕毁警徽" },
  SHERIFF_REGISTER: { label: "警长报名", stage: "sheriff", hint: "玩家上警竞选" },
  SHERIFF_SPEECH: { label: "竞选发言", stage: "sheriff", hint: "警上玩家依次发言，可退水" },
  SHERIFF_VOTE: { label: "警长投票", stage: "sheriff", hint: "警下玩家投票选出警长" },
  SHERIFF_FINAL_SPEECH: { label: "警长终辩", stage: "sheriff", hint: "平票者进行最终发言" },
  SHERIFF_REVOTE: { label: "警长重投", stage: "sheriff", hint: "对平票者再次投票" },
  DAY_SPEECH: { label: "白天发言", stage: "day", hint: "存活玩家依次发言" },
  DAY_VOTE: { label: "放逐投票", stage: "vote", hint: "全员投票放逐一名玩家" },
  PK_SPEECH: { label: "PK 发言", stage: "vote", hint: "平票玩家进行 PK 发言" },
  PK_VOTE: { label: "PK 投票", stage: "vote", hint: "对 PK 玩家再次投票" },
  ENDED: { label: "已结束", stage: "ended", hint: "对局结束，等待引擎收尾" },
};

/** 未开上帝视角时把夜间子阶段折叠成“夜晚”，避免投屏/截图泄露当前睁眼的角色 */
export function publicPhaseInfo(phase: string | null | undefined, reveal: boolean, fallbackLabel?: string): PhaseInfo {
  const info = phaseInfo(phase, fallbackLabel);
  if (reveal || info.stage !== "night") return info;
  return { ...info, label: "夜晚", hint: "夜间行动进行中（打开上帝视角查看具体环节）" };
}

export function phaseInfo(phase: string | null | undefined, fallbackLabel?: string): PhaseInfo {
  const key = phase ?? "";
  const known = PHASES[key];
  if (known) return { key, ...known };
  const stage: WwStage = key.startsWith("NIGHT_") ? "night" : key ? "day" : "signup";
  return { key, label: fallbackLabel || key || "未知阶段", stage, hint: "" };
}

export const STAGE_META: Record<WwStage, { label: string; tone: string; icon: string }> = {
  signup: { label: "报名", tone: "blue", icon: "📝" },
  night: { label: "黑夜", tone: "night", icon: "🌙" },
  dawn: { label: "天亮", tone: "orange", icon: "🌅" },
  sheriff: { label: "警长竞选", tone: "gold", icon: "⭐" },
  day: { label: "白天发言", tone: "green", icon: "☀️" },
  vote: { label: "投票", tone: "red", icon: "🗳️" },
  ended: { label: "结束", tone: "gray", icon: "🏁" },
};

/** 一局的主流程：用于卡片与观战室顶部的进度条 */
export const STAGE_FLOW: WwStage[] = ["signup", "night", "dawn", "sheriff", "day", "vote", "ended"];

export function isNight(phase: string | null | undefined): boolean {
  return phaseInfo(phase).stage === "night";
}

// ── 角色 / 阵营 / 死因 ───────────────────────────────

export interface RoleInfo {
  role: string;
  icon: string;
  faction: "wolf" | "good";
  kind: "wolf" | "god" | "villager" | "third";
}

const ROLES: Record<string, Omit<RoleInfo, "role">> = {
  狼人: { icon: "🐺", faction: "wolf", kind: "wolf" },
  狼王: { icon: "👑", faction: "wolf", kind: "wolf" },
  白狼王: { icon: "🤍", faction: "wolf", kind: "wolf" },
  隐狼: { icon: "🫥", faction: "wolf", kind: "wolf" },
  村民: { icon: "🧑‍🌾", faction: "good", kind: "villager" },
  混血儿: { icon: "🧬", faction: "good", kind: "villager" },
  预言家: { icon: "🔮", faction: "good", kind: "god" },
  女巫: { icon: "🧪", faction: "good", kind: "god" },
  猎人: { icon: "🏹", faction: "good", kind: "god" },
  白痴: { icon: "🤪", faction: "good", kind: "god" },
  守卫: { icon: "🛡️", faction: "good", kind: "god" },
  骑士: { icon: "⚔️", faction: "good", kind: "god" },
  禁言长老: { icon: "🤐", faction: "good", kind: "god" },
  守墓人: { icon: "🪦", faction: "good", kind: "god" },
};

export function roleInfo(role: string | null | undefined, faction?: string | null): RoleInfo {
  const name = role ?? "";
  const known = ROLES[name];
  if (known) return { role: name, ...known };
  const isWolf = faction === "wolf" || name.includes("狼");
  return { role: name || "未知", icon: isWolf ? "🐺" : "❔", faction: isWolf ? "wolf" : "good", kind: isWolf ? "wolf" : "villager" };
}

export function factionLabel(faction: string | null | undefined): string {
  if (faction === "wolf") return "狼人阵营";
  if (faction === "good") return "好人阵营";
  return "未知阵营";
}

export const DEATH_CAUSE: Record<string, { label: string; icon: string }> = {
  WOLF_KILL: { label: "夜晚遇袭", icon: "🐺" },
  WITCH_POISON: { label: "女巫毒杀", icon: "🧪" },
  VOTED: { label: "放逐出局", icon: "🗳️" },
  HUNTER_SHOT: { label: "猎人开枪", icon: "🏹" },
  SELF_DETONATION: { label: "狼人自爆", icon: "💥" },
  KNIGHT_KILL: { label: "骑士决斗", icon: "⚔️" },
  KNIGHT_DEATH: { label: "决斗殉身", icon: "🩸" },
  WOLF_KING_SHOT: { label: "狼王开枪", icon: "👑" },
  WHITE_WOLF_KING_BOOM: { label: "白狼王自爆带人", icon: "💥" },
};

export function deathLabel(cause: string | null | undefined): string {
  if (!cause) return "出局";
  return DEATH_CAUSE[cause]?.label ?? cause;
}

// ── 事件 ──────────────────────────────────────────────

export type FeedFilter = "all" | "talk" | "vote" | "death" | "ai" | "flow" | "system";

export const FEED_FILTERS: { value: FeedFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "talk", label: "发言" },
  { value: "vote", label: "计票" },
  { value: "death", label: "出局" },
  { value: "flow", label: "流程播报" },
  { value: "ai", label: "AI 思考" },
  { value: "system", label: "系统" },
];

export const EVENT_META: Record<string, { label: string; tone: string }> = {
  phase: { label: "阶段", tone: "blue" },
  announce: { label: "播报", tone: "gray" },
  death: { label: "出局", tone: "red" },
  speech: { label: "发言", tone: "green" },
  vote_tally: { label: "计票", tone: "gold" },
  ai_decision: { label: "AI 决策", tone: "purple" },
  ai_speech: { label: "AI 发言稿", tone: "purple" },
  system: { label: "系统", tone: "red" },
};

export function eventFilterOf(type: string): Exclude<FeedFilter, "all"> {
  switch (type) {
    case "speech": return "talk";
    case "vote_tally": return "vote";
    case "death": return "death";
    case "ai_decision":
    case "ai_speech": return "ai";
    case "system": return "system";
    default: return "flow";
  }
}

export interface FeedQuery {
  filter: FeedFilter;
  seat: number | null;
  round: number | null;
  text: string;
}

export function filterEvents(events: WwEvent[], query: FeedQuery): WwEvent[] {
  const needle = query.text.trim().toLowerCase();
  return events.filter((event) => {
    if (query.filter !== "all" && eventFilterOf(event.type) !== query.filter) return false;
    if (query.seat !== null && !eventMentionsSeat(event, query.seat)) return false;
    if (query.round !== null && event.roundNo !== query.round) return false;
    if (needle) {
      const haystack = `${event.text ?? ""} ${event.name ?? ""} ${event.extra.instruction ?? ""}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

/** 事件是否与某座位相关：本人事件或计票里投出/被投 */
export function eventMentionsSeat(event: WwEvent, seat: number): boolean {
  if (event.seat === seat) return true;
  if (event.type === "vote_tally") {
    return (event.extra.votes ?? []).some((vote) => vote.voterSeat === seat || vote.targetSeat === seat);
  }
  return false;
}

export function filterCounts(events: WwEvent[]): Record<FeedFilter, number> {
  const counts: Record<FeedFilter, number> = { all: events.length, talk: 0, vote: 0, death: 0, ai: 0, flow: 0, system: 0 };
  for (const event of events) counts[eventFilterOf(event.type)] += 1;
  return counts;
}

export interface RoundGroup {
  round: number | null;
  events: WwEvent[];
}

/** 按“第 N 天”分组（保持事件原有顺序）；连续同轮合并为一段 */
export function groupByRound(events: WwEvent[]): RoundGroup[] {
  const groups: RoundGroup[] = [];
  for (const event of events) {
    const last = groups[groups.length - 1];
    if (last && last.round === event.roundNo) last.events.push(event);
    else groups.push({ round: event.roundNo, events: [event] });
  }
  return groups;
}

export function roundLabel(round: number | null | undefined): string {
  if (round === null || round === undefined) return "开局前";
  if (round <= 0) return "开局前";
  return `第 ${round} 天`;
}

/** 增量合并（afterSeq）：按 seq 去重排序；对方 seq 回退说明换了一局，直接替换 */
export function mergeEvents(previous: WwEvent[], incoming: WwEvent[], cap = 500): WwEvent[] {
  if (incoming.length === 0) return previous;
  const lastSeq = previous.length ? previous[previous.length - 1].seq : 0;
  if (incoming[0].seq <= lastSeq && incoming[incoming.length - 1].seq < lastSeq) return incoming.slice(-cap);
  const seen = new Set(previous.map((event) => event.seq));
  const merged = previous.concat(incoming.filter((event) => !seen.has(event.seq)));
  merged.sort((left, right) => left.seq - right.seq);
  return merged.slice(-cap);
}

// ── 计票 ──────────────────────────────────────────────

export interface VoteBucket {
  seat: number;
  votes: number;
  voters: { seat: number; sheriff: boolean }[];
}

export interface VoteSummary {
  seq: number;
  round: number | null;
  phase: string | null;
  label: string;
  buckets: VoteBucket[];
  abstain: number[];
  leaders: number[];
  total: number;
}

export function voteSummaries(events: WwEvent[]): VoteSummary[] {
  return events.filter((event) => event.type === "vote_tally").map((event) => {
    const votes = event.extra.votes ?? [];
    const buckets = new Map<number, VoteBucket>();
    const abstain: number[] = [];
    for (const vote of votes) {
      if (vote.targetSeat === null) { abstain.push(vote.voterSeat); continue; }
      const bucket = buckets.get(vote.targetSeat) ?? { seat: vote.targetSeat, votes: 0, voters: [] };
      bucket.votes += vote.isSheriff ? 1.5 : 1;
      bucket.voters.push({ seat: vote.voterSeat, sheriff: vote.isSheriff });
      buckets.set(vote.targetSeat, bucket);
    }
    // 后端给出的 counts 是权威计票（含警长 1.5 票），有则覆盖
    const counts = event.extra.counts ?? {};
    for (const [seat, count] of Object.entries(counts)) {
      const bucket = buckets.get(Number(seat));
      if (bucket) bucket.votes = Number(count);
    }
    const sorted = [...buckets.values()].sort((left, right) => right.votes - left.votes || left.seat - right.seat);
    const top = sorted[0]?.votes ?? 0;
    return {
      seq: event.seq,
      round: event.roundNo,
      phase: event.phase,
      label: phaseInfo(event.phase).label,
      buckets: sorted,
      abstain: abstain.sort((left, right) => left - right),
      leaders: top > 0 ? sorted.filter((bucket) => bucket.votes === top).map((bucket) => bucket.seat) : [],
      total: votes.length,
    };
  });
}

// ── 局势 ──────────────────────────────────────────────

export interface Outlook {
  wolves: number;
  gods: number;
  villagers: number;
  goodAlive: number;
  /** 屠边规则下最接近的胜负提示 */
  verdict: string;
  tone: "wolf" | "good" | "even";
}

export function outlook(players: WwPlayer[]): Outlook {
  const alive = players.filter((player) => player.alive);
  let wolves = 0;
  let gods = 0;
  let villagers = 0;
  for (const player of alive) {
    const info = roleInfo(player.role, player.faction);
    if (info.faction === "wolf") wolves += 1;
    else if (info.kind === "god") gods += 1;
    else villagers += 1;
  }
  const goodAlive = gods + villagers;
  let verdict = "势均力敌";
  let tone: Outlook["tone"] = "even";
  if (players.length === 0) verdict = "尚未发牌";
  else if (wolves === 0) { verdict = "狼人已全部出局"; tone = "good"; }
  else if (gods === 0 || villagers === 0) { verdict = gods === 0 ? "神职已被屠尽" : "平民已被屠尽"; tone = "wolf"; }
  else if (wolves >= goodAlive) { verdict = "狼人数量占优"; tone = "wolf"; }
  else if (gods === 1 || villagers === 1) { verdict = gods === 1 ? `只剩 1 名神职，狼人差一刀屠边` : `只剩 1 名平民，狼人差一刀屠边`; tone = "wolf"; }
  else if (wolves === 1) { verdict = "只剩最后 1 只狼"; tone = "good"; }
  return { wolves, gods, villagers, goodAlive, verdict, tone };
}

// ── 运维告警 ──────────────────────────────────────────

export interface LiveAlert {
  level: "error" | "warning" | "info";
  text: string;
}

export const QUEUE_WARN = 20;
export const STUCK_GRACE_SECONDS = 60;

export function liveAlerts(game: WwGame, options: { now: number; timeouts?: Record<string, number> } = { now: Date.now() }): LiveAlert[] {
  const alerts: LiveAlert[] = [];
  if (!game.workerAlive && game.phase !== "ENDED") alerts.push({ level: "error", text: "引擎任务已停止，对局不会再推进，建议强制结束" });
  if (game.queueDepth >= QUEUE_WARN) alerts.push({ level: "warning", text: `行动队列积压 ${game.queueDepth} 条` });
  const dmFailed = game.players.filter((player) => player.dmOk === false && !player.isAi);
  if (dmFailed.length) alerts.push({ level: "warning", text: `${dmFailed.map((player) => `${player.seat}号`).join("、")} 私聊发送失败（可能未加好友）` });
  const elapsed = phaseElapsed(game.phaseStartedAt, options.now);
  const timeout = options.timeouts?.[game.phase ?? ""];
  if (elapsed !== null && timeout && elapsed > timeout + STUCK_GRACE_SECONDS && game.phase !== "ENDED") {
    alerts.push({ level: "warning", text: `当前阶段已持续 ${formatDuration(elapsed)}，超过窗口 ${formatDuration(timeout)}` });
  }
  if (game.players.length > 0 && game.players.every((player) => player.isAi)) alerts.push({ level: "info", text: "全员 AI 对局" });
  return alerts;
}

export function phaseElapsed(startedAt: string | null | undefined, now: number): number | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.round((now - started) / 1000));
}

/** 剩余秒数：优先用引擎给的精确值，否则用阶段开始时间 + 配置窗口估算 */
export function phaseCountdown(game: WwGame, timeouts: Record<string, number> | undefined, now: number): { remaining: number; total: number | null; estimated: boolean } | null {
  const total = timeouts?.[game.phase ?? ""] ?? null;
  if (typeof game.phaseRemaining === "number") return { remaining: Math.max(0, Math.round(game.phaseRemaining)), total, estimated: false };
  const elapsed = phaseElapsed(game.phaseStartedAt, now);
  if (elapsed === null || !total) return null;
  return { remaining: Math.max(0, total - elapsed), total, estimated: true };
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.round(seconds));
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

export function gameDuration(startedAt: string | null | undefined, endedAt: string | null | undefined): number | null {
  if (!startedAt || !endedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(endedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return Math.round((end - start) / 1000);
}

export function pct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

// ── 座位 ──────────────────────────────────────────────

/** 座位绕椭圆轨道均匀分布（百分比坐标）；1 号位在正上方，顺时针排列 */
export function seatPosition(seat: number, total: number, radius = 42): { x: number; y: number } {
  const count = Math.max(total, 1);
  const angle = ((seat - 1) / count) * Math.PI * 2 - Math.PI / 2;
  return { x: 50 + radius * Math.cos(angle), y: 50 + radius * Math.sin(angle) };
}

/** 时间线用的短时间：同一天只显示时分秒 */
export function clockTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

/** 列表用的紧凑日期：今年省略年份 */
export function shortDateTime(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  const pad = (n: number) => String(n).padStart(2, "0");
  const day = `${date.getMonth() + 1}/${date.getDate()}`;
  return `${sameYear ? "" : `${date.getFullYear()}/`}${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 每日统计补齐没有对局的日子，柱状图才能看出空档 */
export function fillDays<T extends { date: string; games: number }>(rows: T[], make: (date: string) => T): T[] {
  if (rows.length < 2) return rows;
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const out: T[] = [];
  const start = Date.parse(`${rows[0].date}T00:00:00Z`);
  const end = Date.parse(`${rows[rows.length - 1].date}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end) || end - start > 400 * 86_400_000) return rows;
  for (let at = start; at <= end; at += 86_400_000) {
    const key = new Date(at).toISOString().slice(0, 10);
    out.push(byDate.get(key) ?? make(key));
  }
  return out;
}

export interface SeatActivity {
  speeches: number;
  chars: number;
  aiDecisions: number;
  aiRetries: number;
  lastSpeech: WwEvent | null;
  votesCast: number;
  votesReceived: number;
}

export function seatActivity(events: WwEvent[], seat: number): SeatActivity {
  const activity: SeatActivity = { speeches: 0, chars: 0, aiDecisions: 0, aiRetries: 0, lastSpeech: null, votesCast: 0, votesReceived: 0 };
  for (const event of events) {
    if (event.type === "speech" && event.seat === seat) {
      activity.speeches += 1;
      activity.chars += (event.text ?? "").length;
      activity.lastSpeech = event;
    } else if (event.type === "ai_decision" && event.seat === seat) {
      activity.aiDecisions += 1;
      if ((event.extra.attempt ?? 1) > 1) activity.aiRetries += 1;
    } else if (event.type === "vote_tally") {
      for (const vote of event.extra.votes ?? []) {
        if (vote.voterSeat === seat && vote.targetSeat !== null) activity.votesCast += 1;
        if (vote.targetSeat === seat) activity.votesReceived += 1;
      }
    }
  }
  return activity;
}

export interface AiSummary {
  decisions: number;
  speeches: number;
  retries: number;
  empty: number;
  bySeat: { seat: number; name: string | null; decisions: number; retries: number; empty: number }[];
}

export function aiSummary(events: WwEvent[]): AiSummary {
  const bySeat = new Map<number, AiSummary["bySeat"][number]>();
  const summary: AiSummary = { decisions: 0, speeches: 0, retries: 0, empty: 0, bySeat: [] };
  for (const event of events) {
    if (event.type !== "ai_decision" && event.type !== "ai_speech") continue;
    const empty = !(event.text ?? "").trim();
    const retry = (event.extra.attempt ?? 1) > 1;
    if (event.type === "ai_decision") summary.decisions += 1; else summary.speeches += 1;
    if (retry) summary.retries += 1;
    if (empty) summary.empty += 1;
    if (event.seat !== null) {
      const row = bySeat.get(event.seat) ?? { seat: event.seat, name: event.name, decisions: 0, retries: 0, empty: 0 };
      row.decisions += 1;
      if (retry) row.retries += 1;
      if (empty) row.empty += 1;
      bySeat.set(event.seat, row);
    }
  }
  summary.bySeat = [...bySeat.values()].sort((left, right) => left.seat - right.seat);
  return summary;
}

/** 出局顺序（按轮次、座位稳定排序）：观战室与赛后详情共用 */
export function deathOrder<T extends { seat: number; deathRound: number | null }>(players: T[]): T[] {
  return players.filter((player) => player.deathRound !== null).sort((left, right) => (left.deathRound ?? 0) - (right.deathRound ?? 0) || left.seat - right.seat);
}

// ── 配置字典 ──────────────────────────────────────────

export interface ConfigField { key: string; label: string; unit?: string; group: string }

export const CONFIG_FIELDS: ConfigField[] = [
  { key: "ww_min_players", label: "最少开局人数", unit: "人", group: "报名与开局" },
  { key: "ww_max_players", label: "满员自动开局", unit: "人", group: "报名与开局" },
  { key: "ww_signup_timeout", label: "报名窗口", unit: "秒", group: "报名与开局" },
  { key: "ww_signup_warn_remain", label: "第一次报名提醒", unit: "秒前", group: "报名与开局" },
  { key: "ww_signup_warn_remain_final", label: "最后报名提醒", unit: "秒前", group: "报名与开局" },
  { key: "ww_role_request", label: "允许私聊选身份", group: "报名与开局" },
  { key: "ww_night_timeout", label: "女巫/预言家行动", unit: "秒", group: "夜晚" },
  { key: "ww_wolf_timeout", label: "狼人行动", unit: "秒", group: "夜晚" },
  { key: "ww_night_warn_remain", label: "夜间氛围播报间隔", unit: "秒", group: "夜晚" },
  { key: "ww_speech_timeout", label: "每人发言", unit: "秒", group: "白天" },
  { key: "ww_vote_timeout", label: "投票窗口", unit: "秒", group: "白天" },
  { key: "ww_hunter_timeout", label: "猎人开枪决策", unit: "秒", group: "白天" },
  { key: "ww_last_words_timeout", label: "遗言", unit: "秒", group: "白天" },
  { key: "ww_sheriff_register_timeout", label: "警长报名", unit: "秒", group: "白天" },
  { key: "ww_badge_timeout", label: "警徽移交 / 定发言序", unit: "秒", group: "白天" },
  { key: "ww_ai_enabled", label: "启用 AI 玩家", group: "AI 玩家" },
  { key: "ww_ai_autofill", label: "人数不足自动补 AI", group: "AI 玩家" },
  { key: "ww_ai_max", label: "单局 AI 上限", unit: "个", group: "AI 玩家" },
  { key: "ww_ai_wolf_discuss", label: "狼队两段式讨论", group: "AI 玩家" },
  { key: "ww_ai_decision_timeout", label: "AI 决策超时", unit: "秒", group: "AI 玩家" },
  { key: "ww_ai_speech_timeout", label: "AI 发言超时", unit: "秒", group: "AI 玩家" },
  { key: "ww_ai_discuss_timeout", label: "狼队讨论超时", unit: "秒", group: "AI 玩家" },
  { key: "ww_ai_register_buffer", label: "有 AI 时上警窗口延长", unit: "秒", group: "AI 玩家" },
  { key: "ww_ai_max_tokens", label: "AI 决策 token 上限", group: "AI 玩家" },
  { key: "ww_ai_speech_max_tokens", label: "AI 发言 token 上限", group: "AI 玩家" },
  { key: "ww_api_timeout", label: "OneBot 调用超时", unit: "秒", group: "运行保护" },
  { key: "ww_action_queue_max", label: "行动队列上限", unit: "条", group: "运行保护" },
  { key: "ww_user_pending_max", label: "每人待处理行动上限", unit: "条", group: "运行保护" },
];

export interface ConfigRow extends ConfigField { value: string | number | boolean | null }

/** 把后端配置按分组排好；未登记的新字段归入“其他”并显示原始键名 */
export function configRows(config: Record<string, string | number | boolean | null>): { group: string; rows: ConfigRow[] }[] {
  const known = new Set(CONFIG_FIELDS.map((field) => field.key));
  const rows: ConfigRow[] = CONFIG_FIELDS.filter((field) => field.key in config).map((field) => ({ ...field, value: config[field.key] }));
  for (const [key, value] of Object.entries(config)) {
    if (!known.has(key)) rows.push({ key, label: key, group: "其他", value });
  }
  const groups: { group: string; rows: ConfigRow[] }[] = [];
  for (const row of rows) {
    const bucket = groups.find((item) => item.group === row.group);
    if (bucket) bucket.rows.push(row); else groups.push({ group: row.group, rows: [row] });
  }
  return groups;
}

export function configValueText(row: ConfigRow): string {
  if (typeof row.value === "boolean") return row.value ? "开启" : "关闭";
  if (row.value === null || row.value === "") return "—";
  return row.unit ? `${row.value} ${row.unit}` : String(row.value);
}

export const RULE_LABELS: Record<string, string> = {
  sheriff: "警长竞选",
  sheriff_enabled: "警长竞选",
  witch_self_save_first_night: "女巫首夜可自救",
  guard_witch_same_target_dies: "同守同救死亡",
  reveal_on_death: "出局翻牌",
  win_mode: "胜利条件",
  allow_revote: "允许改票",
  speech_seconds: "发言时长",
  vote_seconds: "投票时长",
};

export function ruleText(key: string, value: string | number | boolean | null): string {
  const label = RULE_LABELS[key] ?? key;
  if (typeof value === "boolean") return `${label}：${value ? "开" : "关"}`;
  return `${label}：${value ?? "—"}`;
}

// ── URL 状态 ──────────────────────────────────────────

export type WwView = "live" | "history" | "insights" | "rules";
export const WW_VIEWS: WwView[] = ["live", "history", "insights", "rules"];

export function parseView(value: string | null): WwView {
  return WW_VIEWS.includes(value as WwView) ? (value as WwView) : "live";
}

export function parseIntParam(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}
