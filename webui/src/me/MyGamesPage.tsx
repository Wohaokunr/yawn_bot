import { HistoryOutlined, TrophyOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, Empty, Skeleton, Table, Tag, Typography } from "antd";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type GameStats, spaceApi, type WerewolfGameRow } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { DEATH_CAUSE_LABEL, FACTION_LABEL, percent } from "./me-space-meta";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text } = Typography;

function resultTag(row: WerewolfGameRow): React.JSX.Element {
  if (row.won === null || !row.endedAt) return <Tag>没打完</Tag>;
  return row.won ? <Tag color="success">胜</Tag> : <Tag color="error">负</Tag>;
}

export function MyGamesPage(): React.JSX.Element {
  const { query, data } = useResource<GameStats>(["me", "games"], () => spaceApi.games());

  return <div className="me-page">
    <PageHeader title="狼人杀战绩" subtitle="你在各群玩过的狼人杀（只算真人座位）。群里发 /狼人杀 开一局。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : !data.available ? (
      <Alert type="info" showIcon message="狼人杀没有启用" description="管理员没有加载狼人杀，或还没完成数据库迁移。" />
    ) : !data.games ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没玩过狼人杀" />
    ) : <>
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
      <MeSection icon={<HistoryOutlined />} title="最近的对局">
        <Table<WerewolfGameRow>
          size="small"
          rowKey="gameId"
          pagination={false}
          scroll={{ x: 640 }}
          dataSource={data.recent ?? []}
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
