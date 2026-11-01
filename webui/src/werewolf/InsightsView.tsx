// 数据洞察：时间窗/板子筛选 + 指标卡 + 板子/角色/死因/趋势/时段/排行榜。
import { Empty, Segmented, Select, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import { QueryErrorAlert, useApiQuery } from "../shared";
import type { WwMeta, WwStats } from "./types";
import { groupTitle } from "./LiveView";
import { wwApi } from "./ww-api";
import { deathLabel, fillDays, pct, roleInfo } from "./ww-meta";
import { Metric, MeterRow, RatioBar, RoleTag } from "./WwBits";

const { Text } = Typography;
export const WINDOWS = [
  { value: 7, label: "近 7 天" },
  { value: 30, label: "近 30 天" },
  { value: 90, label: "近 90 天" },
  { value: 0, label: "全部" },
];

export function InsightsView({ days, board, meta, onChange, onOpenHistory }: {
  days: number;
  board: string;
  meta: WwMeta | null;
  onChange: (patch: { days?: number; board?: string }) => void;
  onOpenHistory: (patch: { groupId?: number | null; userId?: number | null; board?: string }) => void;
}): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["ww-stats", days, board],
    fetcher: () => wwApi.stats(days, board),
    invalidation: { resources: ["werewolf_game"] },
  });
  const stats = query.data;
  return (
    <div className="ww2-insights">
      <div className="ww2-toolbar">
        <Segmented value={days} onChange={(value) => onChange({ days: Number(value) })} options={WINDOWS} />
        <Select value={board} onChange={(value) => onChange({ board: value })} style={{ width: 140 }}
          options={[{ value: "", label: "全部板子" }, ...(meta?.boards ?? []).map((item) => ({ value: item.key, label: item.key }))]} />
        {stats?.truncated && <Tag color="orange">数据量较大，只统计最近 5000 局</Tag>}
      </div>
      {query.error && !stats ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : !stats ? <Skeleton active paragraph={{ rows: 10 }} />
          : stats.totals.games === 0 ? <Empty description="这个时间范围内还没有对局" />
            : <InsightsBody stats={stats} onOpenHistory={(patch) => onOpenHistory({ board, ...patch })} />}
    </div>
  );
}

function InsightsBody({ stats, onOpenHistory }: { stats: WwStats; onOpenHistory: (patch: { groupId?: number | null; userId?: number | null; board?: string }) => void }): React.JSX.Element {
  const totals = stats.totals;
  const maxBoard = Math.max(1, ...stats.boards.map((row) => row.games));
  const maxCause = Math.max(1, ...stats.deathCauses.map((row) => row.count));
  const daily = fillDays(stats.daily, (date) => ({ date, games: 0, wolfWins: 0, goodWins: 0, unfinished: 0 }));
  const maxDaily = Math.max(1, ...daily.map((row) => row.games));
  const maxHour = Math.max(1, ...stats.hours.map((row) => row.count));
  const maxGroup = Math.max(1, ...stats.groups.map((row) => row.games));
  return (
    <>
      <div className="ww2-metrics">
        <Metric label="对局" value={totals.games} hint={`${totals.groups} 个群`} />
        <Metric label="完整结束" value={pct(totals.games ? totals.finished / totals.games : null)} hint={`${totals.unfinished} 局中断/流局`} tone={totals.unfinished > totals.finished ? "warn" : undefined} />
        <Metric label="狼人胜率" value={pct(totals.wolfWinRate)} hint={`狼 ${totals.wolfWins} : 好 ${totals.goodWins}`} />
        <Metric label="平均天数" value={totals.avgRounds ?? "—"} hint={totals.avgPlayers ? `平均 ${totals.avgPlayers} 人` : undefined} />
        <Metric label="真人玩家" value={totals.humanPlayers} />
        <Metric label="AI 座位占比" value={pct(totals.aiSeatShare)} />
      </div>
      <div className="ww2-insight-grid">
        <section className="ww2-panel">
          <h4>阵营胜负</h4>
          <RatioBar height={14} segments={[{ value: totals.wolfWins, tone: "wolf", label: "狼人胜" }, { value: totals.goodWins, tone: "good", label: "好人胜" }]} />
          <div className="ww2-legend"><span><i className="is-wolf" />狼人胜 {totals.wolfWins}</span><span><i className="is-good" />好人胜 {totals.goodWins}</span></div>
          <h5>AI 与真人</h5>
          <MeterRow label="真人" value={stats.aiVsHuman.human.winRate ?? 0} max={1} text={`${pct(stats.aiVsHuman.human.winRate)} · ${stats.aiVsHuman.human.seats} 座次`} tone="good" />
          <MeterRow label="AI" value={stats.aiVsHuman.ai.winRate ?? 0} max={1} text={`${pct(stats.aiVsHuman.ai.winRate)} · ${stats.aiVsHuman.ai.seats} 座次`} tone="purple" />
        </section>
        <section className="ww2-panel">
          <h4>板子</h4>
          {stats.boards.map((row) => (
            <MeterRow key={row.board} label={row.board} value={row.games} max={maxBoard} onClick={() => onOpenHistory({ board: row.board })}
              text={<Tooltip title={`狼胜 ${row.wolfWins} · 好胜 ${row.goodWins} · 平均 ${row.avgRounds ?? "—"} 天`}>{row.games} 局 · 狼胜 {pct(row.finished ? row.wolfWins / row.finished : null)}</Tooltip>} />
          ))}
        </section>
        <section className="ww2-panel">
          <h4>死因分布</h4>
          {stats.deathCauses.length === 0 ? <Text type="secondary">暂无</Text> : stats.deathCauses.map((row) => (
            <MeterRow key={row.cause} label={deathLabel(row.cause)} value={row.count} max={maxCause} text={row.count} tone="red" />
          ))}
        </section>
        <section className="ww2-panel ww2-span-2">
          <h4>每日对局</h4>
          <div className="ww2-bars" role="img" aria-label="每日对局数柱状图">
            {daily.map((row) => (
              <Tooltip key={row.date} title={`${row.date}：${row.games} 局（狼胜 ${row.wolfWins} · 好胜 ${row.goodWins} · 未完成 ${row.unfinished}）`}>
                <span className={`ww2-bar${row.games ? "" : " is-zero"}`} style={{ height: `${row.games ? Math.max(4, Math.round((row.games / maxDaily) * 100)) : 2}%` }}>
                  <span className="ww2-bar-wolf" style={{ height: `${row.games ? Math.round((row.wolfWins / row.games) * 100) : 0}%` }} />
                </span>
              </Tooltip>
            ))}
          </div>
          <div className="ww2-axis"><span>{daily[0]?.date}</span><span>{daily[daily.length - 1]?.date}</span></div>
          <div className="ww2-legend"><span><i className="is-accent" />对局</span><span><i className="is-wolf" />其中狼人胜</span></div>
        </section>
        <section className="ww2-panel">
          <h4>开局时段</h4>
          <div className="ww2-hours" role="img" aria-label="24 小时开局热度">
            {stats.hours.map((row) => (
              <Tooltip key={row.hour} title={`${row.hour}:00–${row.hour + 1}:00 · ${row.count} 局`}>
                <span className="ww2-hour" style={{ opacity: row.count ? 0.18 + 0.82 * (row.count / maxHour) : 0.08 }}>{row.hour}</span>
              </Tooltip>
            ))}
          </div>
        </section>
        <section className="ww2-panel ww2-span-2">
          <h4>角色表现</h4>
          <Table size="small" rowKey="role" pagination={false} dataSource={stats.roles} scroll={{ x: 520 }} columns={[
            { title: "角色", render: (_, row) => <RoleTag role={row.role} faction={row.faction} /> },
            { title: "座次", dataIndex: "seats", width: 70, sorter: (left, right) => left.seats - right.seats },
            { title: "胜率", width: 160, sorter: (left, right) => left.winRate - right.winRate, render: (_, row) => <MeterRow label="" value={row.winRate} max={1} text={pct(row.winRate)} tone={roleInfo(row.role, row.faction).faction === "wolf" ? "wolf" : "good"} /> },
            { title: "存活率", width: 90, sorter: (left, right) => left.survivalRate - right.survivalRate, render: (_, row) => pct(row.survivalRate) },
            { title: "当选警长", dataIndex: "sheriff", width: 90 },
          ]} />
        </section>
        <section className="ww2-panel">
          <h4>活跃的群</h4>
          {stats.groups.map((row) => (
            <MeterRow key={row.groupId} label={groupTitle(row.groupName, row.groupId)} value={row.games} max={maxGroup} text={`${row.games} 局`} onClick={() => onOpenHistory({ groupId: row.groupId })} />
          ))}
        </section>
        <section className="ww2-panel ww2-span-3">
          <h4>玩家排行 <Text type="secondary">（只统计真人、完整结束的对局；点击查看 TA 的对局）</Text></h4>
          <Table size="small" rowKey="userId" dataSource={stats.leaderboard} pagination={{ pageSize: 10, hideOnSinglePage: true }} scroll={{ x: 640 }}
            onRow={(row) => ({ onClick: () => onOpenHistory({ userId: row.userId }), style: { cursor: "pointer" } })}
            locale={{ emptyText: "还没有真人完成对局" }}
            columns={[
              { title: "#", width: 48, render: (_, __, index) => index + 1 },
              { title: "玩家", render: (_, row) => <>{row.name ?? "未知昵称"}<br /><Text type="secondary">{row.userId}</Text></> },
              { title: "场次", dataIndex: "games", width: 70, sorter: (left, right) => left.games - right.games },
              { title: "胜率", width: 90, sorter: (left, right) => left.winRate - right.winRate, render: (_, row) => pct(row.winRate) },
              { title: "存活", width: 70, render: (_, row) => pct(row.games ? row.survived / row.games : null) },
              { title: "狼人局", width: 80, dataIndex: "wolfGames" },
              { title: "警长", width: 64, dataIndex: "sheriff" },
              { title: "常玩", width: 110, render: (_, row) => row.favoriteRole ? <RoleTag role={row.favoriteRole} /> : "—" },
            ]} />
        </section>
      </div>
    </>
  );
}
