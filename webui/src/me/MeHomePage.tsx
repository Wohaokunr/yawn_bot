import {
  BellOutlined,
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
import { Alert, App as AntApp, Empty, Progress, Skeleton, Tag, Tooltip, Typography } from "antd";
import { Link } from "react-router-dom";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { formatQqTime, groupRoleLabel } from "./me-meta";
import { type Dashboard, spaceApi } from "./me-space-api";
import { countdownLabel, dueLabel, greeting, MOOD_OPTIONS, moodEmoji, trendLabel } from "./me-space-meta";
import { MeSection, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text } = Typography;

function TodayPanel({ data, onMood }: { data: Dashboard; onMood: (score: number) => void }): React.JSX.Element {
  const { todos, reminders, countdowns, mood, checkin } = data;
  return <div className="me-today">
    <Link to="/me/assistant" className="me-today-card">
      <span className="me-today-head"><CheckSquareOutlined /> 待办 <strong>{todos.pending}</strong>{todos.overdue > 0 && <Tag color="error" className="me-inline-tag">逾期 {todos.overdue}</Tag>}</span>
      {todos.top.length === 0 ? <Text type="secondary" className="me-hint">没有要做的事</Text> : (
        <ul className="me-today-list">
          {todos.top.map((item) => <li key={item.id}><span className="me-today-text">{item.content}</span>{dueLabel(item.dueInDays) && <Text type={item.overdue ? "danger" : "secondary"} className="me-hint">{dueLabel(item.dueInDays)}</Text>}</li>)}
        </ul>
      )}
    </Link>
    <Link to="/me/schedules" className="me-today-card">
      <span className="me-today-head"><BellOutlined /> 接下来的提醒</span>
      {reminders.length === 0 ? <Text type="secondary" className="me-hint">没有排着的提醒</Text> : (
        <ul className="me-today-list">
          {reminders.map((item) => <li key={item.ref}><span className="me-today-text">{item.content}</span><Text type="secondary" className="me-hint me-mono">{item.due.slice(5)}</Text></li>)}
        </ul>
      )}
    </Link>
    <Link to="/me/assistant?tab=countdowns" className="me-today-card">
      <span className="me-today-head"><CalendarOutlined /> 倒数日</span>
      {countdowns.length === 0 ? <Text type="secondary" className="me-hint">没有临近的日子</Text> : (
        <ul className="me-today-list">
          {countdowns.map((item) => <li key={item.id}><span className="me-today-text">{item.title}</span><Text type="secondary" className="me-hint">{countdownLabel(item.daysLeft)}</Text></li>)}
        </ul>
      )}
    </Link>
    <div className="me-today-card">
      <span className="me-today-head"><SmileOutlined /> 今天心情{mood.loggedToday && mood.last && <span className="me-inline-tag">{moodEmoji(mood.last.score)}</span>}</span>
      <div className="me-mood-mini" role="group" aria-label="记一下现在的心情">
        {MOOD_OPTIONS.map((option) => (
          <Tooltip key={option.score} title={option.label}>
            <button type="button" className={`me-mood-mini-btn${mood.loggedToday && mood.last?.score === option.score ? " is-active" : ""}`} onClick={() => onMood(option.score)} aria-label={option.label}>{option.emoji}</button>
          </Tooltip>
        ))}
      </div>
      <Text type="secondary" className="me-hint">{mood.week.count ? `这周 ${mood.week.averageLabel} · ${trendLabel(mood.week.trend)}` : "点一下就记下"}</Text>
    </div>
    <Link to="/me/affinity" className="me-today-card">
      <span className="me-today-head"><FireOutlined /> 签到</span>
      <span><strong>{checkin.checkedToday}/{checkin.groups}</strong> <Text type="secondary">个群今天签过</Text></span>
      {checkin.pendingGroups.length > 0 ? <Text type="secondary" className="me-hint">还没签：{checkin.pendingGroups.map((group) => group.groupName || group.groupId).join("、")}</Text> : checkin.groups > 0 ? <Text type="success" className="me-hint">全签完啦</Text> : null}
      {checkin.bestStreak > 0 && <Text type="secondary" className="me-hint">最长连签 {checkin.bestStreak} 天</Text>}
    </Link>
  </div>;
}

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
  { to: "/me/account", icon: <LockOutlined />, title: "账号与数据", sub: () => "改密码、退出设备、导出我的数据" },
];

export function MeHomePage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const profile = useApiQuery({ queryKey: ["me"], fetcher: () => meApi.profile() });
  const groups = useApiQuery({ queryKey: ["me", "groups"], fetcher: () => meApi.groups() });
  const dash = useResource<Dashboard>(["me", "dashboard"], () => spaceApi.dashboard());
  const me = profile.data;
  const data = dash.data;
  const hour = data ? Number(data.now.slice(11, 13)) : new Date().getHours();
  const title = me?.nickname ? `${greeting(hour)}，${me.nickname}` : greeting(hour);

  const logMood = async (score: number): Promise<void> => {
    try {
      await spaceApi.logMood(score);
      message.success(`记下了：${MOOD_OPTIONS.find((item) => item.score === score)?.label ?? ""}`);
      dash.query.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return <div className="me-page">
    <PageHeader
      title={title}
      subtitle={me ? (me.isSuperuser ? `QQ ${me.userId} · 超级管理员，Bot 在的 ${me.managedGroupCount} 个群都能管` : `QQ ${me.userId} · 你在 ${me.groupCount} 个群里，其中 ${me.managedGroupCount} 个是群主/管理员`) : "正在读取你的信息"}
      onRefresh={() => { profile.reload(); groups.reload(); dash.query.reload(); }}
      refreshing={profile.refreshing || groups.refreshing || dash.query.refreshing}
    />
    {profile.error && !me && <QueryErrorAlert error={profile.error} onRetry={profile.reload} />}
    {dash.query.error && !data && <QueryErrorAlert error={dash.query.error} onRetry={dash.query.reload} />}
    {data?.chat.paused && <Alert type="warning" showIcon message="你暂停了 Bot 的私聊自动回复" action={<Link to="/me/chat">去恢复</Link>} />}
    {data && data.tasks.waitingYou > 0 && <Alert type="info" showIcon message={`有 ${data.tasks.waitingYou} 个后台任务在等你回答`} description="在私聊里直接回复 Bot 就行。" action={<Link to="/me/tasks">看看</Link>} />}

    {!data ? <Skeleton active paragraph={{ rows: 4 }} /> : <TodayPanel data={data} onMood={(score) => void logMood(score)} />}

    {data && (
      <div className="me-affinity-strip">
        <HeartOutlined className="me-affinity-icon" />
        <span>和 Bot 的关系：<strong>{data.affinity.level}</strong></span>
        <Progress className="me-affinity-progress" percent={Math.round(data.affinity.progress * 100)} size="small" showInfo={false} strokeColor="var(--brand)" />
        <Text type="secondary" className="me-hint">{data.affinity.nextLevel ? `下一级「${data.affinity.nextLevel}」` : "最高级"}</Text>
      </div>
    )}

    <div className="me-cards me-entry-grid">
      {ENTRIES.map((entry) => (
        <Link key={entry.to} to={entry.to} className="me-card me-card-link">
          <span className="me-card-icon">{entry.icon}</span>
          <span className="me-card-body">
            <strong>{entry.title}</strong>
            <Text type="secondary" className="me-card-sub" ellipsis>{entry.to === "/me/persona" && me?.persona ? `${me.persona.name || "默认人设"} · ${me.persona.sourceLabel}` : entry.to === "/me/account" && me?.account ? `上次登录 ${formatQqTime(me.account.lastLoginAt)}` : entry.sub(data)}</Text>
          </span>
          <RightOutlined className="me-card-arrow" />
        </Link>
      ))}
    </div>

    {(groups.data?.length ?? 0) > 0 && (
      <MeSection icon={<TeamOutlined />} title="我管理的群" extra={<Link to="/me/groups">全部群 <RightOutlined /></Link>} hint={me?.isSuperuser ? "你是超级管理员：Bot 在的群都列在这里（Bot 自己是管理员的群才能做禁言等操作）" : "打开时会再向 QQ 确认一次身份"}>
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
                    <span>{group.enabledFeatures}/{group.featureCount} 项功能开启</span>
                    {!group.agentEnabled && <Tag className="me-inline-tag">Agent 已关</Tag>}
                  </span>
                </span>
                <RightOutlined className="me-card-arrow" />
              </Link>
            </li>
          ))}
        </ul>
      </MeSection>
    )}
    {groups.data && groups.data.length === 0 && profile.data && !profile.data.isSuperuser && (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span>你目前不是任何 Bot 所在群的群主或管理员，<Link to="/me/groups">看看我在的群</Link>。</span>} />
    )}

    <Alert
      className="me-safety-note"
      type="info"
      showIcon
      icon={<SafetyCertificateOutlined />}
      message="这里只有你自己的东西"
      description="小本本、记忆、聊天记录只有你和 Bot 看得到；群功能开关和群里的 /面板 是同一份设置；忘记密码就私聊 Bot 发 /网页密码。"
    />
  </div>;
}
