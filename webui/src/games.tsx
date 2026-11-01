import {
  DesktopOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  MoonOutlined,
  PlayCircleOutlined,
  SendOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Steps,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import { WerewolfWorkspace } from "./werewolf/WerewolfWorkspace";
import type {
  LiveGames,
  RpgHistoryGame,
  RpgActionKind,
  RpgGameDetail,
  RpgLiveGame,
  RpgPlayerPrivate,
  RpgReplay,
} from "./types";

const { Text } = Typography;

function GameOpsFlow({ current }: { current: 0 | 1 | 2 | 3 }): React.JSX.Element {
  return <Card size="small" className="section-row">
    <Steps
      size="small"
      current={current}
      responsive
      items={[
        { title: "实时对局", description: "发现与进入" },
        { title: "对局详情", description: "局面与管理员视角" },
        { title: "事件时间线", description: "过程与异常定位" },
        { title: "历史回放", description: "终局复盘" },
      ]}
    />
  </Card>;
}

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

export function rpgActionOptions(phase: string | null): { value: RpgActionKind; label: string }[] {
  if (phase === "PLAY") {
    return [
      { value: "SAY", label: "发言" },
      { value: "WAIT", label: "等待" },
      { value: "PASS_TURN", label: "结束行动" },
    ];
  }
  if (phase === "SIGNUP") {
    return [
      { value: "MODULE_SELECT", label: "选择模组" },
      { value: "START_GAME", label: "开始游戏" },
    ];
  }
  return [];
}

function groupTitle(name?: string | null, groupId?: number): string {
  return name || (groupId !== undefined ? `群 ${groupId}` : "未知群");
}

function phaseTag(phase: string | null, label: string, colors: Record<string, string>): React.JSX.Element {
  return <Tag color={colors[phase ?? ""] ?? "default"}>{label || phase || "—"}</Tag>;
}

export function GamesPage(): React.JSX.Element {
  const load = useCallback(() => api<LiveGames>("/games/live").then((r) => r.data), []);
  const query = useApiQuery(load, { resources: ["werewolf_game", "rpg_game"] });
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
  if (!live) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  return <>
    <PageHeader
      title="对局中心"
      subtitle={tab === "werewolf" ? "狼人杀：实时观战、战绩、数据洞察与规则配置" : "跑团：实时对局、行动投递与历史回放"}
      onRefresh={tab === "rpg" ? query.reload : undefined}
      refreshing={query.refreshing}
    />
    <Tabs activeKey={tab} onChange={(key) => setSearchParams(key === "werewolf" ? {} : { tab: key }, { replace: true })} items={[
      { key: "werewolf", label: <span><MoonOutlined /> 狼人杀{live.werewolf.games.length ? ` · ${live.werewolf.games.length}` : ""}</span>, children: <WerewolfWorkspace available={live.werewolf.available} games={live.werewolf.games} onChanged={reload} refreshing={query.refreshing} /> },
      { key: "rpg", label: <span><PlayCircleOutlined /> 跑团{live.rpg.games.length ? ` · ${live.rpg.games.length}` : ""}</span>, children: <RpgTab live={live.rpg} onChanged={reload} /> },
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

function GroupCell({ group }: { group: { groupId: number; groupName?: string | null; hostUserId: number } }): React.JSX.Element {
  return <>{groupTitle(group.groupName, group.groupId)}<br /><Text type="secondary">房主 {group.hostUserId}</Text></>;
}

// ── 跑团 ─────────────────────────────────────────────────

function RpgTab({ live, onChanged }: { live: LiveGames["rpg"]; onChanged: () => void }): React.JSX.Element {
  const { stopping, stop } = useStopGame("rpg", onChanged);
  const [showPrivate, setShowPrivate] = useState(false);
  const [viewGroupId, setViewGroupId] = useState<number | null>(null);
  const [replayRowId, setReplayRowId] = useState<number | null>(null);
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
    { title: "操作", width: 80, render: (_, row) => row.endedAt && row.eventLogId
      ? <Button type="link" onClick={() => setReplayRowId(row.id)}>回放</Button>
      : <Text type="secondary">—</Text> },
  ];
  return <>
    <GameOpsFlow current={0} />
    <Flex justify="space-between" align="center" className="live-heading">
      <Text strong>实时对局({live.games.length} 局)</Text>
      <Space>
        <Text type="secondary">{showPrivate ? <EyeOutlined /> : <EyeInvisibleOutlined />} 管理台显示私密状态</Text>
        <Switch size="small" checked={showPrivate} onChange={setShowPrivate} />
      </Space>
    </Flex>
    {live.games.length === 0 ? <Empty description="当前没有进行中的跑团对局" /> : (
      <Row gutter={[16, 16]}>
        {live.games.map((game) => <RpgLiveCard key={game.groupId} game={game} stopping={stopping === game.groupId} onStop={() => stop(game.groupId)} onView={() => setViewGroupId(game.groupId)} />)}
      </Row>
    )}
    <RpgGameDrawer groupId={viewGroupId} reveal={showPrivate} onClose={() => setViewGroupId(null)} />
    <Card className="section-row" title="历史对局与回放" extra={<Space>
      <Input.Search placeholder="搜索群号或房主" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />
      <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }} options={STATUS_OPTIONS} />
    </Space>}>
      {historyQuery.error && !historyQuery.data
        ? <QueryErrorAlert error={historyQuery.error} onRetry={historyQuery.reload} />
        : <Table rowKey="id" size="small" columns={columns} loading={historyQuery.loading} dataSource={historyQuery.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: historyQuery.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <RpgHistoryPlayers row={row} /> }} />}
    </Card>
    <RpgReplayDrawer rowId={replayRowId} onClose={() => setReplayRowId(null)} />
  </>;
}

function RpgLiveCard({ game, stopping, onStop, onView }: { game: RpgLiveGame; stopping: boolean; onStop: () => Promise<void>; onView: () => void }): React.JSX.Element {
  return <Col xs={24} xl={12}>
    <Card title={<Space>{groupTitle(game.groupName, game.groupId)}{phaseTag(game.phase, game.phaseLabel, RPG_PHASE_COLORS)}{!game.workerAlive && <Tag color="red">引擎停摆</Tag>}</Space>}
      extra={<Space><Button size="small" icon={<DesktopOutlined />} onClick={onView}>进入对局</Button><Popconfirm title="强制结束这局跑团?" description="走子插件 stop_game,与群内结束命令同一路径。" onConfirm={onStop}><Button danger size="small" icon={<StopOutlined />} loading={stopping}>强制结束</Button></Popconfirm></Space>}>
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

function RpgGameDrawer({ groupId, reveal, onClose }: { groupId: number | null; reveal: boolean; onClose: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [detail, setDetail] = useState<RpgGameDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [privateData, setPrivateData] = useState<Record<number, RpgPlayerPrivate>>({});
  const [privateLoading, setPrivateLoading] = useState<Record<number, boolean>>({});
  const [expandedRows, setExpandedRows] = useState<number[]>([]);
  const [actionKind, setActionKind] = useState<RpgActionKind | "">("");
  const [actionUserId, setActionUserId] = useState<number | null>(null);
  const [actionText, setActionText] = useState("");
  const [minutes, setMinutes] = useState(30);
  const load = useCallback(() => {
    if (groupId === null) return Promise.resolve(null);
    return api<RpgGameDetail>(`/games/rpg/${groupId}/detail`).then((r) => r.data);
  }, [groupId]);
  useEffect(() => {
    if (groupId === null) {
      setDetail(null);
      setError(null);
      setPrivateData({});
      setExpandedRows([]);
      return;
    }
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
  const options = rpgActionOptions(game?.phase ?? null);
  const playerOptions = game?.phase === "SIGNUP"
    ? [{ value: game.hostUserId, label: `房主 ${game.hostUserId}` }]
    : (detail?.players ?? []).map((player) => ({ value: player.userId, label: `${player.seat}号 · ${player.charName ?? player.userId}` }));
  useEffect(() => {
    const validKinds = options.map((item) => item.value);
    if (!validKinds.includes(actionKind as RpgActionKind)) setActionKind(validKinds[0] ?? "");
    if (actionUserId === null || !playerOptions.some((item) => item.value === actionUserId)) setActionUserId(playerOptions[0]?.value ?? null);
  }, [game?.phase, detail?.players, actionKind, actionUserId, options, playerOptions]);
  const loadPrivate = async (userId: number) => {
    if (!reveal || privateData[userId] || privateLoading[userId] || groupId === null) return;
    setPrivateLoading((current) => ({ ...current, [userId]: true }));
    try {
      const result = await api<RpgPlayerPrivate>(`/games/rpg/${groupId}/players/${userId}/private`);
      setPrivateData((current) => ({ ...current, [userId]: result.data }));
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "私密信息加载失败");
    } finally {
      setPrivateLoading((current) => ({ ...current, [userId]: false }));
    }
  };
  const submit = async () => {
    if (groupId === null || !actionKind || actionUserId === null) return;
    const payload: { userId: number; kind: RpgActionKind; text?: string; minutes?: number } = { userId: actionUserId, kind: actionKind };
    if (["SAY", "MODULE_SELECT"].includes(actionKind)) payload.text = actionText;
    if (actionKind === "WAIT") payload.minutes = minutes;
    try {
      await api(`/games/rpg/${groupId}/actions`, { method: "POST", body: JSON.stringify(payload) });
      message.success("行动已入队");
      setActionText("");
      const data = await load();
      if (data) setDetail(data);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "行动投递失败");
    }
  };
  const actionLabel = options.find((item) => item.value === actionKind)?.label ?? "行动";
  return <Drawer open={groupId !== null} onClose={onClose} width="min(1120px, 100%)" title={<Space>{game ? groupTitle(game.groupName, game.groupId) : "跑团对局"}{game && phaseTag(game.phase, game.phaseLabel, RPG_PHASE_COLORS)}{game && !game.workerAlive && <Tag color="red">引擎停摆</Tag>}</Space>}>
    {error && !game && <QueryErrorAlert error={error} onRetry={() => { void load().then((data) => { if (data) setDetail(data); }).catch(() => undefined); }} />}
    {!game && !error && <Spin />}
    {game && detail && <>
      <GameOpsFlow current={2} />
      <Descriptions className="section-row" size="small" column={3} items={[
        { key: "module", label: "模组", children: game.moduleName ?? "—" },
        { key: "scene", label: "场景", children: game.sceneId ?? "—" },
        { key: "clock", label: "游戏时钟", children: game.clockText },
        { key: "explore", label: "探索轮", children: game.exploreRound },
        { key: "combat", label: "战斗轮", children: game.combatRound ?? "—" },
        { key: "actor", label: "当前行动者", children: game.currentActorUserId ?? "—" },
        { key: "queue", label: "队列/待处理", children: `${game.queueDepth} / ${game.pendingCount}` },
        { key: "signup", label: "报名人数", children: game.signupCount },
        { key: "worker", label: "引擎", children: game.workerAlive ? <Tag color="green">运行中</Tag> : <Tag color="red">停摆</Tag> },
      ]} />
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={24} lg={12}><Card size="small" title="公共局面"><pre>{detail.situationText || "暂无"}</pre></Card></Col>
        <Col xs={24} lg={12}><Card size="small" title="线索板"><pre>{detail.clueBoardText || "暂无"}</pre></Card></Col>
      </Row>
      {detail.pendingDeduction && <Card size="small" className="section-row" title="待确认联合推理"><Descriptions column={2} size="small" items={[
        { key: "proposer", label: "提案者", children: detail.pendingDeduction.proposerUserId },
        { key: "clues", label: "线索", children: detail.pendingDeduction.clueIds.join("、") || "—" },
        { key: "conclusion", label: "结论", children: detail.pendingDeduction.conclusion },
        { key: "confirmations", label: "已确认", children: detail.pendingDeduction.confirmations.join("、") || "—" },
      ]} /></Card>}
      <Card size="small" className="section-row" title="玩家状态" extra={<Text type="secondary">私密信息仅在开关开启并展开行后加载</Text>}>
        <Table
          size="small"
          rowKey="userId"
          pagination={false}
          dataSource={detail.players}
          expandable={{
            expandedRowKeys: expandedRows,
            onExpand: (expanded, player) => {
              setExpandedRows((current) => expanded ? [...current, player.userId] : current.filter((id) => id !== player.userId));
              if (expanded) void loadPrivate(player.userId);
            },
            expandedRowRender: (player) => {
              if (!reveal) return <Text type="secondary">开启“管理台显示私密状态”后才能查看。</Text>;
              if (privateLoading[player.userId]) return <Spin size="small" />;
              const data = privateData[player.userId];
              if (!data) return <Text type="secondary">展开以加载私密局面。</Text>;
              return <Descriptions column={1} size="small" items={[{ key: "situation", label: "私密局面", children: <pre>{data.situationText || "暂无"}</pre> }, { key: "journal", label: "调查手记", children: <pre>{data.journalText || "暂无"}</pre> }]} />;
            },
          }}
          columns={[
            { title: "座", dataIndex: "seat", width: 44 },
            { title: "玩家/角色", render: (_, player) => <>{player.userId}<br /><Text type="secondary">{player.charName ?? "建卡中"}</Text></> },
            { title: "建卡", width: 80, render: (_, player) => player.confirmed ? <Tag color="green">已确认</Tag> : <Tag color="orange">待确认</Tag> },
            { title: "HP", width: 80, render: (_, player) => reveal ? player.hp : "🔒 隐藏" },
            { title: "SAN", width: 80, render: (_, player) => reveal ? player.san : "🔒 隐藏" },
            { title: "重掷", width: 70, render: (_, player) => reveal ? player.rerollsLeft : "🔒" },
            { title: "私聊", width: 70, render: (_, player) => player.dmOk ? <Tag color="green">正常</Tag> : <Tag color="orange">失败</Tag> },
            { title: "状态", width: 80, render: (_, player) => player.incapped ? <Tag color="red">倒下</Tag> : <Tag color="green">正常</Tag> },
          ]}
        />
      </Card>
      <Card size="small" className="section-row" title="行动投递" extra={<Tag color="blue">submit_action → 引擎单写</Tag>}>
        {options.length === 0 ? <Text type="secondary">当前阶段没有可从管理台投递的行动。</Text> : <Space wrap>
          <Select aria-label="行动玩家" value={actionUserId ?? undefined} onChange={setActionUserId} options={playerOptions} disabled={game.phase === "SIGNUP"} style={{ minWidth: 180 }} />
          <Select aria-label="行动类型" value={actionKind || undefined} onChange={(value) => setActionKind(value as RpgActionKind)} options={options} style={{ minWidth: 130 }} />
          {["SAY", "MODULE_SELECT"].includes(actionKind) && <Input aria-label={`${actionLabel}文本`} value={actionText} onChange={(event) => setActionText(event.target.value)} placeholder={actionKind === "MODULE_SELECT" ? "模组 id 或编号" : "代玩家发言"} style={{ width: 240 }} />}
          {actionKind === "WAIT" && <InputNumber aria-label="等待分钟" min={1} max={1440} value={minutes} onChange={(value) => setMinutes(value ?? 1)} addonAfter="分钟" />}
          <Button type="primary" icon={<SendOutlined />} onClick={() => { void submit(); }}>投递</Button>
        </Space>}
      </Card>
      <Card size="small" className="section-row" title="事件时间线" extra={<Tag color="processing">2.5 秒实时刷新</Tag>}>
        {detail.groupLog.length === 0 ? <Empty description="暂无日志" /> : <Timeline items={detail.groupLog.slice().reverse().map((line, index) => ({ key: index, children: <pre>{line}</pre> }))} />}
      </Card>
    </>}
  </Drawer>;
}

function RpgReplayDrawer({ rowId, onClose }: { rowId: number | null; onClose: () => void }): React.JSX.Element {
  const [replay, setReplay] = useState<RpgReplay | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (rowId === null) { setReplay(null); setError(null); return; }
    let alive = true;
    setReplay(null);
    setError(null);
    api<RpgReplay>(`/games/rpg/history/${rowId}/replay`)
      .then((result) => { if (alive) setReplay(result.data); })
      .catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : "回放加载失败"); });
    return () => { alive = false; };
  }, [rowId]);
  return <Drawer open={rowId !== null} onClose={onClose} width="min(900px, 100%)" title={replay?.title ?? "跑团回放"}>
    {error && <Alert type="error" showIcon message="回放加载失败" description={error} />}
    {!replay && !error && <Spin />}
    {replay && !replay.available && <Alert type="warning" showIcon message="本局不可回放" description={replay.reason ?? "事件日志不完整"} />}
    {replay && replay.available && <>
      <GameOpsFlow current={3} />
      {replay.warnings.map((warning) => <Alert key={warning} className="section-alert" type="warning" showIcon message={warning} />)}
      <Descriptions className="section-row" size="small" column={2} items={[
        { key: "id", label: "回放编号", children: replay.game_id },
        { key: "module", label: "模组", children: replay.summary.module_id ?? "—" },
        { key: "scene", label: "末场景", children: replay.summary.last_scene ?? "—" },
        { key: "outcome", label: "结局", children: replay.summary.outcome ?? replay.summary.ending_id ?? "—" },
        { key: "started", label: "开始", children: formatTime(replay.started_at) },
        { key: "ended", label: "结束", children: formatTime(replay.ended_at) },
      ]} />
      {replay.events.length === 0 ? <Empty description="暂无可见事件" /> : <Timeline items={replay.events.map((event) => ({ key: event.sequence, color: event.audience === "public" ? "blue" : "purple", children: <Space direction="vertical" size={0}><Space><Tag>{event.event_type}</Tag><Text type="secondary">#{event.sequence} · {formatTime(event.occurred_at)}</Text></Space><Text>{event.detail}</Text></Space> }))} />}
    </>}
  </Drawer>;
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
