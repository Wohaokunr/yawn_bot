// 战绩：筛选条（搜索/板子/结果/状态/群/玩家）+ 对局表 + 单局详情抽屉 + 回放查询。
import { HistoryOutlined, SearchOutlined } from "@ant-design/icons";
import { Alert, Button, Drawer, Empty, Input, Select, Skeleton, Table, Tag, Timeline, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { WwHistoryGame, WwMeta, WwReplay } from "./types";
import { groupTitle } from "./LiveView";
import { wwApi } from "./ww-api";
import { deathLabel, deathOrder, formatDuration, gameDuration, roleInfo, roundLabel, shortDateTime } from "./ww-meta";
import { RatioBar, RoleTag, SeatNo } from "./WwBits";

const { Text } = Typography;
export const HISTORY_PAGE_SIZE = 20;

export interface HistoryFilters {
  page: number;
  search: string;
  status: string;
  board: string;
  winner: string;
  groupId: number | null;
  userId: number | null;
  game: number | null;
}

export function ResultTag({ row }: { row: Pick<WwHistoryGame, "winnerFaction" | "endedAt"> }): React.JSX.Element {
  if (row.winnerFaction === "wolf") return <Tag color="red">🐺 狼人胜</Tag>;
  if (row.winnerFaction === "good") return <Tag color="green">🛡️ 好人胜</Tag>;
  return row.endedAt ? <Tag>无结果</Tag> : <Tag color="default">未完成</Tag>;
}

export function HistoryView({ filters, meta, onChange }: { filters: HistoryFilters; meta: WwMeta | null; onChange: (patch: Partial<HistoryFilters>) => void }): React.JSX.Element {
  const [searchDraft, setSearchDraft] = useState(filters.search);
  const [replayOpen, setReplayOpen] = useState(false);
  useEffect(() => setSearchDraft(filters.search), [filters.search]);
  const query = useApiQuery({
    queryKey: ["ww-history", filters.page, filters.search, filters.status, filters.board, filters.winner, filters.groupId, filters.userId],
    fetcher: () => wwApi.history({ ...filters, pageSize: HISTORY_PAGE_SIZE }),
    invalidation: { resources: ["werewolf_game"] },
  });
  const set = (patch: Partial<HistoryFilters>) => onChange({ page: 1, ...patch });
  const anyFilter = Boolean(filters.search || filters.status !== "all" || filters.board || filters.winner || filters.groupId || filters.userId);
  const columns: ColumnsType<WwHistoryGame> = [
    { title: "开始", dataIndex: "startedAt", width: 110, render: (value: string | null) => <span title={formatTime(value)}>{shortDateTime(value)}</span> },
    { title: "群", render: (_, row) => <><strong>{groupTitle(row.groupName, row.groupId)}</strong><br /><Text type="secondary">{row.groupId}</Text></> },
    { title: "真人玩家", responsive: ["lg"], render: (_, row) => {
      const humans = row.players.filter((player) => !player.isAi);
      if (humans.length === 0) return <Text type="secondary">全 AI</Text>;
      const names = humans.map((player) => player.name ?? String(player.userId));
      return <span title={names.join("、")}>{names.slice(0, 3).join("、")}{names.length > 3 ? ` 等 ${names.length} 人` : ""}</span>;
    } },
    { title: "板子", dataIndex: "board", width: 110, render: (value: string | null) => value ?? "—" },
    { title: "人数", width: 90, render: (_, row) => {
      const ai = row.players.filter((player) => player.isAi).length;
      return <>{row.playerCount}{ai ? <Text type="secondary"> · AI {ai}</Text> : null}</>;
    } },
    { title: "结果", width: 110, render: (_, row) => <ResultTag row={row} /> },
    { title: "天数", dataIndex: "endRound", width: 70, render: (value: number | null) => value ?? "—" },
    { title: "时长", width: 100, render: (_, row) => formatDuration(gameDuration(row.startedAt, row.endedAt)) },
    { title: "", width: 70, render: (_, row) => <Button size="small" type="link" onClick={(event) => { event.stopPropagation(); onChange({ game: row.id }); }}>详情</Button> },
  ];
  return (
    <div className="ww2-history">
      <div className="ww2-toolbar">
        <Input.Search
          allowClear
          placeholder="搜索群号或房主 QQ"
          value={searchDraft}
          onChange={(event) => setSearchDraft(event.target.value)}
          onSearch={(value) => set({ search: value.trim() })}
          style={{ maxWidth: 240 }}
        />
        <Select value={filters.board || ""} onChange={(value) => set({ board: value })} style={{ width: 130 }}
          options={[{ value: "", label: "全部板子" }, ...(meta?.boards ?? []).map((board) => ({ value: board.key, label: board.key }))]} />
        <Select value={filters.winner || ""} onChange={(value) => set({ winner: value })} style={{ width: 110 }}
          options={[{ value: "", label: "全部结果" }, { value: "wolf", label: "狼人胜" }, { value: "good", label: "好人胜" }]} />
        <Select value={filters.status} onChange={(value) => set({ status: value })} style={{ width: 110 }}
          options={[{ value: "all", label: "全部状态" }, { value: "finished", label: "已完成" }, { value: "running", label: "未完成" }]} />
        {filters.groupId !== null && <Tag closable onClose={() => set({ groupId: null })}>群 {filters.groupId}</Tag>}
        {filters.userId !== null && <Tag closable onClose={() => set({ userId: null })}>玩家 {filters.userId}</Tag>}
        {anyFilter && <Button type="link" onClick={() => { setSearchDraft(""); set({ search: "", status: "all", board: "", winner: "", groupId: null, userId: null }); }}>清除筛选</Button>}
        <span className="ww2-grow" />
        <Button icon={<HistoryOutlined />} onClick={() => setReplayOpen(true)}>回放查询</Button>
      </div>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : (
          <Table
            rowKey="id"
            size="small"
            columns={columns}
            loading={query.loading}
            dataSource={query.data?.rows ?? []}
            scroll={{ x: 760 }}
            onRow={(row) => ({ onClick: () => onChange({ game: row.id }), style: { cursor: "pointer" } })}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={anyFilter ? "没有符合筛选条件的对局" : "还没有狼人杀战绩"} /> }}
            pagination={{
              current: filters.page,
              pageSize: HISTORY_PAGE_SIZE,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              showTotal: (total) => `共 ${total} 局`,
              onChange: (page) => onChange({ page }),
            }}
          />
        )}
      <HistoryDetailDrawer rowId={filters.game} onClose={() => onChange({ game: null })}
        onFilterGroup={(groupId) => onChange({ game: null, groupId, page: 1 })}
        onFilterUser={(userId) => onChange({ game: null, userId, page: 1 })} />
      <ReplayDrawer open={replayOpen} onClose={() => setReplayOpen(false)} />
    </div>
  );
}

function HistoryDetailDrawer({ rowId, onClose, onFilterGroup, onFilterUser }: { rowId: number | null; onClose: () => void; onFilterGroup: (groupId: number) => void; onFilterUser: (userId: number) => void }): React.JSX.Element {
  const [data, setData] = useState<WwHistoryGame | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    if (rowId === null) return;
    setData(null);
    setError("");
    wwApi.historyGame(rowId).then(setData, (reason: unknown) => setError(reason instanceof Error ? reason.message : "加载失败"));
  }, [rowId]);
  useEffect(load, [load]);
  const wolves = data?.players.filter((player) => player.faction === "wolf") ?? [];
  const good = data?.players.filter((player) => player.faction !== "wolf") ?? [];
  const deaths = data ? deathOrder(data.players) : [];
  const rounds = [...new Set(deaths.map((player) => player.deathRound))];
  return (
    <Drawer open={rowId !== null} onClose={onClose} width="min(760px, 100%)" title={data ? `${groupTitle(data.groupName, data.groupId)} · ${data.board ?? "未知板子"}` : "对局详情"}>
      {error && <QueryErrorAlert error={error} onRetry={load} />}
      {!data && !error && <Skeleton active />}
      {data && (
        <div className="ww2-detail">
          <div className="ww2-detail-summary">
            <ResultTag row={data} />
            <span>{data.playerCount} 人 · {data.endRound ? `${data.endRound} 天` : "未完成"} · {formatDuration(gameDuration(data.startedAt, data.endedAt))}</span>
            <span className="ww2-muted">{formatTime(data.startedAt)} 开局 · 房主 {data.hostName ?? data.hostUserId}</span>
          </div>
          <RatioBar segments={[
            { value: wolves.filter((player) => player.deathRound === null).length, tone: "wolf", label: "终局存活狼人" },
            { value: good.filter((player) => player.deathRound === null).length, tone: "good", label: "终局存活好人" },
            { value: deaths.length, tone: "gray", label: "出局" },
          ]} />
          <div className="ww2-legend">
            <span><i className="is-wolf" />存活狼人 {wolves.filter((player) => player.deathRound === null).length}</span>
            <span><i className="is-good" />存活好人 {good.filter((player) => player.deathRound === null).length}</span>
            <span><i className="is-gray" />出局 {deaths.length}</span>
          </div>
          <div className="ww2-detail-teams">
            {[{ title: "狼人阵营", list: wolves, tone: "wolf" }, { title: "好人阵营", list: good, tone: "good" }].map((team) => (
              <section key={team.title} className={`ww2-team is-${team.tone}`}>
                <h4>{team.title} · {team.list.length} 人</h4>
                {team.list.map((player) => (
                  <div key={player.seat} className={`ww2-team-row${player.deathRound === null ? "" : " is-dead"}`}>
                    <SeatNo seat={player.seat} dead={player.deathRound !== null} />
                    <span className="ww2-team-name">
                      {player.isAi ? <>{player.name ?? "AI"} <Tag color="purple" bordered={false}>AI</Tag></>
                        : <button type="button" className="ww2-linklike" onClick={() => onFilterUser(player.userId)} title="查看 TA 的全部对局">{player.name ?? player.userId}</button>}
                      {player.isSheriff && <Tag color="gold" bordered={false}>警长</Tag>}
                    </span>
                    <RoleTag role={player.role} faction={player.faction} />
                    <span className="ww2-muted">{player.deathRound === null ? "存活" : `${roundLabel(player.deathRound)} · ${deathLabel(player.deathCause)}`}</span>
                    {player.isWinner === true && <Tag color="green" bordered={false}>胜</Tag>}
                    {player.isWinner === false && <Tag bordered={false}>负</Tag>}
                  </div>
                ))}
              </section>
            ))}
          </div>
          <h4>出局顺序</h4>
          {deaths.length === 0 ? <Text type="secondary">没有人出局</Text> : (
            <Timeline items={rounds.map((round) => ({
              children: <div><strong>{roundLabel(round)}</strong><div className="ww2-chip-row">
                {deaths.filter((player) => player.deathRound === round).map((player) => {
                  const info = roleInfo(player.role, player.faction);
                  return <Tag key={player.seat} color={info.faction === "wolf" ? "red" : "green"} bordered={false}>{player.seat}号 {info.icon}{info.role} · {deathLabel(player.deathCause)}</Tag>;
                })}
              </div></div>,
            }))} />
          )}
          <div className="ww2-actions">
            <Button onClick={() => onFilterGroup(data.groupId)}>这个群的全部对局</Button>
          </div>
        </div>
      )}
    </Drawer>
  );
}

function ReplayDrawer({ open, onClose }: { open: boolean; onClose: () => void }): React.JSX.Element {
  const [id, setId] = useState("");
  const [loading, setLoading] = useState(false);
  const [replay, setReplay] = useState<WwReplay | null>(null);
  const [error, setError] = useState("");
  const valid = /^[0-9a-fA-F]{32}$/.test(id.trim());
  const search = async () => {
    if (!valid) return;
    setLoading(true);
    setError("");
    setReplay(null);
    try {
      setReplay(await wwApi.replay(id.trim()));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "查询失败");
    } finally {
      setLoading(false);
    }
  };
  return (
    <Drawer open={open} onClose={onClose} width="min(760px, 100%)" title="回放查询">
      <p className="ww2-muted">每局结束时群里会播报“回放编号”（32 位字母数字），观战室右上角也可复制。这里显示的是公开视角：夜间行动只显示“夜晚”，不暴露身份。</p>
      <div className="ww2-toolbar">
        <Input value={id} onChange={(event) => setId(event.target.value)} onPressEnter={() => { void search(); }} placeholder="粘贴回放编号" status={id && !valid ? "error" : undefined} style={{ maxWidth: 380 }} allowClear />
        <Button type="primary" icon={<SearchOutlined />} disabled={!valid} loading={loading} onClick={() => { void search(); }}>查询</Button>
      </div>
      {id && !valid && <Text type="danger">回放编号应为 32 位十六进制字符</Text>}
      {error && <Alert type="error" showIcon message="查询失败" description={error} />}
      {replay && !replay.available && <Alert type="warning" showIcon message="这局不可回放" description={replay.reason ?? "事件日志不完整或已清理"} />}
      {replay && replay.available && (
        <>
          <div className="ww2-detail-summary">
            <strong>{replay.title}</strong>
            <span className="ww2-muted">{formatTime(replay.started_at)} → {formatTime(replay.ended_at)}</span>
          </div>
          {replay.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={warning} className="ww2-gap" />)}
          {replay.events.length === 0 ? <Empty description="没有可公开的事件" /> : (
            <Timeline items={replay.events.map((event) => ({
              key: event.sequence,
              color: event.audience === "public" ? "blue" : "purple",
              children: <div><Text type="secondary">#{event.sequence} · {formatTime(event.occurred_at)}{event.round ? ` · ${roundLabel(event.round)}` : ""}</Text><div>{event.detail}</div></div>,
            }))} />
          )}
        </>
      )}
    </Drawer>
  );
}
