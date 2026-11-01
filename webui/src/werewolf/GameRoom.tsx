// 观战室：一局狼人杀的完整管理员视角。左侧圆桌 + 选中座位详情 + 局势，
// 右侧时间线 / 投票 / 玩家 / AI 四个标签；事件按 afterSeq 增量拉取。
import {
  ArrowLeftOutlined,
  CopyOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty, Popconfirm, Progress, Skeleton, Tabs, Tag, Tooltip, Typography } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api";
import type { WwEvent, WwGame, WwMeta, WwPlayer } from "./types";
import { EventFeed } from "./EventFeed";
import { AiPanel, PlayersPanel, VotesPanel } from "./RoomPanels";
import { groupTitle } from "./LiveView";
import { wwApi } from "./ww-api";
import {
  clockTime,
  deathLabel,
  formatDuration,
  isNight,
  liveAlerts,
  mergeEvents,
  outlook,
  phaseCountdown,
  phaseElapsed,
  phaseInfo,
  publicPhaseInfo,
  roleInfo,
  ruleText,
  seatActivity,
  seatPosition,
  voteSummaries,
} from "./ww-meta";
import { PhasePill, RatioBar, RoleTag, StageProgress } from "./WwBits";

const { Text } = Typography;
export const ROOM_POLL_MS = 2500;
export type RoomTab = "feed" | "votes" | "players" | "ai";
const ROOM_TABS: RoomTab[] = ["feed", "votes", "players", "ai"];

export function parseRoomTab(value: string | null): RoomTab {
  return ROOM_TABS.includes(value as RoomTab) ? (value as RoomTab) : "feed";
}

interface RoomState {
  game: WwGame | null;
  events: WwEvent[];
  error: string;
  gone: boolean;
  updatedAt: number | null;
}

/** 观战数据：首次全量，之后按最后一条 seq 增量；页面隐藏或手动暂停时不拉 */
function useRoomData(groupId: number, paused: boolean): RoomState & { reload: () => void } {
  const [state, setState] = useState<RoomState>({ game: null, events: [], error: "", gone: false, updatedAt: null });
  const lastSeq = useRef(0);
  const inflight = useRef(false);
  const tick = useCallback(async (force = false) => {
    if (inflight.current || (!force && document.hidden)) return;
    inflight.current = true;
    try {
      const data = await wwApi.events(groupId, lastSeq.current);
      setState((current) => {
        const events = mergeEvents(current.events, data.events);
        lastSeq.current = events.length ? events[events.length - 1].seq : 0;
        return { game: data.game, events, error: "", gone: false, updatedAt: Date.now() };
      });
    } catch (reason) {
      const gone = reason instanceof ApiError && reason.status === 404;
      setState((current) => ({ ...current, gone, error: gone ? "" : reason instanceof Error ? reason.message : "加载失败" }));
    } finally {
      inflight.current = false;
    }
  }, [groupId]);
  useEffect(() => {
    lastSeq.current = 0;
    setState({ game: null, events: [], error: "", gone: false, updatedAt: null });
    void tick(true);
  }, [groupId, tick]);
  useEffect(() => {
    if (paused || state.gone) return undefined;
    const timer = window.setInterval(() => { void tick(); }, ROOM_POLL_MS);
    const onVisible = () => { if (!document.hidden) void tick(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [paused, state.gone, tick]);
  return { ...state, reload: () => { void tick(true); } };
}

export function GameRoom({ groupId, reveal, meta, tab, seat, onTab, onSeat, onBack, onStopped, onOpenHistory }: {
  groupId: number;
  reveal: boolean;
  meta: WwMeta | null;
  tab: RoomTab;
  seat: number | null;
  onTab: (tab: RoomTab) => void;
  onSeat: (seat: number | null) => void;
  onBack: () => void;
  onStopped: () => void;
  onOpenHistory: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [stopping, setStopping] = useState(false);
  const room = useRoomData(groupId, paused);
  const game = room.game;
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const stop = async () => {
    setStopping(true);
    try {
      await wwApi.stop(groupId);
      message.success("已提交强制结束，引擎收尾后对局会从列表消失");
      onStopped();
      room.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setStopping(false);
    }
  };

  const votes = useMemo(() => voteSummaries(room.events), [room.events]);

  // 标签页标题跟随对局，切到别的窗口也能看到进度
  const titleLabel = game ? `${publicPhaseInfo(game.phase, reveal || game.phase === "ENDED", game.phaseLabel).label} · ${groupTitle(game.groupName, game.groupId)}` : null;
  useEffect(() => {
    if (!titleLabel) return undefined;
    const previous = document.title;
    document.title = `狼人杀 · ${titleLabel}`;
    return () => { document.title = previous; };
  }, [titleLabel]);

  // Esc：先取消选中座位，再返回列表（输入框、弹层里按 Esc 不处理）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.closest("input, textarea, [contenteditable=true]") || target.closest(".ant-drawer, .ant-modal, .ant-popover"))) return;
      if (seat !== null) onSeat(null); else onBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [seat, onSeat, onBack]);

  if (!game) {
    return (
      <section className="ww2-room">
        <RoomBackBar onBack={onBack} title={`群 ${groupId}`} />
        {room.gone ? <GoneNotice onBack={onBack} onOpenHistory={onOpenHistory} />
          : room.error ? <Alert type="error" showIcon message="加载对局失败" description={room.error} action={<Button size="small" onClick={room.reload}>重试</Button>} />
          : <Skeleton active paragraph={{ rows: 8 }} />}
      </section>
    );
  }

  const showRole = reveal || game.phase === "ENDED";
  const selected = seat !== null ? game.players.find((player) => player.seat === seat) ?? null : null;
  const alerts = liveAlerts(game, { now, timeouts: meta?.phaseTimeouts });
  return (
    <section className="ww2-room">
      <RoomBackBar onBack={onBack} title={groupTitle(game.groupName, game.groupId)}>
        <PhasePill phase={game.phase} label={game.phaseLabel} reveal={showRole} />
        <Tag>{game.board ?? "未选板子"}</Tag>
        <Tag>第 {game.roundNo} 天</Tag>
        {!game.workerAlive && <Tag color="red">引擎已停止</Tag>}
        <Link to={`/groups/${game.groupId}`} className="ww2-room-link">群设置</Link>
        <span className="ww2-grow" />
        <LiveIndicator paused={paused} gone={room.gone} updatedAt={room.updatedAt} now={now} error={room.error} />
        <Tooltip title={paused ? "继续自动刷新" : "暂停自动刷新（方便仔细阅读）"}>
          <Button size="small" icon={paused ? <PlayCircleOutlined /> : <PauseCircleOutlined />} onClick={() => setPaused((value) => !value)}>{paused ? "继续" : "暂停"}</Button>
        </Tooltip>
        {game.eventLogId && (
          <Tooltip title="复制回放编号：对局结束后可在“战绩 → 回放查询”里打开公开回放">
            <Button size="small" icon={<CopyOutlined />} onClick={() => { void navigator.clipboard?.writeText(game.eventLogId ?? "").then(() => message.success("已复制回放编号"), () => message.info(game.eventLogId ?? "")); }}>回放编号</Button>
          </Tooltip>
        )}
        <Popconfirm title="强制结束这局狼人杀？" description="与群内 /结束游戏 同一路径，本局不计胜负。" okText="强制结束" okButtonProps={{ danger: true }} onConfirm={() => { void stop(); }}>
          <Button size="small" danger icon={<StopOutlined />} loading={stopping} disabled={room.gone}>强制结束</Button>
        </Popconfirm>
      </RoomBackBar>
      {room.gone && <GoneNotice onBack={onBack} onOpenHistory={onOpenHistory} />}
      {alerts.map((alert) => <Alert key={alert.text} className="ww2-gap" type={alert.level} showIcon message={alert.text} />)}
      <PhaseBanner game={game} meta={meta} now={now} reveal={showRole} />
      <div className="ww2-room-grid">
        <div className="ww2-room-left">
          <RoundTable game={game} showRole={showRole} selected={seat} onSelect={(value) => onSeat(value === seat ? null : value)} lastVote={votes[votes.length - 1] ?? null} />
          {selected
            ? <SeatDetail player={selected} game={game} events={room.events} showRole={showRole} onClose={() => onSeat(null)} onFilter={() => onTab("feed")} />
            : <OutlookCard game={game} showRole={showRole} />}
          {game.rules && Object.keys(game.rules).length > 0 && (
            <div className="ww2-panel">
              <h4>本局房规</h4>
              <div className="ww2-chip-row">{Object.entries(game.rules).map(([key, value]) => <Tag key={key}>{ruleText(key, value)}</Tag>)}</div>
            </div>
          )}
        </div>
        <div className="ww2-room-right">
          <Tabs
            activeKey={tab}
            onChange={(key) => onTab(key as RoomTab)}
            items={[
              { key: "feed", label: `时间线 ${room.events.length}`, children: <EventFeed events={room.events} seat={seat} onSeat={onSeat} players={game.players} showRole={showRole} /> },
              { key: "votes", label: `投票 ${votes.length}`, children: <VotesPanel votes={votes} players={game.players} showRole={showRole} onSeat={onSeat} /> },
              { key: "players", label: `玩家 ${game.players.length || game.signupCount}`, children: <PlayersPanel game={game} events={room.events} showRole={showRole} selected={seat} onSeat={onSeat} /> },
              { key: "ai", label: `AI ${game.aiCount}`, children: showRole
                ? <AiPanel events={room.events} onSeat={(value) => { onSeat(value); onTab("feed"); }} />
                : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="AI 的思考过程包含身份和夜间行动，打开右上角“上帝视角”后查看" /> },
            ]}
          />
        </div>
      </div>
    </section>
  );
}

function RoomBackBar({ onBack, title, children }: { onBack: () => void; title: string; children?: React.ReactNode }): React.JSX.Element {
  return (
    <header className="ww2-room-bar">
      <Tooltip title="Esc"><Button size="small" icon={<ArrowLeftOutlined />} onClick={onBack}>返回列表</Button></Tooltip>
      <h3>{title}</h3>
      {children}
    </header>
  );
}

function GoneNotice({ onBack, onOpenHistory }: { onBack: () => void; onOpenHistory: () => void }): React.JSX.Element {
  return (
    <Alert
      className="ww2-gap"
      type="info"
      showIcon
      message="这局已经结束并从内存中清理"
      description="实时时间线只保留在对局期间；终局结果已写入战绩，可在战绩页查看，或用群里播报的回放编号查询公开回放。"
      action={<span className="ww2-actions"><Button size="small" onClick={onOpenHistory}>查看战绩</Button><Button size="small" onClick={onBack}>返回</Button></span>}
    />
  );
}

function LiveIndicator({ paused, gone, updatedAt, now, error }: { paused: boolean; gone: boolean; updatedAt: number | null; now: number; error: string }): React.JSX.Element {
  if (gone) return <span className="ww2-live is-off">已结束</span>;
  if (error) return <Tooltip title={error}><span className="ww2-live is-error">连接异常</span></Tooltip>;
  if (paused) return <span className="ww2-live is-off">已暂停</span>;
  const ago = updatedAt ? Math.max(0, Math.round((now - updatedAt) / 1000)) : null;
  return <span className="ww2-live">实时 · {ago === null ? "连接中" : ago <= 1 ? "刚刚" : `${ago} 秒前`}</span>;
}

function PhaseBanner({ game, meta, now, reveal }: { game: WwGame; meta: WwMeta | null; now: number; reveal: boolean }): React.JSX.Element {
  const info = publicPhaseInfo(game.phase, reveal, game.phaseLabel);
  const countdown = phaseCountdown(game, meta?.phaseTimeouts, now);
  const elapsed = phaseElapsed(game.phaseStartedAt, now);
  const speaker = game.players.find((player) => player.seat === game.currentSpeaker) ?? null;
  const percent = countdown?.total ? Math.round(((countdown.total - countdown.remaining) / countdown.total) * 100) : null;
  return (
    <div className={`ww2-banner${isNight(game.phase) ? " is-night" : ""}`}>
      <div className="ww2-banner-main">
        <strong>{info.label}</strong>
        <span>{info.hint}</span>
        {speaker && <span className="ww2-banner-speaker">🎤 {speaker.seat} 号 {speaker.name} 正在发言</span>}
        {game.voteTargets && game.voteTargets.length > 0 && info.stage !== "night" && (
          <span>可投：{game.voteTargets.map((value) => `${value}号`).join("、")}{game.voteExclude && game.voteExclude.length > 0 ? ` · 不能投票：${game.voteExclude.map((value) => `${value}号`).join("、")}` : ""}</span>
        )}
      </div>
      <div className="ww2-banner-clock">
        {countdown ? (
          <Tooltip title={countdown.estimated ? "按阶段开始时间和配置窗口估算；收到有效行动会提前结束" : "引擎给出的剩余时间"}>
            <span className="ww2-clock">{countdown.estimated ? "约 " : ""}{formatDuration(countdown.remaining)}</span>
          </Tooltip>
        ) : elapsed !== null ? <span className="ww2-clock is-muted">已进行 {formatDuration(elapsed)}</span> : null}
        {percent !== null && <Progress percent={percent} showInfo={false} size="small" strokeColor="var(--accent, #e4558a)" />}
        <StageProgress phase={game.phase} />
      </div>
    </div>
  );
}

function RoundTable({ game, showRole, selected, onSelect, lastVote }: {
  game: WwGame;
  showRole: boolean;
  selected: number | null;
  onSelect: (seat: number) => void;
  lastVote: ReturnType<typeof voteSummaries>[number] | null;
}): React.JSX.Element {
  const night = isNight(game.phase);
  const total = Math.max(game.players.length, 1);
  if (game.players.length === 0) {
    return (
      <div className="ww2-table is-signup">
        <div className="ww2-table-center">
          <strong>报名中 · {game.signupCount} 人</strong>
          <div className="ww2-chip-row">
            {game.signup.map((item) => <Tag key={item.userId} color={item.isAi ? "purple" : undefined}>{item.name}{item.isAi ? " · AI" : ""}{showRole && item.requestedRole ? ` · 想当${item.requestedRole}` : ""}</Tag>)}
          </div>
        </div>
      </div>
    );
  }
  const received = new Map<number, number>();
  for (const bucket of lastVote?.buckets ?? []) received.set(bucket.seat, bucket.votes);
  return (
    <div className={`ww2-table${night ? " is-night" : ""}`} role="group" aria-label="狼人杀圆桌，点击座位查看详情">
      {game.players.map((player) => {
        const pos = seatPosition(player.seat, total, 38);
        const role = roleInfo(player.role, player.faction);
        const cls = [
          "ww2-seat",
          player.alive ? "" : "is-dead",
          player.seat === game.currentSpeaker ? "is-speaking" : "",
          player.seat === selected ? "is-selected" : "",
          showRole ? `is-${role.faction}` : "",
        ].filter(Boolean).join(" ");
        return (
          <button key={player.userId} type="button" className={cls} style={{ left: `${pos.x}%`, top: `${pos.y}%` }} onClick={() => onSelect(player.seat)}
            aria-pressed={player.seat === selected} aria-label={`${player.seat}号 ${player.name}${player.alive ? "" : "，已出局"}`}>
            <span className="ww2-seat-avatar">
              {showRole ? role.icon : player.name.slice(0, 1) || "?"}
              <span className="ww2-seat-no">{player.seat}</span>
              {player.isSheriff && <span className="ww2-seat-badge is-sheriff" title="警长">⭐</span>}
              {player.isAi && <span className="ww2-seat-badge is-ai">AI</span>}
              {player.silenced && <span className="ww2-seat-badge is-silenced" title="今天被禁言/禁票">🤐</span>}
            </span>
            <span className="ww2-seat-name">{player.name}</span>
            {showRole && <span className="ww2-seat-role">{role.role}</span>}
            {!player.alive && <span className="ww2-seat-dead">{deathLabel(player.deathCause)}</span>}
            {received.has(player.seat) && <span className="ww2-seat-votes" title="最近一次计票得票">{received.get(player.seat)} 票</span>}
          </button>
        );
      })}
      <div className="ww2-table-center">
        <span className="ww2-table-icon" aria-hidden="true">{night ? "🌙" : "☀️"}</span>
        <strong>第 {game.roundNo} 天</strong>
        <span>{night ? "天黑请闭眼" : publicPhaseInfo(game.phase, showRole, game.phaseLabel).label}</span>
        <span className="ww2-muted">存活 {game.aliveCount} / {game.playerCount}</span>
      </div>
    </div>
  );
}

function OutlookCard({ game, showRole }: { game: WwGame; showRole: boolean }): React.JSX.Element {
  if (game.players.length === 0) {
    return <div className="ww2-panel"><h4>局势</h4><Text type="secondary">发牌后这里显示阵营存活与胜负走向。</Text></div>;
  }
  if (!showRole) {
    return (
      <div className="ww2-panel">
        <h4>局势</h4>
        <p className="ww2-muted">打开右上角“上帝视角”后可查看阵营存活对比与胜负走向。点击圆桌上的座位查看该玩家详情。</p>
        <RatioBar segments={[{ value: game.aliveCount, tone: "accent", label: "存活" }, { value: game.playerCount - game.aliveCount, tone: "gray", label: "出局" }]} />
      </div>
    );
  }
  const view = outlook(game.players);
  return (
    <div className="ww2-panel">
      <h4>局势 · <span className={`ww2-verdict is-${view.tone}`}>{view.verdict}</span></h4>
      <RatioBar segments={[
        { value: view.wolves, tone: "wolf", label: "狼人" },
        { value: view.gods, tone: "god", label: "神职" },
        { value: view.villagers, tone: "villager", label: "平民" },
      ]} />
      <div className="ww2-legend">
        <span><i className="is-wolf" />狼人 {view.wolves}</span>
        <span><i className="is-god" />神职 {view.gods}</span>
        <span><i className="is-villager" />平民 {view.villagers}</span>
      </div>
      <p className="ww2-muted">默认屠边规则：狼人屠光神职或平民即胜，好人放逐所有狼人即胜。点击座位查看详情。</p>
    </div>
  );
}

function SeatDetail({ player, game, events, showRole, onClose, onFilter }: {
  player: WwPlayer;
  game: WwGame;
  events: WwEvent[];
  showRole: boolean;
  onClose: () => void;
  onFilter: () => void;
}): React.JSX.Element {
  const activity = useMemo(() => seatActivity(events, player.seat), [events, player.seat]);
  const role = roleInfo(player.role, player.faction);
  const flags: { text: string; tone?: string }[] = [];
  if (player.isSheriff) flags.push({ text: "警长", tone: "gold" });
  if (player.sheriffCandidate) flags.push({ text: "警上" });
  if (player.silenced) flags.push({ text: "今日禁言/禁票", tone: "orange" });
  if (player.canVote === false) flags.push({ text: "无投票权", tone: "orange" });
  if (player.dmOk === false && !player.isAi) flags.push({ text: "私聊失败", tone: "red" });
  if (showRole && player.idiotRevealed) flags.push({ text: "白痴已翻牌" });
  if (showRole && role.role === "女巫") {
    flags.push({ text: player.saveUsed ? "解药已用" : "解药在手", tone: player.saveUsed ? undefined : "green" });
    flags.push({ text: player.poisonUsed ? "毒药已用" : "毒药在手", tone: player.poisonUsed ? undefined : "purple" });
  }
  if (showRole && player.ownerSeat) flags.push({ text: `主人 ${player.ownerSeat} 号` });
  if (showRole && player.requestedRole) flags.push({ text: `报名时想当${player.requestedRole}` });
  if (player.pendingAction) flags.push({ text: `排队中：${player.pendingAction}`, tone: "blue" });
  return (
    <div className="ww2-panel ww2-seat-detail">
      <div className="ww2-seat-detail-head">
        <span className={`ww2-seat-avatar is-lg${showRole ? ` is-${role.faction}` : ""}`}>{showRole ? role.icon : player.name.slice(0, 1)}</span>
        <div>
          <strong>{player.seat} 号 · {player.name}</strong>
          <span className="ww2-muted">{player.isAi ? "AI 玩家" : `QQ ${player.userId}`}</span>
        </div>
        <span className="ww2-grow" />
        <Button size="small" type="text" onClick={onClose} aria-label="关闭座位详情">✕</Button>
      </div>
      <div className="ww2-chip-row">
        <RoleTag role={player.role} faction={player.faction} hidden={!showRole} />
        {player.alive ? <Tag color="green">存活</Tag> : <Tag color="red">第 {player.deathRound ?? "?"} 天 · {deathLabel(player.deathCause)}</Tag>}
        {flags.map((flag) => <Tag key={flag.text} color={flag.tone}>{flag.text}</Tag>)}
      </div>
      <dl className="ww2-card-facts">
        <div><dt>发言</dt><dd>{activity.speeches} 次</dd></div>
        <div><dt>字数</dt><dd>{activity.chars}</dd></div>
        <div><dt>投出</dt><dd>{activity.votesCast} 票</dd></div>
        <div><dt>被投</dt><dd>{activity.votesReceived} 票</dd></div>
        {player.isAi && <div><dt>AI 决策</dt><dd>{activity.aiDecisions}{activity.aiRetries ? `（纠正 ${activity.aiRetries}）` : ""}</dd></div>}
      </dl>
      {activity.lastSpeech ? (
        <blockquote className="ww2-quote">
          <span className="ww2-muted">最近发言 · {clockTime(activity.lastSpeech.ts)}</span>
          {activity.lastSpeech.text}
        </blockquote>
      ) : <Text type="secondary">还没有发言记录</Text>}
      <div className="ww2-actions">
        <Button size="small" onClick={onFilter}>在时间线中只看 TA</Button>
        {game.currentSpeaker === player.seat && <Tag color="pink">正在发言</Tag>}
      </div>
    </div>
  );
}

export function EmptyRoom(): React.JSX.Element {
  return <Empty description="选择一局对局开始观战" />;
}
