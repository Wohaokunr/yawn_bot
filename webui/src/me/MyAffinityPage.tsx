import { CalendarOutlined, CheckCircleOutlined, FireOutlined, HeartOutlined, HistoryOutlined, PieChartOutlined } from "@ant-design/icons";
import { MyAffinityPlay } from "../affinity/AffinityPlayPanel";
import { Alert, Button, Empty, Progress, Segmented, Skeleton, Tag, Typography } from "antd";
import { Link } from "react-router-dom";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type AffinityState, spaceApi } from "./me-space-api";
import { Heatmap, HeatLegend } from "./hub-ui";
import { type CheckinHistory, type CheckinOutcome, checkinSummary, hubApi } from "./me-hub-api";
import { heatColumns } from "./me-hub-meta";
import { MeSection, StatTile, UrlTabs, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;
const TAB_KEYS = ["overview", "perks", "play"] as const;

export function MyAffinityPage(): React.JSX.Element {
  const { query, data } = useResource<AffinityState>(["me", "affinity"], () => spaceApi.affinity());
  const history = useResource<CheckinHistory>(["me", "checkin-history"], () => hubApi.checkinHistory(119));
  const { busy, run } = useAction();
  const [outcome, setOutcome] = useState<CheckinOutcome | null>(null);
  const checkin = async (groupId?: string): Promise<void> => {
    const result = await run(groupId ? `checkin-${groupId}` : "checkin", () => hubApi.checkin(groupId), checkinSummary);
    if (result) {
      setOutcome(result);
      query.reload();
      history.query.reload();
    }
  };
  const heat = useMemo(() => {
    if (!history.data) return [];
    return heatColumns(history.data.today, 119, Object.fromEntries(history.data.days.map((row) => [row.day, row.groups])));
  }, [history.data]);
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
    <PageHeader title="好感与签到" subtitle="和 Bot 越熟，它说话越亲近、给你的额度越多、能解锁的玩法越多。签到、私聊、群里互动、送礼、做每日任务都会涨好感。" onRefresh={query.reload} refreshing={query.refreshing} />
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

      {data.groups.length > 0 && (() => {
        const pending = data.groups.filter((group) => !group.checkedToday);
        return <MeSection icon={<CalendarOutlined />} title="签到" extra={history.data ? <HeatLegend less="没签" more="签得多" /> : undefined}>
          <div className="me-checkin-hero">
            <div className="me-checkin-hero-main">
              <strong>{pending.length === 0 ? <><CheckCircleOutlined /> 今天 {data.groups.length} 个群都签过了</> : `今天还有 ${pending.length} 个群没签`}</strong>
              <Text type="secondary" className="me-hint">{pending.length > 0 ? `还没签：${pending.map((group) => group.groupName || group.groupId).join("、")}` : "明天再来，连签越久奖励越多。"}</Text>
              {history.data && <Text type="secondary" className="me-hint">最近 {history.data.days.length ? 120 : 0} 天里签了 {history.data.activeDays} 天，已连续 {history.data.anyStreak} 天，共得 {history.data.reward} 积分。</Text>}
            </div>
            {pending.length > 0 && <Button type="primary" icon={<FireOutlined />} loading={busy === "checkin"} onClick={() => void checkin()}>一键签到</Button>}
          </div>
          {outcome && outcome.signed > 0 && (
            <div className="me-chip-row" aria-live="polite">
              {outcome.results.filter((row) => row.status === "ok").map((row) => (
                <Tag key={row.groupId} color="success">{row.groupName || row.groupId}：积分 +{row.reward ?? 0}{row.lucky ? " 暴击！" : ""}{row.milestone ? ` · 连签 ${row.milestone} 天里程碑` : ` · 连签 ${row.streakDays}`}{row.rankToday ? ` · 今天第 ${row.rankToday} 个` : ""}</Tag>
              ))}
            </div>
          )}
          {history.data && <Heatmap columns={heat} max={Math.max(1, data.groups.length)} label="最近 17 周的签到" tip={(cell) => `${cell.day}：${cell.value ? `签了 ${cell.value} 个群` : "没签"}`} />}
        </MeSection>;
      })()}
      <UrlTabs keys={TAB_KEYS} fallback="overview" items={[
        { key: "overview", label: "来源与签到", children: <AffinityOverview data={data} breakdown={breakdown} events={events} groupNames={groupNames} eventFilter={eventFilter} setEventFilter={setEventFilter} busy={busy} onCheckin={(groupId) => void checkin(groupId)} /> },
        { key: "perks", label: "等级权益", children: <MyAffinityPlay view="perks" /> },
        { key: "play", label: "任务·送礼·商店", children: <MyAffinityPlay view="play" /> },
      ]} />
    </>}
  </div>;
}

function AffinityOverview({ data, breakdown, events, groupNames, eventFilter, setEventFilter, busy, onCheckin }: {
  busy: string | null;
  onCheckin: (groupId: string) => void;
  data: AffinityState;
  breakdown: Array<{ source: string; total: number; label: string; width: number }>;
  events: AffinityState["events"];
  groupNames: Map<string, string | null>;
  eventFilter: string;
  setEventFilter: (value: string) => void;
}): React.JSX.Element {
  return <>
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
        <MeSection icon={<CalendarOutlined />} title="各群签到" hint={<>在这里点“签到”和群里发 /签到 一样；连签越久奖励越多。想看自己在群里排第几，去<Link to="/me/insights">数据洞察</Link>。</>}>
          {data.groups.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还不在任何群里" /> : (
            <ul className="me-checkin-list">
              {data.groups.map((group) => (
                <li key={group.groupId} className={group.checkedToday ? "is-done" : undefined}>
                  <span className="me-plain-main"><strong>{group.groupName || `群 ${group.groupId}`}</strong><Text type="secondary" className="me-hint">累计 {group.totalDays} 天 · 积分 {group.points} · 经验 {group.exp}{group.lastCheckinDate ? ` · 上次 ${group.lastCheckinDate}` : ""}</Text></span>
                  {group.streakDays > 0 && <Tag icon={<FireOutlined />} color="orange">连签 {group.streakDays}</Tag>}
                  {group.checkedToday ? <Tag color="success">今天签过了</Tag> : <Button size="small" loading={busy === `checkin-${group.groupId}`} onClick={() => onCheckin(group.groupId)} aria-label={`在${group.groupName || group.groupId}签到`}>签到</Button>}
                </li>
              ))}
            </ul>
          )}
        </MeSection>
      </div>

      <MeSection icon={<HistoryOutlined />} title="最近的变化" extra={
        <Segmented size="small" value={eventFilter} onChange={setEventFilter} options={[{ value: "all", label: "全部" }, ...Object.entries(data.sources).map(([value, rule]) => ({ value, label: rule.label }))]} />
      }>
        {events.length > 0 && <Text type="secondary" className="me-hint">这 {events.length} 笔合计 {events.reduce((sum, event) => sum + event.delta, 0) >= 0 ? "+" : ""}{events.reduce((sum, event) => sum + event.delta, 0)}</Text>}
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
  </>;
}
