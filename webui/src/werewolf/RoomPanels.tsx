// 观战室右侧的投票 / 玩家 / AI 面板。
import { Empty, Table, Tag, Typography } from "antd";
import { useMemo } from "react";
import type { WwEvent, WwGame, WwPlayer } from "./types";
import { aiSummary, deathLabel, roundLabel, seatActivity } from "./ww-meta";
import type { VoteSummary } from "./ww-meta";
import { Metric, RoleTag, SeatNo } from "./WwBits";

const { Text } = Typography;

export function VotesPanel({ votes, players, showRole, onSeat }: { votes: VoteSummary[]; players: WwPlayer[]; showRole: boolean; onSeat: (seat: number) => void }): React.JSX.Element {
  if (votes.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有投票，警长投票和放逐投票的计票会出现在这里" />;
  const ordered = votes.slice().reverse();
  const byseat = new Map(players.map((player) => [player.seat, player]));
  return (
    <div className="ww2-votes">
      {ordered.map((vote) => {
        const max = Math.max(1, ...vote.buckets.map((bucket) => bucket.votes));
        return (
          <section key={vote.seq} className="ww2-vote">
            <header>
              <strong>{roundLabel(vote.round)} · {vote.label}</strong>
              <Text type="secondary">{vote.total} 人参与{vote.leaders.length > 1 ? ` · ${vote.leaders.join("、")} 号平票` : vote.leaders.length === 1 ? ` · ${vote.leaders[0]} 号最高` : ""}</Text>
            </header>
            {vote.buckets.map((bucket) => {
              const target = byseat.get(bucket.seat);
              return (
                <div key={bucket.seat} className={`ww2-vote-row${vote.leaders.includes(bucket.seat) ? " is-lead" : ""}`}>
                  <button type="button" className="ww2-vote-target" onClick={() => onSeat(bucket.seat)}>
                    <SeatNo seat={bucket.seat} /> {target?.name ?? ""}
                    {showRole && target && <RoleTag role={target.role} faction={target.faction} />}
                  </button>
                  <span className="ww2-meter-track"><span className="ww2-meter-fill is-gold" style={{ width: `${Math.round((bucket.votes / max) * 100)}%` }} /></span>
                  <strong className="ww2-vote-count">{bucket.votes}</strong>
                  <span className="ww2-vote-voters">{bucket.voters.map((voter) => `${voter.seat}${voter.sheriff ? "★" : ""}`).join(" ")}</span>
                </div>
              );
            })}
            {vote.abstain.length > 0 && <div className="ww2-muted">弃票：{vote.abstain.map((seat) => `${seat}号`).join("、")}</div>}
          </section>
        );
      })}
      <p className="ww2-muted">★ 表示警长票（计 1.5 票）。点击座位可在时间线里只看该玩家。</p>
    </div>
  );
}

export function PlayersPanel({ game, events, showRole, selected, onSeat }: { game: WwGame; events: WwEvent[]; showRole: boolean; selected: number | null; onSeat: (seat: number) => void }): React.JSX.Element {
  const rows = useMemo(() => game.players.map((player) => ({ ...player, activity: seatActivity(events, player.seat) })), [game.players, events]);
  if (game.players.length === 0) {
    return (
      <Table size="small" rowKey="userId" pagination={false} dataSource={game.signup} locale={{ emptyText: "还没有人报名" }} columns={[
        { title: "#", width: 48, render: (_, __, index) => index + 1 },
        { title: "报名玩家", render: (_, row) => <>{row.name}{row.isAi && <Tag color="purple" bordered={false}>AI</Tag>}</> },
        { title: "QQ", dataIndex: "userId", render: (value: number) => (value > 0 ? value : "—") },
        ...(showRole ? [{ title: "想当", render: (_: unknown, row: (typeof game.signup)[number]) => row.requestedRole ?? "—" }] : []),
      ]} />
    );
  }
  return (
    <Table
      size="small"
      rowKey="userId"
      pagination={false}
      dataSource={rows}
      scroll={{ x: 640 }}
      rowClassName={(row) => (row.seat === selected ? "ww2-row-selected" : row.alive ? "" : "ww2-row-dead")}
      onRow={(row) => ({ onClick: () => onSeat(row.seat), style: { cursor: "pointer" } })}
      columns={[
        { title: "座", dataIndex: "seat", width: 54, render: (seat: number, row) => <SeatNo seat={seat} dead={!row.alive} /> },
        { title: "玩家", render: (_, row) => <>{row.name}{row.isAi && <Tag color="purple" bordered={false}>AI</Tag>}{row.isSheriff && <Tag color="gold" bordered={false}>警长</Tag>}<br /><Text type="secondary">{row.isAi ? "AI" : row.userId}</Text></> },
        { title: "身份", width: 110, render: (_, row) => <RoleTag role={row.role} faction={row.faction} hidden={!showRole} /> },
        { title: "状态", width: 130, render: (_, row) => row.alive ? <Tag color="green">存活</Tag> : <Tag color="red">{row.deathRound ? `D${row.deathRound} ` : ""}{deathLabel(row.deathCause)}</Tag> },
        { title: "发言", width: 64, sorter: (left, right) => left.activity.speeches - right.activity.speeches, render: (_, row) => row.activity.speeches },
        { title: "被投", width: 64, sorter: (left, right) => left.activity.votesReceived - right.activity.votesReceived, render: (_, row) => row.activity.votesReceived },
        { title: "提示", render: (_, row) => <span className="ww2-chip-row">
          {row.silenced && <Tag color="orange" bordered={false}>禁言</Tag>}
          {row.canVote === false && <Tag color="orange" bordered={false}>无票</Tag>}
          {row.dmOk === false && !row.isAi && <Tag color="red" bordered={false}>私聊失败</Tag>}
          {row.pendingAction && <Tag color="blue" bordered={false}>排队</Tag>}
        </span> },
      ]}
    />
  );
}

export function AiPanel({ events, onSeat }: { events: WwEvent[]; onSeat: (seat: number) => void }): React.JSX.Element {
  const summary = useMemo(() => aiSummary(events), [events]);
  if (summary.decisions + summary.speeches === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本局还没有 AI 调用记录" />;
  return (
    <div className="ww2-ai-panel">
      <div className="ww2-metrics">
        <Metric label="AI 决策" value={summary.decisions} />
        <Metric label="AI 发言稿" value={summary.speeches} />
        <Metric label="格式纠正" value={summary.retries} tone={summary.retries ? "warn" : undefined} />
        <Metric label="空回复" value={summary.empty} tone={summary.empty ? "error" : undefined} hint="空回复会按阶段安全默认托管" />
      </div>
      <Table size="small" rowKey="seat" pagination={false} dataSource={summary.bySeat}
        onRow={(row) => ({ onClick: () => onSeat(row.seat), style: { cursor: "pointer" } })}
        columns={[
          { title: "座", dataIndex: "seat", width: 54, render: (seat: number) => <SeatNo seat={seat} /> },
          { title: "AI", dataIndex: "name", render: (value: string | null) => value ?? "—" },
          { title: "调用", dataIndex: "decisions", width: 70 },
          { title: "纠正", dataIndex: "retries", width: 70, render: (value: number) => value ? <Tag color="orange">{value}</Tag> : 0 },
          { title: "空回复", dataIndex: "empty", width: 80, render: (value: number) => value ? <Tag color="red">{value}</Tag> : 0 },
        ]} />
      <p className="ww2-muted">点击一行可在时间线里只看该 AI 的思考与发言。</p>
    </div>
  );
}
