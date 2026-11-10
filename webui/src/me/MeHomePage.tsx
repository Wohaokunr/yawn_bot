import {
  AppstoreOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  BarChartOutlined,
  BellOutlined,
  CalendarOutlined as CalendarIcon,
  CheckCircleOutlined,
  CloseOutlined,
  GiftOutlined,
  SearchOutlined,
  SettingOutlined,
  StarFilled,
  StarOutlined,
  BulbOutlined,
  CalendarOutlined,
  CheckSquareOutlined,
  CommentOutlined,
  ControlOutlined,
  FieldTimeOutlined,
  FireOutlined,
  HeartOutlined,
  LockOutlined,
  ProfileOutlined,
  RightOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SmileOutlined,
  TeamOutlined,
  TrophyOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Checkbox, Empty, Input, Modal, Progress, Skeleton, Switch, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { affinityMeApi } from "../affinity/affinity-api";
import { questDone, questPercent, unclaimed } from "../affinity/affinity-meta";
import type { PerksState } from "../affinity/types";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { formatQqTime, groupRoleLabel } from "./me-meta";
import { type Dashboard, spaceApi } from "./me-space-api";
import { countdownLabel, dueLabel, greeting, MOOD_OPTIONS, moodEmoji, trendLabel } from "./me-space-meta";
import { ME_CHANGED_EVENT, useInbox, useUiPrefs } from "./hub-ui";
import { checkinSummary, hubApi, type Insights } from "./me-hub-api";
import { compare, onboardingSteps, parseQuickTodo, todayIso, WEEKDAY_LABELS, weekdayOf } from "./me-hub-meta";
import { MeSection, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

function TodayPanel({ data, onChanged }: { data: Dashboard; onChanged: () => void }): React.JSX.Element {
  const { todos, reminders, countdowns, checkin } = data;
  const { busy, run } = useAction();
  const [draft, setDraft] = useState("");
  const add = async (): Promise<void> => {
    if (!draft.trim()) return;
    const parsed = parseQuickTodo(draft, todayIso());
    const ok = await run("home-add", () => spaceApi.addTodo({ content: parsed.content, priority: parsed.priority, due: parsed.due }), "记下了");
    if (ok) {
      setDraft("");
      onChanged();
    }
  };
  const finish = async (id: number): Promise<void> => {
    if (await run(`home-done-${id}`, () => spaceApi.editTodo(id, { done: true }), "完成一件！")) onChanged();
  };
  const signAll = async (): Promise<void> => {
    if (await run("home-checkin", () => hubApi.checkin(), checkinSummary)) onChanged();
  };
  return <div className="me-today">
    <div className="me-today-card">
      <h3 className="me-today-head"><Link to="/me/assistant" className="me-today-link"><CheckSquareOutlined /> 待办 <strong>{todos.pending}</strong></Link>{todos.overdue > 0 && <Tag color="error" className="me-inline-tag">逾期 {todos.overdue}</Tag>}</h3>
      {todos.top.length === 0 ? <Text type="secondary" className="me-hint">没有要做的事</Text> : (
        <ul className="me-today-list">
          {todos.top.map((item) => <li key={item.id}>
            <Checkbox className="me-today-check" checked={false} disabled={busy === `home-done-${item.id}`} onChange={() => void finish(item.id)} aria-label={`完成：${item.content}`} />
            <span className="me-today-text">{item.content}</span>{dueLabel(item.dueInDays) && <Text type={item.overdue ? "danger" : "secondary"} className="me-hint">{dueLabel(item.dueInDays)}</Text>}
          </li>)}
        </ul>
      )}
      <Input size="small" variant="filled" className="me-today-add" placeholder="加一条，回车记下（可以写“明天 …”）" maxLength={120} value={draft} disabled={busy === "home-add"} onChange={(event) => setDraft(event.target.value)} onPressEnter={() => void add()} aria-label="快速加一条待办" />
    </div>
    <Link to="/me/schedules" className="me-today-card">
      <h3 className="me-today-head"><BellOutlined /> 接下来的提醒</h3>
      {reminders.length === 0 ? <Text type="secondary" className="me-hint">没有排着的提醒</Text> : (
        <ul className="me-today-list">
          {reminders.map((item) => <li key={item.ref}><span className="me-today-text">{item.content}</span><Text type="secondary" className="me-hint me-mono">{item.due.slice(5)}</Text></li>)}
        </ul>
      )}
    </Link>
    <Link to="/me/assistant?tab=countdowns" className="me-today-card">
      <h3 className="me-today-head"><CalendarOutlined /> 倒数日</h3>
      {countdowns.length === 0 ? <Text type="secondary" className="me-hint">没有临近的日子</Text> : (
        <ul className="me-today-list">
          {countdowns.map((item) => <li key={item.id}><span className="me-today-text">{item.title}</span><Text type="secondary" className="me-hint">{countdownLabel(item.daysLeft)}</Text></li>)}
        </ul>
      )}
    </Link>
    <div className="me-today-card">
      <h3 className="me-today-head"><Link to="/me/affinity" className="me-today-link"><FireOutlined /> 签到</Link></h3>
      <span><strong>{checkin.checkedToday}/{checkin.groups}</strong> <Text type="secondary">个群今天签过</Text></span>
      {checkin.pendingGroups.length > 0 ? <Text type="secondary" className="me-hint">还没签：{checkin.pendingGroups.map((group) => group.groupName || group.groupId).join("、")}</Text> : checkin.groups > 0 ? <Text type="success" className="me-hint">全签完啦</Text> : null}
      {checkin.bestStreak > 0 && <Text type="secondary" className="me-hint">最长连签 {checkin.bestStreak} 天</Text>}
      {checkin.groups > checkin.checkedToday && <Button size="small" type="primary" className="me-today-action" loading={busy === "home-checkin"} onClick={() => void signAll()}>一键签到</Button>}
    </div>
  </div>;
}

/** 右侧“我和 Bot”卡：好感等级 + 今天心情，一眼看完不用再滚。 */
function BondPanel({ data, onMood }: { data: Dashboard; onMood: (score: number) => void }): React.JSX.Element {
  const { mood, affinity } = data;
  const percent = Math.round(affinity.progress * 100);
  return <div className="me-bond">
    <Link to="/me/affinity" className="me-bond-affinity">
      <span className="me-bond-label"><HeartOutlined /> 和 Bot 的关系</span>
      <span className="me-bond-level">
        <strong>{affinity.level}</strong>
        {affinity.today > 0 && <Text type="success" className="me-hint">今天 +{affinity.today}</Text>}
      </span>
      <Progress className="me-affinity-progress" percent={percent} size="small" showInfo={false} strokeColor="var(--brand)" />
      <Text type="secondary" className="me-hint">{affinity.nextLevel ? `下一级「${affinity.nextLevel}」· 进度 ${percent}%` : "已经是最高级"}</Text>
    </Link>
    <div className="me-bond-mood">
      <span className="me-bond-label"><SmileOutlined /> 今天心情{mood.loggedToday && mood.last && <span className="me-inline-tag">{moodEmoji(mood.last.score)}</span>}</span>
      <div className="me-mood-mini" role="group" aria-label="记一下现在的心情">
        {MOOD_OPTIONS.map((option) => (
          <Tooltip key={option.score} title={option.label}>
            <button type="button" className={`me-mood-mini-btn${mood.loggedToday && mood.last?.score === option.score ? " is-active" : ""}`} onClick={() => onMood(option.score)} aria-label={option.label} aria-pressed={mood.loggedToday && mood.last?.score === option.score}>{option.emoji}</button>
          </Tooltip>
        ))}
      </div>
      <Text type="secondary" className="me-hint">{mood.week.count ? `这周 ${mood.week.averageLabel} · ${trendLabel(mood.week.trend)}` : "点一下就记下"}</Text>
    </div>
  </div>;
}

/** 提醒中心里最要紧的几条，直接摆在主页最上面。 */
function NoticeStrip(): React.JSX.Element | null {
  const { inbox } = useInbox();
  const urgent = (inbox?.items ?? []).filter((item) => item.tone === "danger" || item.tone === "warning").slice(0, 3);
  if (urgent.length === 0) return null;
  return <div className="me-home-notices" role="list" aria-label="等你处理">
    {urgent.map((item) => (
      <Link key={item.id} to={item.to} role="listitem" className={`me-home-notice is-${item.tone}`}>
        <span className="me-notice-dot" aria-hidden="true" />
        <span className="me-plain-main"><strong>{item.title}</strong>{item.detail && <Text type="secondary" className="me-hint">{item.detail}</Text>}</span>
        <RightOutlined className="me-card-arrow" />
      </Link>
    ))}
  </div>;
}

/** 新手清单：做完或点掉后不再出现（跟着账号走）。 */
function Onboarding({ data, perks, onDismiss }: { data: Dashboard; perks: PerksState | null; onDismiss: () => void }): React.JSX.Element | null {
  const [callMe, setCallMe] = useState(false);
  useEffect(() => {
    let alive = true;
    spaceApi.chat().then((chat) => { if (alive) setCallMe(Boolean(chat.prefs.callMe)); }).catch(() => undefined);
    return () => { alive = false; };
  }, []);
  let visitedMemory = false;
  try {
    visitedMemory = window.localStorage.getItem("yawnbot.me.visited.memory") === "1";
  } catch {
    visitedMemory = false;
  }
  const steps = onboardingSteps({
    todos: data.counts.todos ?? 0,
    notes: data.counts.notes ?? 0,
    moods: data.counts.moods ?? 0,
    checkedToday: data.checkin.checkedToday,
    callMe,
    hasFacts: visitedMemory,
    levelIndex: perks?.levelIndex ?? 0,
  });
  const done = steps.filter((step) => step.done).length;
  if (done === steps.length) return null;
  return <MeSection icon={<CheckCircleOutlined />} title={`上手清单 ${done}/${steps.length}`} hint="做完这几件，基本就把这里用熟了。" extra={<Button type="text" size="small" icon={<CloseOutlined />} onClick={onDismiss} aria-label="不再显示上手清单">不再显示</Button>}>
    <Progress percent={Math.round((done / steps.length) * 100)} size="small" showInfo={false} strokeColor="var(--brand)" aria-label="上手进度" />
    <ul className="me-onboard-list">
      {steps.map((step) => (
        <li key={step.key} className={step.done ? "is-done" : undefined}>
          <Link to={step.to} className="me-onboard-step">
            <span className="me-onboard-mark" aria-hidden="true">{step.done ? "✓" : ""}</span>
            <span className="me-plain-main"><strong>{step.title}</strong><Text type="secondary" className="me-hint">{step.hint}</Text></span>
          </Link>
        </li>
      ))}
    </ul>
  </MeSection>;
}

/** 每日任务、签到积分、今日心仪：好感玩法在主页的一小块。 */
function QuestCard({ perks, onChange }: { perks: PerksState; onChange: (next: PerksState) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const quests = perks.quests;
  const favorite = perks.gifts.menu.find((gift) => gift.key === perks.gifts.favorite);
  const giftsLeft = Math.max(perks.gifts.dailyLimit - perks.gifts.sentToday, 0);
  const claim = async (): Promise<void> => {
    const response = await run("home-claim", () => affinityMeApi.claim(), (r) => (r.quests.claimed.length ? `领好了：好感 +${r.quests.affinity}${r.quests.bonus_item ? `，送你一张${r.quests.bonus_item}` : ""}` : "没有可领的"));
    if (response) onChange(response.state);
  };
  return <MeSection icon={<GiftOutlined />} title="今日任务" extra={<Link to="/me/affinity?tab=play">任务·送礼·商店 <RightOutlined /></Link>}>
    <div className="me-quest-card">
      <div className="me-quest-main">
        {!perks.enabled || !quests.enabled ? <Text type="secondary" className="me-hint">每日任务没开。</Text> : <>
          <Progress percent={questPercent(quests)} size="small" strokeColor="var(--brand)" format={() => `${questDone(quests)}/${quests.items.length}`} aria-label="今日任务进度" />
          <ul className="me-quest-list">
            {quests.items.map((item) => <li key={item.key} className={item.done ? "is-done" : undefined}><span>{item.done ? "✓" : "○"} {item.title}</span><Text type="secondary" className="me-hint">{item.progress}/{item.target}{item.claimed ? " · 已领" : ""}</Text></li>)}
          </ul>
          {unclaimed(quests) > 0 && <Button size="small" type="primary" loading={busy === "home-claim"} onClick={() => void claim()}>领取 {unclaimed(quests)} 个奖励</Button>}
        </>}
      </div>
      <dl className="me-quest-side">
        <div><dt>签到积分</dt><dd>{perks.wallet.total}</dd></div>
        <div><dt>今天还能送礼</dt><dd>{giftsLeft} 次</dd></div>
        {favorite && <div><dt>今日心仪</dt><dd>{favorite.emoji} {favorite.name}</dd></div>}
        {perks.bag.shield_until && <div><dt>好感护盾</dt><dd>到 {perks.bag.shield_until.slice(5)}</dd></div>}
        {perks.bag.lucky_today && <div><dt>暴击卡</dt><dd>今天生效</dd></div>}
      </dl>
    </div>
  </MeSection>;
}

/** 最近七天：每天来没来、说了几句，和上周比。 */
function WeekStrip({ data }: { data: Insights }): React.JSX.Element {
  const week = data.series.slice(-7);
  const top = Math.max(1, ...week.map((row) => row.mine));
  return <MeSection icon={<BarChartOutlined />} title="这七天" extra={<Link to="/me/insights">数据洞察 <RightOutlined /></Link>}>
    <div className="me-week">
      <div className="me-week-days" role="img" aria-label="最近七天的活跃情况">
        {week.map((row) => {
          const active = row.mine > 0 || row.checkins > 0 || row.todosDone > 0;
          return <Tooltip key={row.day} title={`${row.day.slice(5)}：说了 ${row.mine} 句，完成 ${row.todosDone} 件，签到 ${row.checkins} 次${row.mood !== null ? `，心情 ${row.mood}` : ""}`}>
            <span className={`me-week-day${active ? " is-active" : ""}${row.day === data.today ? " is-today" : ""}`}>
              <span className="me-week-bar"><i style={{ height: `${Math.round((row.mine / top) * 100)}%` }} /></span>
              <span className="me-week-mood" aria-hidden="true">{row.mood !== null ? moodEmoji(row.mood) : row.checkins ? "🔥" : "·"}</span>
              <span className="me-week-label">{row.day === data.today ? "今" : WEEKDAY_LABELS[weekdayOf(row.day)]}</span>
            </span>
          </Tooltip>;
        })}
      </div>
      <dl className="me-week-stats">
        <div><dt>说了</dt><dd>{data.thisWeek.mine} 句</dd><Text type="secondary" className="me-hint">{compare(data.thisWeek.mine, data.lastWeek?.mine).label}</Text></div>
        <div><dt>完成待办</dt><dd>{data.thisWeek.todosDone}</dd><Text type="secondary" className="me-hint">{compare(data.thisWeek.todosDone, data.lastWeek?.todosDone).label}</Text></div>
        <div><dt>好感</dt><dd>{data.thisWeek.affinity >= 0 ? "+" : ""}{data.thisWeek.affinity}</dd><Text type="secondary" className="me-hint">来了 {data.thisWeek.activeDays} 天</Text></div>
      </dl>
    </div>
  </MeSection>;
}

const BLOCK_LABEL: Record<string, string> = {
  notices: "等你处理",
  onboarding: "上手清单",
  today: "今天",
  quests: "今日任务",
  week: "这七天",
  groups: "我管理的群",
  more: "全部入口",
};
const DEFAULT_ORDER = ["notices", "onboarding", "today", "quests", "week", "groups", "more"];

/** 排序：用户排过的在前，没排过的按默认顺序补在后面。 */
export function resolveBlockOrder(saved: string[] | undefined): string[] {
  const known = (saved ?? []).filter((key) => DEFAULT_ORDER.includes(key));
  return [...known, ...DEFAULT_ORDER.filter((key) => !known.includes(key))];
}

function CustomizeModal({ open, order, hidden, onClose, onSave }: { open: boolean; order: string[]; hidden: string[]; onClose: () => void; onSave: (order: string[], hidden: string[]) => void }): React.JSX.Element {
  const [draftOrder, setDraftOrder] = useState(order);
  const [draftHidden, setDraftHidden] = useState(hidden);
  useEffect(() => {
    if (open) {
      setDraftOrder(order);
      setDraftHidden(hidden);
    }
  }, [open, order, hidden]);
  const move = (index: number, delta: number): void => {
    const target = index + delta;
    if (target < 0 || target >= draftOrder.length) return;
    const next = [...draftOrder];
    [next[index], next[target]] = [next[target], next[index]];
    setDraftOrder(next);
  };
  return <Modal open={open} title="自定义主页" okText="保存" onCancel={onClose} onOk={() => { onSave(draftOrder, draftHidden); onClose(); }} footer={(_node, { OkBtn, CancelBtn }) => <><Button type="link" onClick={() => { setDraftOrder(DEFAULT_ORDER); setDraftHidden([]); }}>恢复默认</Button><CancelBtn /><OkBtn /></>}>
    <Text type="secondary" className="me-hint">选要显示哪些板块、谁排在前面。这个设置跟着账号走，换设备也一样。</Text>
    <ul className="me-customize-list">
      {draftOrder.map((key, index) => (
        <li key={key}>
          <Switch size="small" checked={!draftHidden.includes(key)} onChange={(on) => setDraftHidden(on ? draftHidden.filter((item) => item !== key) : [...draftHidden, key])} aria-label={`显示${BLOCK_LABEL[key]}`} />
          <span className="me-plain-main">{BLOCK_LABEL[key]}</span>
          <Button type="text" size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => move(index, -1)} aria-label={`${BLOCK_LABEL[key]}上移`} />
          <Button type="text" size="small" icon={<ArrowDownOutlined />} disabled={index === draftOrder.length - 1} onClick={() => move(index, 1)} aria-label={`${BLOCK_LABEL[key]}下移`} />
        </li>
      ))}
    </ul>
  </Modal>;
}

// 首页“更多”：今天面板和“我管理的群”里已经露出的入口不再重复一遍。
const HOME_HIDDEN_ENTRIES = new Set(["/me/assistant", "/me/schedules", "/me/affinity", "/me/groups"]);

const ENTRIES: Array<{ to: string; icon: React.ReactNode; title: string; sub: (data: Dashboard | null) => string }> = [
  { to: "/me/assistant", icon: <ProfileOutlined />, title: "小本本", sub: (d) => (d ? `${d.counts.todos ?? 0} 条待办 · ${d.counts.notes ?? 0} 条笔记 · ${d.counts.countdowns ?? 0} 个倒数日` : "待办、笔记、倒数日、心情") },
  { to: "/me/schedules", icon: <FieldTimeOutlined />, title: "我的定时任务", sub: (d) => (d ? `${d.counts.reminders ?? 0} 条私聊提醒 · 群定时任务` : "私聊提醒 + 群定时任务") },
  { to: "/me/tasks", icon: <RobotOutlined />, title: "后台小助手", sub: (d) => (d ? (d.tasks.running ? `${d.tasks.running} 个在跑${d.tasks.waitingYou ? `，${d.tasks.waitingYou} 个等你回答` : ""}` : `做过 ${d.tasks.total} 个任务`) : "在后台帮你做事") },
  { to: "/me/chat", icon: <ControlOutlined />, title: "私聊设置", sub: (d) => (d ? (d.chat.paused ? "自动回复已暂停" : `今天聊了 ${d.chat.turnsToday}/${d.chat.dailyTurns} 轮`) : "称呼、免打扰、主动提议") },
  { to: "/me/persona", icon: <SmileOutlined />, title: "私聊人设", sub: () => "Bot 和你私聊时的自称和性格" },
  { to: "/me/memory", icon: <BulbOutlined />, title: "Bot 记得我", sub: () => "看看它记住了什么，随时让它忘掉" },
  { to: "/me/chats", icon: <CommentOutlined />, title: "聊天记录", sub: () => "翻看、搜索、导出和 Bot 的对话" },
  { to: "/me/groups", icon: <TeamOutlined />, title: "我在的群", sub: (d) => (d ? `${d.checkin.groups} 个群` : "身份、签到、本群功能") },
  { to: "/me/affinity", icon: <HeartOutlined />, title: "好感与签到", sub: (d) => (d ? `现在是「${d.affinity.level}」${d.affinity.today ? `，今天 +${d.affinity.today}` : ""}` : "好感等级与各群连签") },
  { to: "/me/games", icon: <TrophyOutlined />, title: "狼人杀战绩", sub: () => "胜率、常玩角色、最近对局" },
  { to: "/me/calendar", icon: <CalendarIcon />, title: "日历", sub: (d) => (d ? `今天到期 ${d.todos.dueToday} 件 · ${d.counts.countdowns ?? 0} 个倒数日` : "一个月的安排摆在一起") },
  { to: "/me/search", icon: <SearchOutlined />, title: "搜一搜", sub: () => "待办、笔记、记忆、聊天记录一次搜遍" },
  { to: "/me/insights", icon: <BarChartOutlined />, title: "数据洞察", sub: () => "聊天、待办、心情走势，各群名次" },
  { to: "/me/achievements", icon: <StarOutlined />, title: "成就", sub: () => "看看达成了哪些，还差多少" },
  { to: "/me/account", icon: <LockOutlined />, title: "账号与数据", sub: () => "改密码、登录设备、显示偏好、导出数据" },
];

export function MeHomePage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const profile = useApiQuery({ queryKey: ["me"], fetcher: () => meApi.profile() });
  const groups = useApiQuery({ queryKey: ["me", "groups"], fetcher: () => meApi.groups() });
  const dash = useResource<Dashboard>(["me", "dashboard"], () => spaceApi.dashboard());
  const perks = useResource<PerksState>(["me", "affinity", "perks"], () => affinityMeApi.perks());
  const week = useResource<Insights>(["me", "insights", 14], () => hubApi.insights(14));
  const { prefs, save } = useUiPrefs();
  const [customizing, setCustomizing] = useState(false);
  const me = profile.data;
  const data = dash.data;
  const hour = data ? Number(data.now.slice(11, 13)) : new Date().getHours();
  const title = me?.nickname ? `${greeting(hour)}，${me.nickname}` : greeting(hour);
  const order = useMemo(() => resolveBlockOrder(prefs?.homeOrder), [prefs]);
  const hidden = prefs?.homeHidden ?? [];
  const starred = prefs?.starred ?? [];

  const reloadAll = (): void => { profile.reload(); groups.reload(); dash.query.reload(); perks.query.reload(); week.query.reload(); };
  // 随手记里记了东西，主页跟着刷新
  const reloadDash = dash.query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reloadDash);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reloadDash);
  }, [reloadDash]);

  const logMood = async (score: number): Promise<void> => {
    try {
      await spaceApi.logMood(score);
      message.success(`记下了：${MOOD_OPTIONS.find((item) => item.score === score)?.label ?? ""}`);
      dash.query.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const toggleStar = (to: string): void => save({ starred: starred.includes(to) ? starred.filter((item) => item !== to) : [...starred, to].slice(0, 12) });
  const entries = useMemo(() => {
    const visible = ENTRIES.filter((entry) => !HOME_HIDDEN_ENTRIES.has(entry.to) || starred.includes(entry.to));
    return [...visible.filter((entry) => starred.includes(entry.to)), ...visible.filter((entry) => !starred.includes(entry.to))];
  }, [starred]);

  const blocks: Record<string, React.ReactNode> = {
    notices: <NoticeStrip key="notices" />,
    onboarding: data && prefs && !prefs.onboardingDismissed ? <Onboarding key="onboarding" data={data} perks={perks.data} onDismiss={() => save({ onboardingDismissed: true })} /> : null,
    today: !data ? <Skeleton key="today" active paragraph={{ rows: 4 }} /> : (
      <div key="today" className="me-home-top">
        <TodayPanel data={data} onChanged={() => { dash.query.reload(); perks.query.reload(); }} />
        <BondPanel data={data} onMood={(score) => void logMood(score)} />
      </div>
    ),
    quests: perks.data ? <QuestCard key="quests" perks={perks.data} onChange={perks.setData} /> : null,
    week: week.data ? <WeekStrip key="week" data={week.data} /> : null,
    groups: (groups.data?.length ?? 0) > 0 ? (
      <MeSection key="groups" icon={<TeamOutlined />} title="我管理的群" extra={<Link to="/me/groups">全部群 <RightOutlined /></Link>} hint={me?.isSuperuser ? "你是超级管理员：Bot 在的群都列在这里（Bot 自己是管理员的群才能做禁言等操作）" : "打开时会再向 QQ 确认一次身份"}>
        <ul className="me-group-list">
          {(groups.data ?? []).map((group) => (
            <li key={group.groupId}>
              <Link to={`/me/groups/${group.groupId}`} className="me-group-row">
                <span className="me-group-avatar" aria-hidden="true">{(group.groupName || "群").slice(0, 1)}</span>
                <span className="me-group-main">
                  <span className="me-group-name">{group.groupName || `群 ${group.groupId}`}</span>
                  <span className="me-group-meta">
                    <Tag className={`me-role-tag is-${group.role}`}>{groupRoleLabel(group.role)}</Tag>
                    <span>{group.memberCount} 名成员</span>
                    <span>·</span>
                    <span>开着 {group.enabledFeatures} / {group.featureCount} 项群功能</span>
                    {!group.agentEnabled && <Tag className="me-inline-tag">群里的 AI 聊天关着</Tag>}
                  </span>
                </span>
                <RightOutlined className="me-card-arrow" />
              </Link>
            </li>
          ))}
        </ul>
      </MeSection>
    ) : groups.data && groups.data.length === 0 && profile.data && !profile.data.isSuperuser ? (
      <Empty key="groups" image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span>你目前不是任何 Bot 所在群的群主或管理员，<Link to="/me/groups">看看我在的群</Link>。</span>} />
    ) : null,
    more: (
      <MeSection key="more" icon={<AppstoreOutlined />} title="更多" className="me-more-section" hint="点星标把常用的排到前面。">
        <div className="me-more-grid">
          {entries.map((entry) => (
            <div key={entry.to} className="me-more-item">
              <Link to={entry.to} className="me-more-link">
                <span className="me-card-icon">{entry.icon}</span>
                <span className="me-card-body">
                  <strong>{entry.title}</strong>
                  <Text type="secondary" className="me-card-sub" ellipsis>{entry.to === "/me/persona" && me?.persona ? `${me.persona.name || "默认人设"} · ${me.persona.sourceLabel}` : entry.to === "/me/account" && me?.account ? `上次登录 ${formatQqTime(me.account.lastLoginAt)}` : entry.sub(data)}</Text>
                </span>
              </Link>
              <Tooltip title={starred.includes(entry.to) ? "取消常用" : "设为常用"}>
                <button type="button" className={`me-star${starred.includes(entry.to) ? " is-on" : ""}`} onClick={() => toggleStar(entry.to)} aria-label={`${starred.includes(entry.to) ? "取消常用" : "设为常用"}：${entry.title}`} aria-pressed={starred.includes(entry.to)}>
                  {starred.includes(entry.to) ? <StarFilled /> : <StarOutlined />}
                </button>
              </Tooltip>
            </div>
          ))}
        </div>
      </MeSection>
    ),
  };

  return <div className="me-page">
    <PageHeader
      title={title}
      subtitle={me ? (me.isSuperuser ? `QQ ${me.userId} · 超级管理员，Bot 在的 ${me.managedGroupCount} 个群都能管` : `QQ ${me.userId} · 你在 ${me.groupCount} 个群里，其中 ${me.managedGroupCount} 个是群主/管理员`) : "正在读取你的信息"}
      onRefresh={reloadAll}
      refreshing={profile.refreshing || groups.refreshing || dash.query.refreshing}
      extra={<Button icon={<SettingOutlined />} onClick={() => setCustomizing(true)} aria-label="自定义主页"><span className="me-wide-only">自定义主页</span></Button>}
    />
    {profile.error && !me && <QueryErrorAlert error={profile.error} onRetry={profile.reload} />}
    {dash.query.error && !data && <QueryErrorAlert error={dash.query.error} onRetry={dash.query.reload} />}
    {data?.chat.paused && <Alert type="warning" showIcon message="你暂停了 Bot 的私聊自动回复" action={<Link to="/me/chat">去恢复</Link>} />}
    {data && data.tasks.waitingYou > 0 && <Alert type="info" showIcon message={`有 ${data.tasks.waitingYou} 个后台任务在等你回答`} description="在私聊里直接回复 Bot 就行。" action={<Link to="/me/tasks">看看</Link>} />}

    {order.filter((key) => !hidden.includes(key)).map((key) => blocks[key])}
    {hidden.length > 0 && <Text type="secondary" className="me-hint">有 {hidden.length} 个板块被你收起来了，<Button type="link" size="small" onClick={() => setCustomizing(true)}>去调整</Button></Text>}

    <p className="me-safety-note">
      <SafetyCertificateOutlined /> 这里只有你自己的东西：小本本、记忆、聊天记录只有你和 Bot 看得到；群功能开关和群里的 /面板 是同一份设置；忘记密码就私聊 Bot 发 /网页密码。
    </p>
    <CustomizeModal open={customizing} order={order} hidden={hidden} onClose={() => setCustomizing(false)} onSave={(nextOrder, nextHidden) => save({ homeOrder: nextOrder, homeHidden: nextHidden })} />
  </div>;
}
