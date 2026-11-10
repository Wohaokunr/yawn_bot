import { BarChartOutlined, BulbOutlined, ClockCircleOutlined, CommentOutlined, CrownOutlined, HeartOutlined, SmileOutlined } from "@ant-design/icons";
import { Empty, Segmented, Skeleton, Tag, Typography } from "antd";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { Columns, DeltaNote, Heatmap, HeatLegend } from "./hub-ui";
import { hubApi, type Insights } from "./me-hub-api";
import { compare, heatColumns, hourLabel, insightHighlights, WEEKDAY_LABELS } from "./me-hub-meta";
import { moodEmoji } from "./me-space-meta";
import { MeSection, StatTile, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;
const RANGES = [14, 30, 90] as const;

export function MyInsightsPage(): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const raw = Number(params.get("days"));
  const days = (RANGES as readonly number[]).includes(raw) ? raw : 30;
  const { query, data } = useResource<Insights>(["me", "insights", days], () => hubApi.insights(days));
  const activity = useMemo(() => {
    if (!data) return { columns: [], max: 0 };
    const values = Object.fromEntries(data.series.map((row) => [row.day, row.mine + row.todosDone * 2 + row.checkins * 2]));
    return { columns: heatColumns(data.today, data.days, values), max: Math.max(0, ...Object.values(values)) };
  }, [data]);
  const highlights = data ? insightHighlights(data.thisWeek, data.lastWeek, data.busiestHour, data.activeStreak) : [];
  const tail = data ? data.series.slice(-Math.min(data.days, 30)) : [];

  return <div className="me-page">
    <PageHeader title="数据洞察" subtitle="你最近和 Bot 的来往：聊了多少、做完多少事、心情怎么样、在各群排第几。只有你自己看得到。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    <Segmented<number> value={days} onChange={(value) => { const next = new URLSearchParams(params); if (value === 30) next.delete("days"); else next.set("days", String(value)); setParams(next, { replace: true }); }} options={RANGES.map((value) => ({ value, label: `最近 ${value} 天` }))} aria-label="时间范围" />
    {!data ? <Skeleton active paragraph={{ rows: 8 }} /> : <>
      <div className="me-stat-row">
        <StatTile label="这周说了" value={`${data.thisWeek.mine} 句`} sub={<DeltaNote delta={compare(data.thisWeek.mine, data.lastWeek?.mine)} />} tone="brand" />
        <StatTile label="这周完成待办" value={data.thisWeek.todosDone} sub={<DeltaNote delta={compare(data.thisWeek.todosDone, data.lastWeek?.todosDone)} />} tone="success" />
        <StatTile label="这周签到" value={`${data.thisWeek.checkins} 次`} sub={<DeltaNote delta={compare(data.thisWeek.checkins, data.lastWeek?.checkins)} />} tone="warning" />
        <StatTile label="这周好感" value={`${data.thisWeek.affinity >= 0 ? "+" : ""}${data.thisWeek.affinity}`} sub={<DeltaNote delta={compare(data.thisWeek.affinity, data.lastWeek?.affinity)} />} />
        <StatTile label="这周心情" value={data.thisWeek.mood === null ? "—" : `${moodEmoji(data.thisWeek.mood)} ${data.thisWeek.mood}`} sub={data.lastWeek?.mood != null ? `上周 ${data.lastWeek.mood}` : "上周没记"} />
      </div>

      {highlights.length > 0 && (
        <MeSection icon={<BulbOutlined />} title="一句话小结">
          <ul className="me-highlight-list">{highlights.map((line) => <li key={line}>{line}</li>)}</ul>
        </MeSection>
      )}

      <MeSection icon={<BarChartOutlined />} title="活跃日历" hint={`聊天、完成待办、签到都算。${data.totals.activeDays}/${data.days} 天来过，最长连续 ${data.activeStreak} 天。`} extra={<HeatLegend />}>
        <Heatmap columns={activity.columns} max={activity.max} label="每天的活跃程度" tip={(cell) => `${cell.day}：${cell.value ? "来过" : "没来"}`} />
      </MeSection>

      <div className="me-two-col">
        <MeSection icon={<CommentOutlined />} title="每天说了几句" hint={data.bestDay ? `最多的一天是 ${data.bestDay.day.slice(5)}，说了 ${data.bestDay.mine} 句` : "这段时间还没聊过"}>
          <Columns values={tail.map((row) => row.mine)} label="每天和 Bot 说的话" tip={(value, index) => `${tail[index].day.slice(5)}：${value ?? 0} 句`} />
        </MeSection>
        <MeSection icon={<ClockCircleOutlined />} title="一天里什么时候找 Bot" hint={data.busiestHour === null ? "还没有数据" : `最常在${hourLabel(data.busiestHour)}`}>
          <Columns values={data.hours} labels={data.hours.map((_value, hour) => (hour % 6 === 0 ? String(hour) : ""))} label="按小时的聊天分布" tip={(value, hour) => `${hour} 点：${value ?? 0} 句`} tone={(_value, hour) => (hour === data.busiestHour ? "is-peak" : "")} />
        </MeSection>
        <MeSection icon={<SmileOutlined />} title="心情走势" hint="柱子越高心情越好；空着的是那天没记">
          <Columns values={tail.map((row) => row.mood)} max={5} label="每天的心情" tip={(value, index) => `${tail[index].day.slice(5)}：${value === null ? "没记" : `${moodEmoji(value)} ${value}`}`} tone={(value) => (value !== null && value < 2.5 ? "is-low" : "")} />
        </MeSection>
        <MeSection icon={<HeartOutlined />} title="好感变化" hint={`这段时间合计 ${data.totals.affinity >= 0 ? "+" : ""}${data.totals.affinity}`}>
          <Columns values={tail.map((row) => Math.max(row.affinity, 0))} label="每天涨的好感" tip={(_value, index) => `${tail[index].day.slice(5)}：${tail[index].affinity >= 0 ? "+" : ""}${tail[index].affinity}`} />
        </MeSection>
      </div>

      <MeSection icon={<BarChartOutlined />} title="星期几最爱聊">
        <Columns values={data.weekdays} labels={WEEKDAY_LABELS.map((label) => `周${label}`)} label="按星期的聊天分布" tip={(value, index) => `周${WEEKDAY_LABELS[index]}：${value ?? 0} 句`} />
      </MeSection>

      <MeSection icon={<CrownOutlined />} title="我在各群排第几" hint="只显示你自己的名次和群里的人数，看不到别人是谁。">
        {data.ranks.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还不在任何群里" /> : (
          <ul className="me-plain-list">
            {data.ranks.map((row) => (
              <li key={row.groupId}>
                <span className="me-plain-main"><strong>{row.groupName || `群 ${row.groupId}`}</strong><Text type="secondary" className="me-hint">{row.members} 人 · 累计签到 {row.totalDays} 天{row.streakDays ? ` · 连签 ${row.streakDays}` : ""}</Text></span>
                {row.affinityRank ? <Tag color={row.affinityRank <= 3 ? "magenta" : "default"}>群好感 第 {row.affinityRank} 名</Tag> : <Tag>群好感还没上榜</Tag>}
                {row.pointsRank ? <Tag color={row.pointsRank <= 3 ? "gold" : "default"}>积分 第 {row.pointsRank} 名</Tag> : null}
              </li>
            ))}
          </ul>
        )}
        <Text type="secondary" className="me-hint">想看成就和进度，去<Link to="/me/achievements">成就</Link>。</Text>
      </MeSection>
    </>}
  </div>;
}
