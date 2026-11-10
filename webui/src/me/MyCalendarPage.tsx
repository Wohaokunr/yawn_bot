import { BellOutlined, CalendarOutlined, CheckSquareOutlined, FireOutlined, HeartOutlined, LeftOutlined, PlusOutlined, RightOutlined } from "@ant-design/icons";
import { Button, Checkbox, Empty, Skeleton, Tag, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { ME_CHANGED_EVENT } from "./hub-ui";
import { type CalendarDay, type CalendarMonth, hubApi } from "./me-hub-api";
import { calendarCells, monthLabel, shiftMonth, WEEKDAY_LABELS, weekdayOf } from "./me-hub-meta";
import { spaceApi } from "./me-space-api";
import { moodEmoji, PRIORITY_META } from "./me-space-meta";
import { openCapture } from "./MeShellExtras";
import { MeSection, StatTile, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

type Layer = "todos" | "reminders" | "countdowns" | "mood" | "checkins";
const LAYERS: Array<{ key: Layer; label: string }> = [
  { key: "todos", label: "待办" },
  { key: "reminders", label: "提醒" },
  { key: "countdowns", label: "倒数日" },
  { key: "mood", label: "心情" },
  { key: "checkins", label: "签到" },
];

function dayCount(day: CalendarDay): number {
  return day.todos.length + day.reminders.length + day.countdowns.length;
}

function DayDetail({ day, today, onToggle, busy }: { day: CalendarDay; today: string; onToggle: (id: number, done: boolean) => void; busy: string | null }): React.JSX.Element {
  const empty = dayCount(day) === 0 && day.mood === null && !day.checkins && !day.done && !day.affinity;
  const label = day.date === today ? "今天" : `${Number(day.date.slice(5, 7))} 月 ${Number(day.date.slice(8))} 日 周${WEEKDAY_LABELS[weekdayOf(day.date)]}`;
  return <MeSection icon={<CalendarOutlined />} title={label} extra={
    <span className="me-chip-row">
      <Button size="small" icon={<PlusOutlined />} onClick={() => openCapture({ kind: "todo", date: day.date })}>这天的待办</Button>
      {day.date >= today && <Button size="small" onClick={() => openCapture({ kind: "countdown", date: day.date })}>设成倒数日</Button>}
    </span>
  }>
    {empty ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这天没有安排" /> : (
      <div className="me-stack">
        {day.todos.length > 0 && <ul className="me-plain-list">
          {day.todos.map((todo) => (
            <li key={todo.id}>
              <Checkbox checked={todo.done} disabled={busy === `cal-${todo.id}`} onChange={(event) => onToggle(todo.id, event.target.checked)} aria-label={`完成：${todo.content}`} />
              <span className={`me-plain-main${todo.done ? " me-struck" : ""}`}>{todo.content}</span>
              {todo.priority !== "normal" && <Tag color={PRIORITY_META[todo.priority].color}>{PRIORITY_META[todo.priority].label}</Tag>}
            </li>
          ))}
        </ul>}
        {day.reminders.length > 0 && <ul className="me-plain-list">
          {day.reminders.map((item) => <li key={item.ref}><BellOutlined aria-hidden="true" /><span className="me-plain-main">{item.content}</span>{item.repeat && <Tag>重复</Tag>}<Text type="secondary" className="me-hint me-mono">{item.time}</Text></li>)}
        </ul>}
        {day.countdowns.map((item) => <div key={item.id} className="me-cal-countdown"><CalendarOutlined aria-hidden="true" /> {item.title}</div>)}
        <div className="me-chip-row">
          {day.mood !== null && <Tag>{moodEmoji(day.mood)} 心情 {day.mood}{day.moodCount > 1 ? `（${day.moodCount} 次）` : ""}</Tag>}
          {day.checkins > 0 && <Tag icon={<FireOutlined />} color="orange">签了 {day.checkins} 个群</Tag>}
          {day.done > 0 && <Tag icon={<CheckSquareOutlined />} color="success">完成 {day.done} 件</Tag>}
          {day.affinity !== 0 && <Tag icon={<HeartOutlined />}>好感 {day.affinity > 0 ? "+" : ""}{day.affinity}</Tag>}
        </div>
      </div>
    )}
  </MeSection>;
}

export function MyCalendarPage(): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const month = params.get("month") ?? "";
  const { query, data, setData } = useResource<CalendarMonth>(["me", "calendar", month], () => hubApi.calendar(month || undefined));
  const { busy, run } = useAction();
  const [off, setOff] = useState<Set<Layer>>(new Set());
  const picked = params.get("day");
  const cells = useMemo(() => (data ? calendarCells(data.firstWeekday, data.days) : []), [data]);
  const selected = data?.days.find((day) => day.date === picked) ?? data?.days.find((day) => day.date === data.today) ?? null;

  // 随手记里加了东西，日历跟着刷新
  const reload = query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reload);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reload);
  }, [reload]);

  const patch = (changes: Record<string, string | null>): void => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next, { replace: true });
  };
  const go = (delta: number): void => { if (data) patch({ month: shiftMonth(data.month, delta), day: null }); };
  const toggle = async (id: number, done: boolean): Promise<void> => {
    if (!data) return;
    const ok = await run(`cal-${id}`, () => spaceApi.editTodo(id, { done }), done ? "完成一件！" : undefined);
    if (ok) setData({ ...data, days: data.days.map((day) => ({ ...day, todos: day.todos.map((todo) => (todo.id === id ? { ...todo, done } : todo)) })) });
  };
  const show = (layer: Layer): boolean => !off.has(layer);

  return <div className="me-page">
    <PageHeader title="日历" subtitle="到期的待办、要响的提醒、倒数日、每天的心情和签到，都摆在一张月历上。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 8 }} /> : <>
      <div className="me-stat-row">
        <StatTile label="这个月到期的待办" value={data.totals.todos} sub={`完成了 ${data.totals.done} 件`} tone="brand" />
        <StatTile label="提醒" value={data.totals.reminders} sub={data.totals.countdowns ? `${data.totals.countdowns} 个倒数日` : undefined} />
        <StatTile label="签到天数" value={data.totals.checkinDays} tone="warning" />
        <StatTile label="记了心情" value={`${data.totals.moodDays} 天`} sub={data.totals.affinity ? `好感 ${data.totals.affinity > 0 ? "+" : ""}${data.totals.affinity}` : undefined} />
      </div>
      <div className="me-cal-bar">
        <Button icon={<LeftOutlined />} aria-label="上个月" onClick={() => go(-1)} />
        <strong className="me-cal-month">{monthLabel(data.month)}</strong>
        <Button icon={<RightOutlined />} aria-label="下个月" onClick={() => go(1)} />
        <Button onClick={() => patch({ month: null, day: null })} disabled={data.month === data.today.slice(0, 7) && !picked}>回到今天</Button>
        <span className="me-toolbar-spacer" />
        <span className="me-chip-row" role="group" aria-label="显示哪些">
          {LAYERS.map((layer) => <Tag.CheckableTag key={layer.key} checked={show(layer.key)} onChange={(on) => { const next = new Set(off); if (on) next.delete(layer.key); else next.add(layer.key); setOff(next); }}>{layer.label}</Tag.CheckableTag>)}
        </span>
      </div>
      <div className="me-cal-layout">
        <div className="me-cal" role="grid" aria-label={monthLabel(data.month)}>
          {WEEKDAY_LABELS.map((label, index) => <div key={label} className={`me-cal-head${index >= 5 ? " is-weekend" : ""}`} role="columnheader">{label}</div>)}
          {cells.map((day, index) => (day === null ? <div key={`blank-${index}`} className="me-cal-cell is-blank" /> : (
            <button
              key={day.date}
              type="button"
              role="gridcell"
              aria-selected={selected?.date === day.date}
              aria-label={`${day.date}，${dayCount(day)} 件事`}
              className={`me-cal-cell${day.date === data.today ? " is-today" : ""}${selected?.date === day.date ? " is-selected" : ""}${day.date < data.today ? " is-past" : ""}`}
              onClick={() => patch({ day: day.date })}
            >
              <span className="me-cal-num">{Number(day.date.slice(8))}{show("mood") && day.mood !== null && <span className="me-cal-mood" aria-hidden="true">{moodEmoji(day.mood)}</span>}</span>
              <span className="me-cal-items">
                {show("countdowns") && day.countdowns.map((item) => <span key={`c${item.id}`} className="me-cal-chip is-countdown">{item.title}</span>)}
                {show("todos") && day.todos.slice(0, 2).map((todo) => <span key={`t${todo.id}`} className={`me-cal-chip is-todo${todo.done ? " is-done" : ""}${todo.priority === "high" ? " is-high" : ""}`}>{todo.content}</span>)}
                {show("reminders") && day.reminders.slice(0, 1).map((item) => <span key={item.ref} className="me-cal-chip is-reminder">{item.time} {item.content}</span>)}
                {(() => {
                  const hidden = (show("todos") ? Math.max(day.todos.length - 2, 0) : 0) + (show("reminders") ? Math.max(day.reminders.length - 1, 0) : 0);
                  return hidden > 0 ? <span className="me-cal-more">还有 {hidden}</span> : null;
                })()}
              </span>
              <span className="me-cal-dots" aria-hidden="true">
                {show("todos") && day.todos.some((todo) => !todo.done) && <i className="is-todo" />}
                {show("reminders") && day.reminders.length > 0 && <i className="is-reminder" />}
                {show("countdowns") && day.countdowns.length > 0 && <i className="is-countdown" />}
                {show("checkins") && day.checkins > 0 && <i className="is-checkin" />}
              </span>
            </button>
          )))}
        </div>
        <div className="me-cal-side">
          {selected && <DayDetail day={selected} today={data.today} busy={busy} onToggle={(id, done) => void toggle(id, done)} />}
          <Text type="secondary" className="me-hint">重复的提醒只标下一次要响的那天。全部提醒在<Link to="/me/schedules">我的定时任务</Link>里。</Text>
        </div>
      </div>
    </>}
  </div>;
}
