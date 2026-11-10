import { HistoryOutlined, TrophyOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Empty, Segmented, Select, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type GameStats, spaceApi, type WerewolfGameRow } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { DEATH_CAUSE_LABEL, FACTION_LABEL, percent } from "./me-space-meta";
import { filterGames, type GameFilter, recentForm, werewolfTitle } from "./me-hub-meta";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

function resultTag(row: WerewolfGameRow): React.JSX.Element {
  if (row.won === null || !row.endedAt) return <Tag>没打完</Tag>;
  return row.won ? <Tag color="success">胜</Tag> : <Tag color="error">负</Tag>;
}

export function MyGamesPage(): React.JSX.Element {
  const { query, data } = useResource<GameStats>(["me", "games"], () => spaceApi.games());
  const [filter, setFilter] = useState<GameFilter>({ faction: "all", result: "all", board: "" });
  const recent = useMemo(() => filterGames(data?.recent ?? [], filter), [data, filter]);
  const boards = useMemo(() => [...new Set((data?.recent ?? []).map((row) => row.board).filter((board): board is string => Boolean(board)))], [data]);
  const form = useMemo(() => recentForm(data?.recent ?? []), [data]);
  const bestRole = useMemo(() => [...(data?.byRole ?? [])].filter((row) => row.games >= 2).sort((a, b) => b.wins / b.games - a.wins / a.games || b.games - a.games)[0] ?? null, [data]);

  return <div className="me-page">
    <PageHeader title="狼人杀战绩" subtitle="你在各群玩过的狼人杀（只算真人座位）。群里发 /狼人杀 开一局。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : !data.available ? (
      <Alert type="info" showIcon message="狼人杀没有启用" description="管理员没有加载狼人杀，或还没完成数据库迁移。" />
    ) : !data.games ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没玩过狼人杀" />
    ) : <>
      <section className="me-ach-hero" aria-label="我的称号">
        <span className="me-badge-icon" aria-hidden="true">🐺</span>
        <div className="me-ach-hero-main">
          <strong>{werewolfTitle(data)}</strong>
          <Text type="secondary" className="me-hint">按你的战绩起的称号，打得越多越准。{bestRole ? `最拿手的是「${bestRole.role}」，${bestRole.games} 局赢了 ${bestRole.wins} 局。` : ""}</Text>
          {form.length > 0 && <span className="me-ach-next"><Text type="secondary" className="me-hint">最近 {form.length} 局：</Text><span className="me-form-dots" role="img" aria-label={`最近 ${form.length} 局：${form.map((item) => (item === "W" ? "胜" : item === "L" ? "负" : "没打完")).join("、")}`}>{form.map((item, index) => <Tooltip key={index} title={item === "W" ? "胜" : item === "L" ? "负" : "没打完"}><i className={item === "W" ? "is-win" : item === "L" ? "is-loss" : undefined}>{item === "W" ? "胜" : item === "L" ? "负" : "·"}</i></Tooltip>)}</span></span>}
        </div>
      </section>
      <div className="me-stat-row">
        <StatTile label="胜率" value={percent(data.winRate)} sub={`${data.wins} 胜 / ${data.finished} 局打完`} tone="brand" />
        <StatTile label="一共参加" value={`${data.games} 局`} sub={data.aborted ? `${data.aborted} 局没打完` : undefined} />
        <StatTile label="连胜" value={data.currentStreak ?? 0} sub={`最长 ${data.bestStreak ?? 0} 连胜`} tone="warning" />
        <StatTile label="活到最后" value={data.survived ?? 0} sub={`当过 ${data.sheriff ?? 0} 次警长`} />
      </div>
      <div className="me-two-col">
        <MeSection icon={<TrophyOutlined />} title="阵营表现">
          <ul className="me-bar-list">
            {Object.entries(data.byFaction ?? {}).map(([faction, row]) => (
              <li key={faction}>
                <span className="me-bar-label">{FACTION_LABEL[faction] ?? faction}</span>
                <span className="me-bar-track"><span className={`me-bar-fill is-${faction}`} style={{ width: `${row.games ? Math.round((row.wins / row.games) * 100) : 0}%` }} /></span>
                <strong className="me-bar-value">{row.games ? percent(row.wins / row.games) : "—"}</strong>
                <Text type="secondary" className="me-hint">{row.wins}/{row.games}</Text>
              </li>
            ))}
          </ul>
          {Object.keys(data.deathCauses ?? {}).length > 0 && (
            <div className="me-chip-row">
              <Text type="secondary" className="me-hint">出局方式：</Text>
              {Object.entries(data.deathCauses ?? {}).map(([cause, count]) => <Tag key={cause}>{DEATH_CAUSE_LABEL[cause] ?? cause} {count}</Tag>)}
            </div>
          )}
        </MeSection>
        <MeSection icon={<UserOutlined />} title="常玩角色">
          <ul className="me-bar-list">
            {(data.byRole ?? []).slice(0, 8).map((row) => (
              <li key={row.role}>
                <span className="me-bar-label">{row.role}</span>
                <span className="me-bar-track"><span className="me-bar-fill" style={{ width: `${Math.round((row.wins / Math.max(row.games, 1)) * 100)}%` }} /></span>
                <strong className="me-bar-value">{percent(row.wins / Math.max(row.games, 1))}</strong>
                <Text type="secondary" className="me-hint">{row.games} 局</Text>
              </li>
            ))}
          </ul>
        </MeSection>
      </div>
      <MeSection icon={<HistoryOutlined />} title="最近的对局" hint={recent.length === (data.recent ?? []).length ? undefined : `筛出 ${recent.length} / ${(data.recent ?? []).length} 局`}>
        <div className="me-toolbar">
          <Segmented<GameFilter["result"]> size="small" value={filter.result} onChange={(result) => setFilter({ ...filter, result })} options={[{ value: "all", label: "全部" }, { value: "win", label: "赢的" }, { value: "lose", label: "输的" }, { value: "open", label: "没打完" }]} aria-label="按结果筛选" />
          <Segmented<GameFilter["faction"]> size="small" value={filter.faction} onChange={(faction) => setFilter({ ...filter, faction })} options={[{ value: "all", label: "两个阵营" }, { value: "good", label: "好人" }, { value: "wolf", label: "狼人" }]} aria-label="按阵营筛选" />
          {boards.length > 1 && <Select size="small" allowClear placeholder="全部板子" value={filter.board || undefined} onChange={(board) => setFilter({ ...filter, board: board ?? "" })} options={boards.map((value) => ({ value, label: value }))} aria-label="按板子筛选" popupMatchSelectWidth={false} />}
        </div>
        <Table<WerewolfGameRow>
          size="small"
          rowKey="gameId"
          pagination={false}
          scroll={{ x: 640 }}
          dataSource={recent}
          locale={{ emptyText: "没有符合条件的对局" }}
          columns={[
            { title: "时间", dataIndex: "startedAt", render: (value: string | null) => formatQqTime(value) },
            { title: "群", dataIndex: "groupName", render: (value: string | null, row) => value || row.groupId, responsive: ["md"] },
            { title: "板子", dataIndex: "board", render: (value: string | null, row) => `${value ?? "—"} · ${row.players} 人`, responsive: ["lg"] },
            { title: "我的身份", dataIndex: "role", render: (value: string, row) => <span>{value}{row.sheriff && <Tag className="me-inline-tag">警长</Tag>}</span> },
            { title: "结果", key: "result", render: (_value, row) => resultTag(row) },
            { title: "出局", key: "death", render: (_value, row) => (row.deathRound ? `第 ${row.deathRound} 轮 · ${DEATH_CAUSE_LABEL[row.deathCause ?? ""] ?? row.deathCause ?? ""}` : row.endedAt ? "活到最后" : "—"), responsive: ["md"] },
          ]}
        />
      </MeSection>
    </>}
  </div>;
}
