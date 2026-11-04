import { CalendarOutlined, FireOutlined, HeartOutlined, HistoryOutlined, PieChartOutlined } from "@ant-design/icons";
import { Alert, Empty, Progress, Segmented, Skeleton, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type AffinityState, spaceApi } from "./me-space-api";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text } = Typography;

export function MyAffinityPage(): React.JSX.Element {
  const { query, data } = useResource<AffinityState>(["me", "affinity"], () => spaceApi.affinity());
  const [eventFilter, setEventFilter] = useState<string>("all");
  const breakdown = useMemo(() => {
    if (!data) return [];
    const rows = Object.entries(data.breakdown30d).map(([source, total]) => ({ source, total, label: data.sources[source]?.label ?? source }));
    const max = Math.max(1, ...rows.map((row) => Math.abs(row.total)));
    return rows.sort((a, b) => b.total - a.total).map((row) => ({ ...row, width: Math.round((Math.abs(row.total) / max) * 100) }));
  }, [data]);
  const events = useMemo(() => (data?.events ?? []).filter((event) => eventFilter === "all" || event.source === eventFilter), [data, eventFilter]);
  const groupNames = useMemo(() => new Map((data?.groups ?? []).map((group) => [group.groupId, group.groupName])), [data]);

  return <div className="me-page">
    <PageHeader title="好感与签到" subtitle="和 Bot 越熟，它说话越亲近。签到、私聊、在群里和它互动都会涨好感。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : <>
      {!data.available && <Alert type="info" showIcon message="好感流水还没启用" description="管理员完成数据库迁移后，这里会显示每一笔变化。" />}
      <section className="me-level-card">
        <span className="me-level-icon" aria-hidden="true"><HeartOutlined /></span>
        <div className="me-level-main">
          <Text type="secondary">现在的关系</Text>
          <strong className="me-level-name">{data.level}</strong>
          <Text type="secondary" className="me-hint">{data.tones[data.level] ?? ""}</Text>
          <Progress percent={Math.round(data.progress * 100)} showInfo={false} strokeColor="var(--brand)" aria-label="升级进度" />
          <Text type="secondary" className="me-hint">{data.nextLevel ? `再 ${data.toNext ?? 0} 点升到「${data.nextLevel}」` : "已经是最高等级啦"} · 今天 +{data.todayTotal}</Text>
        </div>
        <ol className="me-level-ladder" aria-label="等级">
          {data.levels.map((level, index) => (
            <li key={level.name} className={index < data.levelIndex ? "is-passed" : index === data.levelIndex ? "is-current" : undefined}>
              <span>{level.name}</span><Text type="secondary" className="me-hint">{level.floor}</Text>
            </li>
          ))}
        </ol>
      </section>

      <div className="me-stat-row">
        <StatTile label="好感点数" value={data.points} tone="brand" />
        <StatTile label="今天获得" value={`+${data.todayTotal}`} />
        <StatTile label="签到的群" value={data.groups.filter((group) => group.totalDays > 0).length} sub={`共 ${data.groups.length} 个群`} />
        <StatTile label="最长连签" value={`${Math.max(0, ...data.groups.map((group) => group.streakDays))} 天`} tone="warning" />
      </div>

      <div className="me-two-col">
        <MeSection icon={<PieChartOutlined />} title="近 30 天从哪来">
          {breakdown.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="最近 30 天没有变化" /> : (
            <ul className="me-bar-list">
              {breakdown.map((row) => (
                <li key={row.source}>
                  <span className="me-bar-label">{row.label}</span>
                  <span className="me-bar-track"><span className={`me-bar-fill${row.total < 0 ? " is-negative" : ""}`} style={{ width: `${row.width}%` }} /></span>
                  <strong className="me-bar-value">{row.total > 0 ? "+" : ""}{row.total}</strong>
                </li>
              ))}
            </ul>
          )}
          <Text type="secondary" className="me-hint">每天每种来源有上限（今天已得/上限）：{Object.entries(data.sources).filter(([, rule]) => rule.dailyCap !== null).map(([source, rule]) => `${rule.label} ${data.today[source] ?? 0}/${rule.dailyCap}`).join("、")}</Text>
        </MeSection>
        <MeSection icon={<CalendarOutlined />} title="各群签到" hint="群里发 /签到；连签越久奖励越多。">
          {data.groups.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还不在任何群里" /> : (
            <ul className="me-checkin-list">
              {data.groups.map((group) => (
                <li key={group.groupId} className={group.checkedToday ? "is-done" : undefined}>
                  <span className="me-plain-main"><strong>{group.groupName || `群 ${group.groupId}`}</strong><Text type="secondary" className="me-hint">累计 {group.totalDays} 天 · 积分 {group.points} · 经验 {group.exp}{group.lastCheckinDate ? ` · 上次 ${group.lastCheckinDate}` : ""}</Text></span>
                  {group.streakDays > 0 && <Tag icon={<FireOutlined />} color="orange">连签 {group.streakDays}</Tag>}
                  {group.checkedToday ? <Tag color="success">今天签过了</Tag> : <Tag>今天还没签</Tag>}
                </li>
              ))}
            </ul>
          )}
        </MeSection>
      </div>

      <MeSection icon={<HistoryOutlined />} title="最近的变化" extra={
        <Segmented size="small" value={eventFilter} onChange={setEventFilter} options={[{ value: "all", label: "全部" }, ...Object.entries(data.sources).map(([value, rule]) => ({ value, label: rule.label }))]} />
      }>
        {events.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有记录" /> : (
          <ul className="me-plain-list">
            {events.map((event) => (
              <li key={event.id}>
                <strong className={`me-delta${event.delta < 0 ? " is-negative" : ""}`}>{event.delta > 0 ? "+" : ""}{event.delta}</strong>
                <span className="me-plain-main">{event.source_label}{event.note && <Text type="secondary"> · {event.note}</Text>}{event.group_id !== null && <Text type="secondary"> · {groupNames.get(String(event.group_id)) ?? `群 ${event.group_id}`}</Text>}</span>
                <Text type="secondary" className="me-hint me-mono">{(event.created_at ?? "").replace("T", " ").slice(0, 16)}</Text>
              </li>
            ))}
          </ul>
        )}
      </MeSection>
    </>}
  </div>;
}
