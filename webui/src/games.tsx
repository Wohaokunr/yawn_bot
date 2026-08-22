import {
  DesktopOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  MoonOutlined,
  PlayCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Input,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type {
  LiveGames,
  RpgHistoryGame,
  RpgLiveGame,
  WerewolfGameDetail,
  WerewolfGameEvent,
  WerewolfHistoryGame,
  WerewolfLiveGame,
  WerewolfLivePlayer,
} from "./types";

const { Text } = Typography;

// 与后端 / yawn_werewolf 保持一致的不良死因文案;未知值原样展示。
const WW_DEATH_CAUSE: Record<string, string> = {
  WOLF_KILL: "夜晚遇袭",
  WITCH_POISON: "女巫毒杀",
  VOTED: "放逐出局",
  HUNTER_SHOT: "猎人开枪",
  SELF_DETONATION: "狼人自爆",
  KNIGHT_KILL: "骑士决斗",
  KNIGHT_DEATH: "决斗殉身",
};

const WW_PHASE_COLORS: Record<string, string> = {
  SIGNUP: "blue",
  DEALING: "cyan",
  NIGHT_HALFBLOOD: "purple",
  NIGHT_WOLVES: "purple",
  NIGHT_WITCH: "purple",
  NIGHT_SEER: "purple",
  NIGHT_ELDER: "purple",
  DAY_ANNOUNCE: "orange",
  LAST_WORDS: "orange",
  HUNTER_SHOT: "orange",
  BADGE_TRANSFER: "orange",
  SHERIFF_REGISTER: "geekblue",
  SHERIFF_SPEECH: "geekblue",
  SHERIFF_VOTE: "geekblue",
  SHERIFF_FINAL_SPEECH: "geekblue",
  SHERIFF_REVOTE: "geekblue",
  DAY_SPEECH: "gold",
  DAY_VOTE: "gold",
  PK_SPEECH: "volcano",
  PK_VOTE: "volcano",
  ENDED: "default",
};

const RPG_PHASE_COLORS: Record<string, string> = {
  SIGNUP: "blue",
  CHAR_CREATE: "cyan",
  PLAY: "green",
  ENDED: "default",
};

const RPG_OUTCOMES: Record<string, { label: string; color: string }> = {
  good: { label: "好结局", color: "green" },
  bad: { label: "坏结局", color: "red" },
  neutral: { label: "中立结局", color: "gold" },
};

const RPG_TERMINATION: Record<string, string> = { manual_stop: "手动结束" };

const STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "running", label: "进行中" },
  { value: "finished", label: "已结束" },
];

function groupTitle(name?: string | null, groupId?: number): string {
  return name || (groupId !== undefined ? `群 ${groupId}` : "未知群");
}

function phaseTag(phase: string | null, label: string, colors: Record<string, string>): React.JSX.Element {
  return <Tag color={colors[phase ?? ""] ?? "default"}>{label || phase || "—"}</Tag>;
}

// 座位绕椭圆轨道均匀分布的角度位置(百分比);1号位在正上方,顺时针排列。
export function wwSeatPosition(seat: number, total: number): { x: number; y: number } {
  const count = Math.max(total, 1);
  const angle = ((seat - 1) / count) * Math.PI * 2 - Math.PI / 2;
  return { x: 50 + 42 * Math.cos(angle), y: 50 + 42 * Math.sin(angle) };
}

function wwIsNight(phase: string | null): boolean {
  return phase?.startsWith("NIGHT_") ?? false;
}

// 夜空星星/飘落花瓣的固定布景坐标(装饰层,不参与布局)
const WW_STARS: { x: number; y: number; delay: number; size: number }[] = [
  { x: 14, y: 16, delay: 0, size: 3 },
  { x: 26, y: 8, delay: 0.7, size: 2 },
  { x: 40, y: 12, delay: 1.4, size: 2.5 },
  { x: 58, y: 7, delay: 0.4, size: 2 },
  { x: 72, y: 13, delay: 1.9, size: 3 },
  { x: 86, y: 18, delay: 1.1, size: 2 },
  { x: 20, y: 78, delay: 0.9, size: 2 },
  { x: 80, y: 80, delay: 1.6, size: 2.5 },
];
const WW_PETALS: { x: number; delay: number; duration: number }[] = [
  { x: 18, delay: 0, duration: 7 },
  { x: 34, delay: 2.2, duration: 9 },
  { x: 52, delay: 4.5, duration: 8 },
  { x: 68, delay: 1.3, duration: 10 },
  { x: 82, delay: 3.6, duration: 7.5 },
];

export function GamesPage(): React.JSX.Element {
  const load = useCallback(() => api<LiveGames>("/games/live").then((r) => r.data), []);
  const query = useApiQuery(load);
  // 实时对局只有强停会推送 entity.changed,阶段流转靠轮询兜底;
  // 页面隐藏时暂停轮询,回到前台立即补一次。
  const reload = query.reload;
  useEffect(() => {
    const tick = () => { if (!document.hidden) reload(); };
    const timer = window.setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [reload]);
  // Tab 写入 URL,刷新或分享链接时保持狼人杀/跑团上下文。
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") === "rpg" ? "rpg" : "werewolf";
  const live = query.data;
  if (!live) return query.error ? <Alert className="section-alert" type="error" showIcon message={query.error} /> : <Spin />;
  return <>
    <PageHeader title="对局中心" subtitle="狼人杀与跑团的实时监控与战绩(只读口径,强停走子插件状态机)" />
    <Tabs activeKey={tab} onChange={(key) => setSearchParams(key === "werewolf" ? {} : { tab: key }, { replace: true })} items={[
      { key: "werewolf", label: <span><MoonOutlined /> 狼人杀</span>, children: <WerewolfTab live={live.werewolf} onChanged={reload} /> },
      { key: "rpg", label: <span><PlayCircleOutlined /> 跑团</span>, children: <RpgTab live={live.rpg} onChanged={reload} /> },
    ]} />
  </>;
}

function PluginUnavailable({ kind }: { kind: string }): React.JSX.Element {
  return <Alert className="section-alert" type="warning" showIcon message={`${kind}子插件未加载`} description="实时对局与战绩不可用;子插件随 bot 启动加载后此页自动恢复。" />;
}

function useStopGame(kind: "werewolf" | "rpg", onChanged: () => void): { stopping: number; stop: (groupId: number) => Promise<void> } {
  const { message } = AntApp.useApp();
  const [stopping, setStopping] = useState(0);
  const stop = async (groupId: number) => {
    setStopping(groupId);
    try {
      await api(`/games/${kind}/${groupId}/stop`, { method: "POST" });
      message.success("已提交强制结束,等待引擎收尾");
      onChanged();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setStopping(0);
    }
  };
  return { stopping, stop };
}

// ── 狼人杀 ───────────────────────────────────────────────

function WerewolfTab({ live, onChanged }: { live: LiveGames["werewolf"]; onChanged: () => void }): React.JSX.Element {
  const [showRoles, setShowRoles] = useState(false);
  const [viewGroupId, setViewGroupId] = useState<number | null>(null);
  const { stopping, stop } = useStopGame("werewolf", onChanged);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const loadHistory = useCallback(() => api<WerewolfHistoryGame[]>(`/games/werewolf/history?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&status=${status}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search, status]);
  const historyQuery = useApiQuery(loadHistory);
  if (!live.available) return <PluginUnavailable kind="狼人杀" />;
  const reveal = showRoles;
  const columns: ColumnsType<WerewolfHistoryGame> = [
    { title: "群", render: (_, row) => <><GroupCell group={row} /></> },
    { title: "板子", dataIndex: "board", width: 100, render: (value: string | null) => value ?? "—" },
    { title: "人数", dataIndex: "playerCount", width: 70 },
    { title: "结果", width: 110, render: (_, row) => row.winnerFaction ? <Tag color={row.winnerFaction === "wolf" ? "red" : "green"}>{row.winnerFaction === "wolf" ? "狼人胜" : "好人胜"}</Tag> : <Tag color="blue">进行中</Tag> },
    { title: "轮次", dataIndex: "endRound", width: 70, render: (value: number | null) => value ?? "—" },
    { title: "开始时间", dataIndex: "startedAt", render: formatTime },
    { title: "结束时间", dataIndex: "endedAt", render: formatTime },
  ];
  return <>
    <Flex justify="space-between" align="center" className="live-heading">
      <Text strong>实时对局({live.games.length} 局)</Text>
      <Space>
        <Text type="secondary">{reveal ? <EyeOutlined /> : <EyeInvisibleOutlined />} 管理台显示身份</Text>
        <Switch size="small" checked={showRoles} onChange={setShowRoles} />
      </Space>
    </Flex>
    {live.games.length === 0 ? <Empty description="当前没有进行中的狼人杀对局" /> : (
      <Row gutter={[16, 16]}>
        {live.games.map((game) => <WerewolfLiveCard key={game.groupId} game={game} reveal={reveal} stopping={stopping === game.groupId} onStop={() => stop(game.groupId)} onView={() => setViewGroupId(game.groupId)} />)}
      </Row>
    )}
    <WerewolfGameDrawer groupId={viewGroupId} reveal={showRoles} onClose={() => setViewGroupId(null)} />
    <Card className="section-row" title="对局战绩" extra={<Space>
      <Input.Search placeholder="搜索群号或房主" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />
      <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }} options={STATUS_OPTIONS} />
    </Space>}>
      {historyQuery.error && !historyQuery.data
        ? <QueryErrorAlert error={historyQuery.error} onRetry={historyQuery.reload} />
        : <Table rowKey="id" size="small" columns={columns} loading={historyQuery.loading} dataSource={historyQuery.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: historyQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <WerewolfHistoryPlayers row={row} /> }} />}
    </Card>
  </>;
}

function GroupCell({ group }: { group: { groupId: number; groupName?: string | null; hostUserId: number } }): React.JSX.Element {
  return <>{groupTitle(group.groupName, group.groupId)}<br /><Text type="secondary">房主 {group.hostUserId}</Text></>;
}

function WerewolfLiveCard({ game, reveal, stopping, onStop, onView }: { game: WerewolfLiveGame; reveal: boolean; stopping: boolean; onStop: () => Promise<void>; onView: () => void }): React.JSX.Element {
  const ended = game.phase === "ENDED";
  return <Col xs={24} xl={12}>
    <Card title={<Space>{groupTitle(game.groupName, game.groupId)}{phaseTag(game.phase, game.phaseLabel, WW_PHASE_COLORS)}{!game.workerAlive && <Tag color="red">引擎停摆</Tag>}</Space>}
      extra={<Space>
        <Button size="small" icon={<DesktopOutlined />} onClick={onView}>进入对局</Button>
        <Popconfirm title="强制结束这局狼人杀?" description="走子插件 stop_game,与群内结束命令同一路径。" onConfirm={onStop}><Button danger size="small" icon={<StopOutlined />} loading={stopping}>强制结束</Button></Popconfirm>
      </Space>}>
      <Descriptions size="small" column={3} items={[
        { key: "board", label: "板子", children: game.board ?? "—" },
        { key: "round", label: "轮次", children: game.roundNo },
        { key: "count", label: "玩家/存活", children: `${game.playerCount} / ${game.aliveCount}` },
        { key: "ai", label: "AI 玩家", children: game.aiCount },
        { key: "queue", label: "队列/待处理", children: `${game.queueDepth} / ${game.pendingCount}` },
        { key: "host", label: "房主", children: game.hostUserId },
      ]} />
      <Table size="small" rowKey="userId" pagination={false} dataSource={game.players} className="live-players" columns={[
        { title: "座", dataIndex: "seat", width: 44 },
        { title: "玩家", render: (_, p) => <>{p.name}{p.isAi && <Tag className="inline-tag">AI</Tag>}<br /><Text type="secondary">{p.userId}</Text></> },
        { title: "状态", width: 120, render: (_, p) => p.alive ? <Tag color="green">存活</Tag> : <Tag color="red">{p.deathRound ? `第 ${p.deathRound} 轮` : ""} {WW_DEATH_CAUSE[p.deathCause ?? ""] ?? "出局"}</Tag> },
        { title: "警徽", width: 64, render: (_, p) => p.isSheriff ? <Tag color="gold">警长</Tag> : "—" },
        { title: "身份", width: 110, render: (_, p) => (reveal || ended) ? <Tag color={p.faction === "wolf" ? "red" : "green"}>{p.role ?? "—"}</Tag> : <Text type="secondary">🔒 隐藏</Text> },
      ]} />
      {game.signup.length > 0 && <div className="signup-line"><Text type="secondary">报名({game.signup.length}):</Text>{game.signup.map((s) => <Tag key={s.userId} color="pink">{s.name}</Tag>)}</div>}
    </Card>
  </Col>;
}

function WerewolfHistoryPlayers({ row }: { row: WerewolfHistoryGame }): React.JSX.Element {
  return <Table size="small" rowKey="userId" pagination={false} dataSource={row.players} columns={[
    { title: "座", dataIndex: "seat", width: 44 },
    { title: "玩家", render: (_, p) => <>{p.userId}{p.isAi && <Tag className="inline-tag">AI</Tag>}</> },
    { title: "身份", width: 100, render: (_, p) => <Tag color={p.faction === "wolf" ? "red" : "green"}>{p.role}</Tag> },
    { title: "警徽", width: 64, render: (_, p) => p.isSheriff ? <Tag color="gold">警长</Tag> : "—" },
    { title: "胜负", width: 80, render: (_, p) => p.isWinner === null ? "—" : p.isWinner ? <Tag color="green">胜</Tag> : <Tag>负</Tag> },
    { title: "出局", render: (_, p) => p.deathRound ? `第 ${p.deathRound} 轮 · ${WW_DEATH_CAUSE[p.deathCause ?? ""] ?? p.deathCause ?? "出局"}` : "存活" },
  ]} />;
}

// ── 狼人杀可视化对局(网杀圆桌 + 事件时间线 + AI 思考) ──────

const WW_EVENT_META: Record<string, { label: string; color: string }> = {
  phase: { label: "阶段", color: "geekblue" },
  announce: { label: "公告", color: "default" },
  death: { label: "出局", color: "red" },
  speech: { label: "发言", color: "green" },
  vote_tally: { label: "计票", color: "gold" },
  ai_decision: { label: "AI 思考", color: "purple" },
  ai_speech: { label: "AI 发言稿", color: "purple" },
  system: { label: "系统", color: "red" },
};

function WerewolfGameDrawer({ groupId, reveal, onClose }: { groupId: number | null; reveal: boolean; onClose: () => void }): React.JSX.Element {
  const [detail, setDetail] = useState<WerewolfGameDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 全量拉取(内存日志 ≤500 条);打开期间 2.5s 轮询,页面隐藏时暂停。
  const load = useCallback(() => {
    if (groupId === null) return Promise.resolve(null);
    return api<WerewolfGameDetail>(`/games/werewolf/${groupId}/events`).then((r) => r.data);
  }, [groupId]);
  useEffect(() => {
    if (groupId === null) { setDetail(null); setError(null); return; }
    let alive = true;
    const tick = async () => {
      if (document.hidden) return;
      try {
        const data = await load();
        if (alive) { setDetail(data); setError(null); }
      } catch (reason) {
        if (alive) setError(reason instanceof Error ? reason.message : "加载失败");
      }
    };
    void tick();
    const timer = window.setInterval(tick, 2500);
    document.addEventListener("visibilitychange", tick);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [load, groupId]);
  const game = detail?.game ?? null;
  return <Drawer
    open={groupId !== null}
    onClose={onClose}
    width="min(1080px, 100%)"
    title={<Space>
      {game ? groupTitle(game.groupName, game.groupId) : "对局"}
      {game && phaseTag(game.phase, game.phaseLabel, WW_PHASE_COLORS)}
      {game && !game.workerAlive && <Tag color="red">引擎停摆</Tag>}
    </Space>}>
    {error && !game && <QueryErrorAlert error={error} onRetry={() => { void load().then((data) => { if (data) setDetail(data); }).catch(() => undefined); }} />}
    {!game && !error && <Spin />}
    {game && <>
      <WerewolfTable game={game} reveal={reveal} />
      <WerewolfTimeline events={detail?.events ?? []} />
    </>}
  </Drawer>;
}

function WerewolfTable({ game, reveal }: { game: WerewolfLiveGame; reveal: boolean }): React.JSX.Element {
  const ended = game.phase === "ENDED";
  const showRole = reveal || ended;
  const night = wwIsNight(game.phase);
  const speaker = game.players.find((p) => p.seat === game.currentSpeaker) ?? null;
  const total = Math.max(game.players.length, 1);
  return <div className={`ww-arena${night ? " night" : ""}`}>
    <div className="ww-table" role="img" aria-label="狼人杀圆桌">
      <div className="ww-sky" aria-hidden="true">
        <span className="ww-moon" />
        <span className="ww-sun" />
        {WW_STARS.map((star, index) => (
          <span key={index} className="ww-star"
            style={{ left: `${star.x}%`, top: `${star.y}%`, animationDelay: `${star.delay}s`, width: star.size, height: star.size }} />
        ))}
      </div>
      <div className="ww-petals" aria-hidden="true">
        {WW_PETALS.map((petal, index) => (
          <span key={index} className="ww-petal"
            style={{ left: `${petal.x}%`, animationDelay: `${petal.delay}s`, animationDuration: `${petal.duration}s` }} />
        ))}
      </div>
      {game.players.map((p) => {
        const pos = wwSeatPosition(p.seat, total);
        return <div key={p.userId} className={`ww-seat${p.alive ? "" : " dead"}${p.seat === game.currentSpeaker ? " speaking" : ""}`}
          style={{ left: `${pos.x}%`, top: `${pos.y}%` }}>
          <SeatAvatar player={p} showRole={showRole} />
        </div>;
      })}
      <div className="ww-center">
        <div className="ww-board">{game.board ?? "—"}</div>
        <div className="ww-round">第 {game.roundNo} 天</div>
        <div className={`ww-phase${night ? " night" : ""}`}>{night ? "🌙 天黑请闭眼" : "☀️ 白天"}</div>
        {speaker && <div className="ww-speaker">🎤 {speaker.seat}号 {speaker.name} 发言中</div>}
        <div className="ww-counts">存活 {game.aliveCount} / {game.playerCount}{game.aiCount > 0 && ` · AI ${game.aiCount}`}</div>
      </div>
    </div>
    <div className="ww-seats-mobile">
      {game.players.map((p) => <div key={p.userId} className={`ww-seat-card${p.alive ? "" : " dead"}${p.seat === game.currentSpeaker ? " speaking" : ""}`}>
        <SeatAvatar player={p} showRole={showRole} />
      </div>)}
    </div>
  </div>;
}

function SeatAvatar({ player, showRole }: { player: WerewolfLivePlayer; showRole: boolean }): React.JSX.Element {
  const factionClass = !showRole ? "" : player.faction === "wolf" ? " faction-wolf" : " faction-good";
  return <Tooltip title={<div className="ww-seat-tip">
    <div>{player.seat}号 {player.name}{player.isAi && " (AI)"}</div>
    <div>{showRole ? `${player.role ?? "—"} · ${player.faction === "wolf" ? "狼人阵营" : "好人阵营"}` : "身份隐藏中"}</div>
    {!player.alive && <div>第 {player.deathRound ?? "?"} 轮 · {WW_DEATH_CAUSE[player.deathCause ?? ""] ?? player.deathCause ?? "出局"}</div>}
  </div>}>
    <div className={`ww-avatar${factionClass}`}>
      <span className="ww-avatar-char">{player.name.slice(0, 1) || "?"}</span>
      {player.isSheriff && <span className="ww-badge-sheriff" title="警长">⭐</span>}
      {!player.alive && <span className="ww-badge-dead" title="出局">✕</span>}
      {player.isAi && <span className="ww-badge-ai">AI</span>}
    </div>
    <div className="ww-seat-label">{player.seat}号 {player.name}</div>
    {showRole && <div className={`ww-seat-role${factionClass}`}>{player.role ?? "—"}</div>}
  </Tooltip>;
}

function WerewolfTimeline({ events }: { events: WerewolfGameEvent[] }): React.JSX.Element {
  const [filter, setFilter] = useState<string>("all");
  const shown = events.filter((e) => {
    if (filter === "all") return true;
    if (filter === "ai") return e.type === "ai_decision" || e.type === "ai_speech";
    if (filter === "talk") return e.type === "speech";
    return e.type === filter;
  }).slice().reverse(); // 最新在上
  return <Card className="section-row ww-timeline" title="对局时间线" extra={
    <Segmented size="small" value={filter} onChange={(v) => setFilter(v as string)} options={[
      { value: "all", label: "全部" },
      { value: "talk", label: "发言" },
      { value: "vote_tally", label: "计票" },
      { value: "ai", label: "AI 思考" },
      { value: "death", label: "出局" },
      { value: "system", label: "系统" },
    ]} />
  }>
    {shown.length === 0 ? <Empty description="暂无事件" /> : <Timeline items={shown.map((event) => ({
      color: WW_EVENT_META[event.type]?.color === "default" ? "gray" : WW_EVENT_META[event.type]?.color ?? "gray",
      dot: <span className={`ww-dot ww-dot-${event.type}`} />,
      children: <div className="ww-event">
        <div className="ww-event-head">
          <Tag color={WW_EVENT_META[event.type]?.color ?? "default"}>{WW_EVENT_META[event.type]?.label ?? event.type}</Tag>
          {event.seat !== null && <Text strong>{event.seat}号{event.name ? ` ${event.name}` : ""}</Text>}
          {event.type === "speech" && event.extra.isAi && <Tag className="inline-tag">AI</Tag>}
          <Text type="secondary">{formatTime(event.ts)}</Text>
          {event.roundNo !== null && <Text type="secondary">第 {event.roundNo} 天</Text>}
        </div>
        <WerewolfEventBody event={event} />
      </div>,
    }))} />}
  </Card>;
}

function WerewolfEventBody({ event }: { event: WerewolfGameEvent }): React.JSX.Element {
  if (event.type === "vote_tally") {
    const votes = event.extra.votes ?? [];
    return <div className="ww-votes">
      {votes.map((vote) => <Tag key={vote.voterSeat} color={vote.targetSeat === null ? "default" : "gold"}>
        {vote.voterSeat}号 → {vote.targetSeat === null ? "弃票" : `${vote.targetSeat}号`}{vote.isSheriff ? " ×1.5" : ""}
      </Tag>)}
      {votes.length === 0 && <Text type="secondary">无人投票</Text>}
    </div>;
  }
  if (event.type === "ai_decision" || event.type === "ai_speech") {
    const context = event.extra.context;
    return <div className="ww-ai-event">
      {event.extra.instruction && <div className="ww-ai-instruction">指令:{event.extra.instruction}</div>}
      <div className="ww-ai-reply">💬 {event.text ?? "(空回复)"}</div>
      {event.type === "ai_decision" && event.extra.action && <div>
        <Tag color={event.extra.action.value === null ? "default" : "green"}>
          解析行动:{event.extra.action.kind}{event.extra.action.value !== null ? ` → ${event.extra.action.value}号` : ""}
        </Tag>
        {event.extra.attempt !== undefined && event.extra.attempt > 1 && <Tag color="orange">第 {event.extra.attempt} 次纠正</Tag>}
      </div>}
      {context && <Collapse size="small" ghost items={[{
        key: "ctx",
        label: <Text type="secondary">展开决策上下文(喂给 AI 的局势快照)</Text>,
        children: <pre className="ww-ai-context">{context}</pre>,
      }]} />}
    </div>;
  }
  if (event.type === "death") {
    return <div>{event.text}{event.extra.role && <Tag className="inline-tag" color={event.extra.role === "werewolf" ? "red" : "green"}>{event.extra.role}</Tag>}</div>;
  }
  return <div className="ww-event-text">{event.text ?? "—"}</div>;
}

// ── 跑团 ─────────────────────────────────────────────────

function RpgTab({ live, onChanged }: { live: LiveGames["rpg"]; onChanged: () => void }): React.JSX.Element {
  const { stopping, stop } = useStopGame("rpg", onChanged);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const loadHistory = useCallback(() => api<RpgHistoryGame[]>(`/games/rpg/history?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&status=${status}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search, status]);
  const historyQuery = useApiQuery(loadHistory);
  if (!live.available) return <PluginUnavailable kind="跑团" />;
  const columns: ColumnsType<RpgHistoryGame> = [
    { title: "群", render: (_, row) => <><GroupCell group={row} /></> },
    { title: "模组", render: (_, row) => <>{row.moduleName}<br /><Text type="secondary">{row.moduleId}</Text></> },
    { title: "人数", dataIndex: "playerCount", width: 70 },
    { title: "结局", width: 130, render: (_, row) => {
      if (!row.endedAt) return <Tag color="blue">进行中</Tag>;
      const outcome = row.outcome ? RPG_OUTCOMES[row.outcome] : undefined;
      return outcome ? <Tag color={outcome.color}>{outcome.label}</Tag> : <Tag>已结束</Tag>;
    } },
    { title: "终止原因", width: 100, render: (_, row) => row.terminationReason ? RPG_TERMINATION[row.terminationReason] ?? row.terminationReason : "—" },
    { title: "开始时间", dataIndex: "startedAt", render: formatTime },
    { title: "结束时间", dataIndex: "endedAt", render: formatTime },
  ];
  return <>
    <Flex justify="space-between" align="center" className="live-heading"><Text strong>实时对局({live.games.length} 局)</Text></Flex>
    {live.games.length === 0 ? <Empty description="当前没有进行中的跑团对局" /> : (
      <Row gutter={[16, 16]}>
        {live.games.map((game) => <RpgLiveCard key={game.groupId} game={game} stopping={stopping === game.groupId} onStop={() => stop(game.groupId)} />)}
      </Row>
    )}
    <Card className="section-row" title="对局战绩" extra={<Space>
      <Input.Search placeholder="搜索群号或房主" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />
      <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }} options={STATUS_OPTIONS} />
    </Space>}>
      {historyQuery.error && !historyQuery.data
        ? <QueryErrorAlert error={historyQuery.error} onRetry={historyQuery.reload} />
        : <Table rowKey="id" size="small" columns={columns} loading={historyQuery.loading} dataSource={historyQuery.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: historyQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <RpgHistoryPlayers row={row} /> }} />}
    </Card>
  </>;
}

function RpgLiveCard({ game, stopping, onStop }: { game: RpgLiveGame; stopping: boolean; onStop: () => Promise<void> }): React.JSX.Element {
  return <Col xs={24} xl={12}>
    <Card title={<Space>{groupTitle(game.groupName, game.groupId)}{phaseTag(game.phase, game.phaseLabel, RPG_PHASE_COLORS)}{!game.workerAlive && <Tag color="red">引擎停摆</Tag>}</Space>}
      extra={<Popconfirm title="强制结束这局跑团?" description="走子插件 stop_game,与群内结束命令同一路径。" onConfirm={onStop}><Button danger size="small" icon={<StopOutlined />} loading={stopping}>强制结束</Button></Popconfirm>}>
      <Descriptions size="small" column={3} items={[
        { key: "module", label: "模组", children: game.moduleName ?? "—" },
        { key: "scene", label: "场景", children: game.sceneId ?? "—" },
        { key: "clock", label: "时钟", children: game.clockText },
        { key: "explore", label: "探索轮", children: game.exploreRound },
        { key: "combat", label: "战斗轮", children: game.combatRound ?? "—" },
        { key: "actor", label: "当前行动", children: game.currentActorUserId ?? "—" },
        { key: "count", label: "玩家/报名", children: `${game.playerCount} / ${game.signupCount}` },
        { key: "queue", label: "队列/待处理", children: `${game.queueDepth} / ${game.pendingCount}` },
        { key: "tools", label: "损坏工具", children: game.toolsBroken },
      ]} />
      <Table size="small" rowKey="userId" pagination={false} dataSource={game.players} className="live-players" columns={[
        { title: "座", dataIndex: "seat", width: 44 },
        { title: "玩家", render: (_, p) => <>{p.userId}<br /><Text type="secondary">{p.charName ?? "建卡中"}</Text></> },
        { title: "建卡", width: 80, render: (_, p) => p.confirmed ? <Tag color="green">已确认</Tag> : <Tag color="orange">待确认</Tag> },
        { title: "状态", width: 80, render: (_, p) => p.incapped ? <Tag color="red">倒下</Tag> : <Tag color="green">正常</Tag> },
      ]} />
    </Card>
  </Col>;
}

function RpgHistoryPlayers({ row }: { row: RpgHistoryGame }): React.JSX.Element {
  return <Table size="small" rowKey="userId" pagination={false} dataSource={row.players} columns={[
    { title: "玩家", dataIndex: "userId" },
    { title: "角色", dataIndex: "charName" },
    { title: "HP", width: 120, render: (_, p) => `${p.startHp} → ${p.finalHp ?? "—"}` },
    { title: "SAN", width: 120, render: (_, p) => `${p.startSan} → ${p.finalSan ?? "—"}` },
    { title: "状态", width: 90, render: (_, p) => p.survived === null ? (p.isIncapped ? <Tag color="red">倒下</Tag> : <Tag color="green">正常</Tag>) : p.survived ? <Tag color="green">幸存</Tag> : <Tag color="red">出局</Tag> },
  ]} />;
}
