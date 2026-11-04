import { BellOutlined, BulbOutlined, ClockCircleOutlined, HistoryOutlined, ReloadOutlined, SaveOutlined, SendOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Checkbox, Collapse, Empty, Input, InputNumber, Popconfirm, Segmented, Select, Skeleton, Space, Switch, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { privateApi, type OfferCandidate, type OfferKind, type OfferLogRow, type OfferSettings, type OfferUserDetail, type OfferUserRow } from "./private-api";
import { OFFER_DROP_LABELS, formatHours, offerAcceptRate, offerReasonColor, offerStatusColor, parseUserIds } from "./private-meta";

const { Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type NumberKey = { [K in keyof OfferSettings]: OfferSettings[K] extends number ? K : never }[keyof OfferSettings];
type BoolKey = { [K in keyof OfferSettings]: OfferSettings[K] extends boolean ? K : never }[keyof OfferSettings];

const NUMBER_FIELDS: Array<{ key: NumberKey; label: string; min: number; max: number; step?: number; hint: string }> = [
  { key: "interval_hours", label: "每隔几小时问一次", min: 0.5, max: 48, step: 0.5, hint: "从上一次提议和上一次聊天里更晚的那个算起" },
  { key: "daily_max", label: "每人每天最多", min: 1, max: 12, hint: "次" },
  { key: "max_unanswered", label: "连着几次没回就停", min: 1, max: 10, hint: "停到他先开口" },
  { key: "backoff_factor", label: "每没回一次间隔乘以", min: 1, max: 4, step: 0.5, hint: "1 表示不拉长" },
  { key: "max_backoff_hours", label: "间隔最长拉到", min: 1, max: 168, hint: "小时" },
  { key: "min_idle_minutes", label: "刚聊过多久内不插话", min: 5, max: 720, hint: "分钟" },
  { key: "active_within_days", label: "只问最近几天聊过的人", min: 1, max: 90, hint: "天" },
  { key: "item_cooldown_hours", label: "同一件事多久内不再问", min: 1, max: 336, hint: "小时" },
  { key: "decline_mute_days", label: "他说不用的那类静音", min: 0, max: 60, hint: "天（0 不静音）" },
  { key: "answer_window_hours", label: "多久没回算“没回”", min: 1, max: 72, hint: "小时" },
  { key: "max_candidates", label: "给模型看几件候选", min: 1, max: 8, hint: "件" },
  { key: "max_items_per_offer", label: "一次最多问几件", min: 1, max: 3, hint: "件" },
  { key: "min_score", label: "候选最低分", min: 0, max: 100, hint: "0~100，分数见用户详情" },
  { key: "user_min_interval_hours", label: "他自己最短能设", min: 0.5, max: 48, step: 0.5, hint: "小时一次" },
  { key: "jitter_minutes", label: "随机错开", min: 0, max: 120, hint: "分钟（按人固定）" },
  { key: "per_tick", label: "每分钟最多问几个人", min: 1, max: 10, hint: "人" },
];

const BOOL_FIELDS: Array<{ key: BoolKey; label: string; hint: string }> = [
  { key: "enabled", label: "开启主动提议", hint: "关掉后谁都不问；每个人自己的设置保留" },
  { key: "default_on", label: "新用户默认开启", hint: "关掉后要他自己说“可以主动问我”才问" },
  { key: "quiet_hours_enabled", label: "夜间不打扰", hint: "他自己的免打扰时段另算，两个都躲开" },
  { key: "allow_model_skip", label: "允许看完觉得不值得就不问", hint: "关掉后找到事就一定会问" },
  { key: "only_friends", label: "只问好友", hint: "不是好友的私聊不主动找" },
];

function SettingsPanel({ settings, kinds, onSaved }: { settings: OfferSettings; kinds: OfferKind[]; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [draft, setDraft] = useState<OfferSettings>(settings);
  const [allowText, setAllowText] = useState(settings.allowed_users.join(", "));
  const [blockText, setBlockText] = useState(settings.blocked_users.join(", "));
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setDraft(settings);
    setAllowText(settings.allowed_users.join(", "));
    setBlockText(settings.blocked_users.join(", "));
  }, [settings]);
  const set = <K extends keyof OfferSettings>(key: K, value: OfferSettings[K]): void => setDraft((current) => ({ ...current, [key]: value }));
  const dirty = JSON.stringify({ ...draft, allowed_users: parseUserIds(allowText), blocked_users: parseUserIds(blockText) }) !== JSON.stringify(settings);
  const save = async (): Promise<void> => {
    setSaving(true);
    try {
      await privateApi.saveOfferSettings({ ...draft, allowed_users: parseUserIds(allowText), blocked_users: parseUserIds(blockText) });
      message.success("已保存，下一分钟起生效");
      onSaved();
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setSaving(false);
    }
  };
  return <div className="pa-offer-settings">
    <div className="pa-offer-switches">
      {BOOL_FIELDS.map((field) => <label key={field.key} className="pa-offer-switch">
        <Switch size="small" checked={draft[field.key]} onChange={(checked) => set(field.key, checked)} aria-label={field.label} />
        <span><Text>{field.label}</Text><Text type="secondary" className="dbg-small pa-block">{field.hint}</Text></span>
      </label>)}
    </div>
    <Space wrap size={[8, 8]} className="pa-offer-quiet">
      <Text>夜间时段</Text>
      <Input aria-label="夜间开始" value={draft.quiet_start} onChange={(event) => set("quiet_start", event.target.value)} style={{ width: 80 }} disabled={!draft.quiet_hours_enabled} />
      <Text type="secondary">到</Text>
      <Input aria-label="夜间结束" value={draft.quiet_end} onChange={(event) => set("quiet_end", event.target.value)} style={{ width: 80 }} disabled={!draft.quiet_hours_enabled} />
    </Space>
    <div className="pa-offer-numbers">
      {NUMBER_FIELDS.map((field) => <label key={field.key} className="pa-offer-number">
        <Text>{field.label}</Text>
        <InputNumber aria-label={field.label} size="small" min={field.min} max={field.max} step={field.step ?? 1} value={draft[field.key]} onChange={(value) => set(field.key, Number(value ?? field.min))} />
        <Text type="secondary" className="dbg-small">{field.hint}</Text>
      </label>)}
    </div>
    <Text strong className="pa-block">哪些事可以拿来问</Text>
    <div className="pa-offer-kinds">
      {kinds.map((kind) => <Checkbox key={kind.kind} checked={draft.kinds[kind.kind] !== false} onChange={(event) => set("kinds", { ...draft.kinds, [kind.kind]: event.target.checked })}>
        <Text>{kind.label}</Text> <Text type="secondary" className="dbg-small">{kind.description}</Text>
      </Checkbox>)}
    </div>
    <Space wrap size={[8, 8]} align="start" className="pa-offer-lists">
      <Segmented value={draft.user_mode} onChange={(value) => set("user_mode", value as OfferSettings["user_mode"])} options={[{ value: "all", label: "所有人" }, { value: "allowlist", label: "只问名单里的人" }]} />
      {draft.user_mode === "allowlist" && <Input.TextArea aria-label="开放名单" rows={1} placeholder="QQ 号，逗号或空格分隔" value={allowText} onChange={(event) => setAllowText(event.target.value)} style={{ width: 260 }} />}
      <Input.TextArea aria-label="屏蔽名单" rows={1} placeholder="不问这些人（QQ 号）" value={blockText} onChange={(event) => setBlockText(event.target.value)} style={{ width: 260 }} />
    </Space>
    <div className="pst-section-head">
      <span className="dbg-grow" />
      <Button size="small" onClick={() => { setDraft(settings); setAllowText(settings.allowed_users.join(", ")); setBlockText(settings.blocked_users.join(", ")); }} disabled={!dirty || saving}>还原</Button>
      <Button size="small" type="primary" icon={<SaveOutlined />} onClick={() => void save()} loading={saving} disabled={!dirty}>保存策略</Button>
    </div>
  </div>;
}

function LogList({ rows }: { rows: OfferLogRow[] }): React.JSX.Element {
  if (rows.length === 0) return <Empty description="还没有主动问过他" />;
  return <ul className="pa-user-list pa-bridge-list" aria-label="主动提议记录">
    {rows.map((row) => <li key={row.id}>
      <div className="pa-bridge-main">
        <Text strong>{row.items.length ? row.items.map((item) => item.what).join("、") : row.status_label}</Text>
        {row.text && <Text className="pa-offer-text">“{row.text}”</Text>}
        <Text type="secondary" className="dbg-small">
          {row.at} · {row.status_label}{row.by === "webui" ? " · 管理员手动" : ""}{row.replied_at ? ` · 他 ${row.replied_at} 回了话` : ""}
        </Text>
      </div>
      <Tag color={offerStatusColor(row.status, row.outcome)}>{row.outcome_label || row.status_label}</Tag>
    </li>)}
  </ul>;
}

function CandidateList({ rows, kinds, dropped }: { rows: OfferCandidate[]; kinds: Record<string, string>; dropped?: boolean }): React.JSX.Element {
  if (rows.length === 0) return <Empty description={dropped ? "没有被筛掉的" : "现在没有值得问的事"} />;
  return <ul className="pa-user-list pa-bridge-list" aria-label={dropped ? "被筛掉的事" : "能拿来问的事"}>
    {rows.map((row) => <li key={row.key}>
      <div className="pa-bridge-main">
        <Text strong>{row.what}</Text>
        <Text type="secondary" className="dbg-small">{kinds[row.kind] ?? row.kind} · 可以：{row.could_do}{dropped && row.dropped ? ` · ${OFFER_DROP_LABELS[row.dropped] ?? row.dropped}` : ""}</Text>
      </div>
      <Tag>{row.score} 分</Tag>
    </li>)}
  </ul>;
}

function UserOffers({ userId, kinds }: { userId: string; kinds: OfferKind[] }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<OfferUserDetail>({
    queryKey: ["private-offers-user", userId],
    fetcher: (signal) => privateApi.offerUser(userId, signal),
  });
  const [busy, setBusy] = useState(false);
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const data = query.data;
  const kindLabels = Object.fromEntries(kinds.map((kind) => [kind.kind, kind.label]));
  const act = async (work: () => Promise<unknown>, done: string): Promise<void> => {
    setBusy(true);
    try {
      await work();
      message.success(done);
      query.reload();
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  const run = async (): Promise<void> => {
    setBusy(true);
    try {
      const result = await privateApi.offerRun(userId);
      if (result.status === "sent") message.success(`已问他：${result.text ?? ""}`);
      else if (result.status === "not_now") message.info(`现在不问：${result.label ?? result.reason ?? ""}`);
      else message.info({ skipped: "模型看完觉得这次不值得问", user_spoke: "正要问时他先说话了", failed: "没发出去" }[result.status] ?? result.status);
      query.reload();
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  const own = data.own;
  const prefs = data.prefs;
  return <section className="dbg-panel" aria-label={`用户 ${userId} 的主动提议`}>
    <div className="pst-section-head">
      <Text strong><UserOutlined /> 用户 {userId}</Text>
      <Tag color={offerReasonColor(data.gate.reason)}>{data.gate.label}</Tag>
      {data.gate.next_at && <Tag icon={<ClockCircleOutlined />}>下一次最早 {data.gate.next_at}</Tag>}
      <span className="dbg-grow" />
      <Popconfirm title="现在就问他一次？" description="跳过间隔、没回次数和每日上限，但仍尊重他的开关、/退出、夜间和免打扰。" onConfirm={() => void run()}>
        <Button size="small" type="primary" icon={<SendOutlined />} disabled={busy || data.candidates.length === 0}>现在试一次</Button>
      </Popconfirm>
      <Popconfirm title="清空他的提议记录和冷却？" description="问过的事又可以拿来问；他自己的开关和间隔保留。" onConfirm={() => act(() => privateApi.offerReset(userId, false), "已清空记录")}>
        <Button size="small" disabled={busy}>清空记录</Button>
      </Popconfirm>
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    <Space wrap size={[4, 4]}>
      <Tag>他上次说话 {data.facts.last_user_at ?? "—"}</Tag>
      <Tag>我上次说话 {data.facts.last_bot_at ?? "—"}</Tag>
      {data.facts.friend === false && <Tag color="orange">不是好友</Tag>}
      {data.facts.paused && <Tag color="orange">/退出 暂停中</Tag>}
      <Tag>今天问了 {data.gate.today} 次</Tag>
      {data.gate.unanswered > 0 && <Tag color="orange">连着 {data.gate.unanswered} 次没回</Tag>}
      <Tag>当前间隔 {formatHours(data.gate.interval_hours)}</Tag>
      <Tag color="green">答应 {data.stats.accepted}/{data.stats.sent}（{offerAcceptRate(data.stats)}）</Tag>
    </Space>
    <div className="pa-offer-own">
      <Text strong><BellOutlined /> 他的设置</Text>
      <Segmented
        size="small"
        value={own.enabled === null ? "default" : own.enabled ? "on" : "off"}
        onChange={(value) => void act(() => privateApi.offerPrefs(userId, value === "default" ? { follow_default: true } : { enabled: value === "on" }), "已更新")}
        options={[{ value: "default", label: own.enabled === null ? `跟随默认（${prefs.on ? "开" : "关"}）` : "跟随默认" }, { value: "on", label: "开" }, { value: "off", label: "关" }]}
        disabled={busy}
      />
      <Space size={4}>
        <Text type="secondary">每隔</Text>
        <InputNumber aria-label="他的间隔" size="small" min={0.5} max={48} step={0.5} placeholder={String(prefs.every_hours)} value={own.interval_hours ?? undefined}
          onChange={(value) => { if (value !== null) void act(() => privateApi.offerPrefs(userId, { interval_hours: Number(value) }), "已改间隔"); }} disabled={busy} />
        <Text type="secondary">小时</Text>
        {own.interval_hours !== null && <Button size="small" type="link" onClick={() => void act(() => privateApi.offerPrefs(userId, { reset_interval: true }), "已恢复默认间隔")}>恢复默认</Button>}
      </Space>
      {prefs.paused_until
        ? <Tag color="orange" closable onClose={(event) => { event.preventDefault(); void act(() => privateApi.offerPrefs(userId, { snooze_hours: 0 }), "已取消暂停"); }}>暂停到 {prefs.paused_until}</Tag>
        : <Button size="small" onClick={() => void act(() => privateApi.offerPrefs(userId, { snooze_hours: 24 }), "已暂停一天")} disabled={busy}>暂停一天</Button>}
      {(prefs.muted ?? []).map((row) => <Tag key={row.kind} color="orange" closable onClose={(event) => { event.preventDefault(); void act(() => privateApi.offerPrefs(userId, { unmute: [row.kind] }), "已恢复"); }}>
        不问「{row.label}」{row.until === "一直" ? "" : `到 ${row.until}`}
      </Tag>)}
    </div>
    <Text strong><BulbOutlined /> 现在能拿来问的事</Text>
    <CandidateList rows={data.candidates} kinds={kindLabels} />
    <Collapse size="small" items={[
      { key: "dropped", label: `被筛掉的（${data.dropped.length}）`, children: <CandidateList rows={data.dropped} kinds={kindLabels} dropped /> },
      { key: "payload", label: "会发给模型的资料", children: data.candidates.length ? <pre className="pa-offer-json">{JSON.stringify(data.payload, null, 2)}</pre> : <Empty description="没有候选就不会问" /> },
      { key: "cooling", label: `冷却中的事（${data.cooling.length}）`, children: data.cooling.length ? <ul className="pa-offer-cooling">{data.cooling.map((row) => <li key={row.key}><Text code>{row.key}</Text> <Text type="secondary" className="dbg-small">{row.at}</Text></li>)}</ul> : <Empty description="没有" /> },
    ]} />
    <Text strong><HistoryOutlined /> 最近的提议</Text>
    <LogList rows={data.log} />
  </section>;
}

function UserTable({ rows, onPickUser }: { rows: OfferUserRow[]; onPickUser: (userId: string) => void }): React.JSX.Element {
  if (rows.length === 0) return <Empty description="还没有有个人数据的私聊用户" />;
  return <ul className="pa-user-list pa-bridge-list" aria-label="用户主动提议状态">
    {rows.map((row) => <li key={row.user_id}>
      <div className="pa-bridge-main">
        <Text strong>{row.user_id}</Text>
        <Text type="secondary" className="dbg-small">
          {row.prefs.on ? `每 ${formatHours(row.prefs.every_hours)}` : "已关闭"}
          {row.last ? ` · 上次 ${row.last.at} ${row.last.outcome_label || row.last.status_label}` : " · 还没问过"}
          {` · 答应 ${row.stats.accepted}/${row.stats.sent}`}
        </Text>
      </div>
      {row.last_check && <Tag color={offerReasonColor(row.last_check.reason)}>{row.last_check.label}</Tag>}
      {row.pending && <Tag color="blue">等他回应</Tag>}
      <Button size="small" type="link" onClick={() => onPickUser(String(row.user_id))}>详情</Button>
    </li>)}
  </ul>;
}

/** 主动提议：每隔几小时替他找找能帮他做的事并私聊问他。全站策略、今天的用量、每个人现在问不问和为什么。 */
export function OffersTab({ userId, onPickUser }: { userId: string; onPickUser: (userId: string | null) => void }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["private-offers"],
    fetcher: (signal) => privateApi.offers(signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const data = query.data;
  const results = data.usage.results;
  const settings = data.settings;
  return <div className="pa-offers">
    {!data.env_on && <Alert type="warning" showIcon message="主动提议总闸关着" description="PRIVATE_AGENT_OFFERS=false：下面的设置保留，但谁都不会被主动问。" />}
    {data.env_on && !data.agent_on && <Alert type="warning" showIcon message="私聊 Agent 或主动消息关着" description="PRIVATE_AGENT_ENABLED / PRIVATE_AGENT_PROACTIVE 关掉时也不会主动问。" />}
    <section className="dbg-panel" aria-label="主动提议概况">
      <div className="pst-section-head">
        <Text strong><BulbOutlined /> 主动提议</Text>
        <Tag color={settings.enabled && data.env_on ? "green" : "default"}>{settings.enabled && data.env_on ? `每 ${formatHours(settings.interval_hours)}` : "关闭"}</Tag>
        <Tag>今天问了 {results.sent ?? 0}</Tag>
        <Tag>没问 {results.skipped ?? 0}</Tag>
        <Tag color="green">答应 {results.accepted ?? 0}</Tag>
        <Tag color="orange">不用 {results.declined ?? 0}</Tag>
        <Tag>晚点 {results.later ?? 0}</Tag>
        {(results.failed ?? 0) > 0 && <Tag color="red">没发出去 {results.failed}</Tag>}
        <span className="dbg-grow" />
        {userId && <Button size="small" onClick={() => onPickUser(null)}>看全部</Button>}
        <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
      </div>
      <Paragraph type="secondary" className="dbg-small">
        每隔 {formatHours(settings.interval_hours)}（从上次提议和上次聊天里更晚的那个算起），我会翻翻他的待办、提醒、最近聊过的事，挑一两件现在就能帮他做的，私聊问他要不要我去做；他答应了下一句再动手。
        {settings.quiet_hours_enabled ? `${settings.quiet_start}–${settings.quiet_end} 不问，` : ""}他的免打扰时段、刚聊过 {settings.min_idle_minutes} 分钟内、/退出 之后都不问；连着 {settings.max_unanswered} 次没回就停到他先开口，每天最多 {settings.daily_max} 次。找不到值得问的事就什么都不说。
      </Paragraph>
      <Collapse size="small" items={[{ key: "settings", label: "策略", children: <SettingsPanel settings={settings} kinds={data.kinds} onSaved={query.reload} /> }]} />
    </section>
    {userId
      ? <UserOffers userId={userId} kinds={data.kinds} />
      : <section className="dbg-panel"><Text strong><UserOutlined /> 每个人（{data.total_users}）</Text><UserTable rows={data.users} onPickUser={onPickUser} /></section>}
  </div>;
}
