import {
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
  Descriptions,
  Empty,
  Flex,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { formatTime, PageHeader, useEntityRefresh } from "./shared";
import type {
  LiveGames,
  RpgHistoryGame,
  RpgLiveGame,
  WerewolfHistoryGame,
  WerewolfLiveGame,
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

export function GamesPage(): React.JSX.Element {
  const [live, setLive] = useState<LiveGames | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api<LiveGames>("/games/live").then((r) => setLive(r.data)).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  useEntityRefresh(load);
  // 实时对局只有强停会推送 entity.changed,阶段流转靠轮询兜底。
  useEffect(() => {
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);
  if (!live) return error ? <Alert type="error" showIcon message={error} /> : <Spin />;
  return <>
    <PageHeader title="对局中心" subtitle="狼人杀与跑团的实时监控与战绩(只读口径,强停走子插件状态机)" />
    <Tabs items={[
      { key: "werewolf", label: <span><MoonOutlined /> 狼人杀</span>, children: <WerewolfTab live={live.werewolf} onChanged={load} /> },
      { key: "rpg", label: <span><PlayCircleOutlined /> 跑团</span>, children: <RpgTab live={live.rpg} onChanged={load} /> },
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
  const { stopping, stop } = useStopGame("werewolf", onChanged);
  const [rows, setRows] = useState<WerewolfHistoryGame[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const loadHistory = useCallback(() => api<WerewolfHistoryGame[]>(`/games/werewolf/history?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&status=${status}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [page, search, status]);
  useEffect(() => { void loadHistory(); }, [loadHistory]);
  useEntityRefresh(loadHistory);
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
        {live.games.map((game) => <WerewolfLiveCard key={game.groupId} game={game} reveal={reveal} stopping={stopping === game.groupId} onStop={() => stop(game.groupId)} />)}
      </Row>
    )}
    <Card className="section-row" title="对局战绩" extra={<Space>
      <Input.Search placeholder="搜索群号或房主" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />
      <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }} options={STATUS_OPTIONS} />
    </Space>}>
      <Table rowKey="id" size="small" columns={columns} dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <WerewolfHistoryPlayers row={row} /> }} />
    </Card>
  </>;
}

function GroupCell({ group }: { group: { groupId: number; groupName?: string | null; hostUserId: number } }): React.JSX.Element {
  return <>{groupTitle(group.groupName, group.groupId)}<br /><Text type="secondary">房主 {group.hostUserId}</Text></>;
}

function WerewolfLiveCard({ game, reveal, stopping, onStop }: { game: WerewolfLiveGame; reveal: boolean; stopping: boolean; onStop: () => Promise<void> }): React.JSX.Element {
  const ended = game.phase === "ENDED";
  return <Col xs={24} xl={12}>
    <Card title={<Space>{groupTitle(game.groupName, game.groupId)}{phaseTag(game.phase, game.phaseLabel, WW_PHASE_COLORS)}{!game.workerAlive && <Tag color="red">引擎停摆</Tag>}</Space>}
      extra={<Popconfirm title="强制结束这局狼人杀?" description="走子插件 stop_game,与群内结束命令同一路径。" onConfirm={onStop}><Button danger size="small" icon={<StopOutlined />} loading={stopping}>强制结束</Button></Popconfirm>}>
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

// ── 跑团 ─────────────────────────────────────────────────

function RpgTab({ live, onChanged }: { live: LiveGames["rpg"]; onChanged: () => void }): React.JSX.Element {
  const { stopping, stop } = useStopGame("rpg", onChanged);
  const [rows, setRows] = useState<RpgHistoryGame[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const loadHistory = useCallback(() => api<RpgHistoryGame[]>(`/games/rpg/history?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&status=${status}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [page, search, status]);
  useEffect(() => { void loadHistory(); }, [loadHistory]);
  useEntityRefresh(loadHistory);
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
      <Table rowKey="id" size="small" columns={columns} dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <RpgHistoryPlayers row={row} /> }} />
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
