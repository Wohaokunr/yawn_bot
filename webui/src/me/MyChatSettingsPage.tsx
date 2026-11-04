import { BellOutlined, ControlOutlined, MessageOutlined, PauseCircleOutlined, PlayCircleOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, Button, Form, Input, InputNumber, Popconfirm, Segmented, Select, Skeleton, Switch, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type ChatPrefs, type ChatSettings, type OfferState, spaceApi } from "./me-space-api";
import { EMOJI_OPTIONS, isBirthday, isClock, quietLabel, REPLY_LENGTH_OPTIONS } from "./me-space-meta";
import { MeSection, StatTile, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text } = Typography;

function clockRule(label: string) {
  return { validator: async (_rule: unknown, value: string) => { if (value && !isClock(value)) throw new Error(`${label}写成 HH:MM，比如 07:30`); } };
}

function PrefsForm({ data, onSaved }: { data: ChatSettings; onSaved: (next: ChatSettings) => void }): React.JSX.Element {
  const [form] = Form.useForm<ChatPrefs>();
  const { busy, run } = useAction();
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    form.setFieldsValue(data.prefs);
    setDirty(false);
  }, [data, form]);

  const save = async (values: ChatPrefs): Promise<void> => {
    // 只交改过的字段；清空的输入框交 ""，后端当作“关掉”
    const changes: Partial<ChatPrefs> = {};
    for (const [key, value] of Object.entries(values) as Array<[keyof ChatPrefs, ChatPrefs[keyof ChatPrefs]]>) {
      const normalized = typeof value === "string" ? value.trim().replace("：", ":") : value;
      if (normalized !== data.prefs[key]) (changes as Record<string, unknown>)[key] = normalized ?? "";
    }
    if (Object.keys(changes).length === 0) return;
    const next = await run("save", () => spaceApi.patchChatPrefs(changes), "保存好了，下一句私聊就生效");
    if (next) onSaved(next);
  };

  return <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void save(values)} onValuesChange={() => setDirty(true)} className="me-prefs-form">
    <div className="me-form-grid">
      <Form.Item name="callMe" label="怎么称呼你" extra="留空就用你的群名片/昵称">
        <Input maxLength={16} placeholder="比如：小猫、老板" allowClear />
      </Form.Item>
      <Form.Item name="replyLength" label="回复长短">
        <Segmented options={REPLY_LENGTH_OPTIONS.map((item) => ({ value: item.value, label: item.label }))} />
      </Form.Item>
      <Form.Item name="emoji" label="表情符号">
        <Segmented options={EMOJI_OPTIONS.map((item) => ({ value: item.value, label: item.label }))} />
      </Form.Item>
      <Form.Item name="birthday" label="生日（月-日）" extra="生日当天 Bot 会私聊祝你" rules={[{ validator: async (_rule, value: string) => { if (value && !isBirthday(value)) throw new Error("写成 MM-DD，比如 03-14"); } }]}>
        <Input maxLength={5} placeholder="MM-DD" allowClear />
      </Form.Item>
    </div>
    <div className="me-subhead"><BellOutlined /> 主动消息</div>
    <div className="me-form-grid">
      <Form.Item name="proactive" label="允许 Bot 主动找你" valuePropName="checked" extra={data.agent.proactiveAllowed ? "关掉后早安、签到提醒、生日祝福都不发；你自己设的提醒照常送达" : "管理员暂时关掉了全站的主动消息"}>
        <Switch disabled={!data.agent.proactiveAllowed} />
      </Form.Item>
      <Form.Item name="morningGreeting" label="早安问候时间" extra="留空不发" rules={[clockRule("早安时间")]}>
        <Input maxLength={5} placeholder="07:30" allowClear />
      </Form.Item>
      <Form.Item name="checkinNudge" label="提醒我签到" extra="到点还没签到就私聊提醒，留空不提醒" rules={[clockRule("签到提醒")]}>
        <Input maxLength={5} placeholder="21:00" allowClear />
      </Form.Item>
    </div>
    <div className="me-subhead"><StopOutlined /> 免打扰</div>
    <div className="me-form-grid">
      <Form.Item name="quietStart" label="从" rules={[clockRule("开始时间"), ({ getFieldValue }) => ({ validator: async (_rule, value: string) => { if (Boolean(value) !== Boolean(getFieldValue("quietEnd"))) throw new Error("开始和结束要一起填，或一起清空"); } })]} dependencies={["quietEnd"]}>
        <Input maxLength={5} placeholder="23:00" allowClear />
      </Form.Item>
      <Form.Item name="quietEnd" label="到" rules={[clockRule("结束时间")]}>
        <Input maxLength={5} placeholder="08:00" allowClear />
      </Form.Item>
      <Form.Item label="现在">
        <Text type="secondary">{quietLabel(data.prefs.quietStart, data.prefs.quietEnd)}{data.inQuietHours && <Tag color="purple" className="me-inline-tag">正在免打扰</Tag>}</Text>
      </Form.Item>
    </div>
    <div className="me-form-actions">
      <Button onClick={() => { form.setFieldsValue(data.prefs); setDirty(false); }} disabled={!dirty}>还原</Button>
      <Button type="primary" htmlType="submit" loading={busy === "save"} disabled={!dirty}>保存</Button>
    </div>
  </Form>;
}

function OffersCard(): React.JSX.Element {
  const { query, data, setData } = useResource<OfferState>(["me", "offers"], () => spaceApi.offers());
  const { busy, run } = useAction();
  const [hours, setHours] = useState<number | null>(null);
  useEffect(() => setHours(data?.everyHours ?? null), [data]);

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 3 }} />;
  const patch = async (key: string, change: Parameters<typeof spaceApi.patchOffers>[0], success?: string): Promise<void> => {
    const next = await run(key, () => spaceApi.patchOffers(change), success);
    if (next) setData(next);
  };
  const blocked = !data.siteEnabled ? "管理员关掉了主动提议" : !data.siteOpen ? "主动提议暂时没对你开放" : null;
  const accepted = data.stats.accepted ?? 0;
  const sent = data.stats.sent ?? 0;

  return <MeSection
    icon={<MessageOutlined />}
    title="主动提议"
    hint={`隔一段时间，Bot 会看看有没有能帮你的事（快到期的待办、临近的倒数日……）问你一句，只问不做。每天最多 ${data.dailyMax} 次${data.siteQuiet ? `，${data.siteQuiet} 不问` : ""}。`}
    extra={blocked ? <Tag>{blocked}</Tag> : <Tag color={data.on ? "success" : "default"}>{data.on ? "开着" : "关着"}{data.ownChoice ? "（你选的）" : "（默认）"}</Tag>}
  >
    {blocked ? <Alert type="info" showIcon message={blocked} /> : <div className="me-stack">
      <div className="me-offer-row">
        <Segmented
          value={data.ownChoice ? (data.on ? "on" : "off") : "default"}
          onChange={(value) => void patch("mode", value === "default" ? { followDefault: true } : { enabled: value === "on" }, "改好了")}
          options={[
            { value: "on", label: "开" },
            { value: "off", label: "关" },
            { value: "default", label: `跟随默认（${data.defaultOn ? "开" : "关"}）` },
          ]}
        />
        <span className="me-inline-switch">
          每隔
          <InputNumber size="small" min={data.minEveryHours} max={48} step={0.5} value={hours} onChange={(value) => setHours(value)} aria-label="间隔小时" />
          小时问一次
          <Button size="small" disabled={hours === null || hours === data.everyHours} loading={busy === "every"} onClick={() => hours !== null && void patch("every", { everyHours: hours }, "间隔改好了")}>应用</Button>
          {hours !== data.defaultEveryHours && <Button size="small" type="link" onClick={() => void patch("every", { resetInterval: true })}>恢复默认 {data.defaultEveryHours} 小时</Button>}
        </span>
      </div>
      <div className="me-offer-row">
        {data.pausedUntil ? (
          <>
            <Tag color="purple">暂停到 {data.pausedUntil}</Tag>
            <Button size="small" onClick={() => void patch("snooze", { snoozeHours: 0 }, "恢复了")}>现在就恢复</Button>
          </>
        ) : (
          <>
            <Text type="secondary">先别问我：</Text>
            {[3, 24, 72, 168].map((value) => <Button key={value} size="small" loading={busy === `snooze-${value}`} onClick={() => void patch(`snooze-${value}`, { snoozeHours: value }, "好的，这段时间不问你")}>{value < 24 ? `${value} 小时` : `${value / 24} 天`}</Button>)}
          </>
        )}
      </div>
      <div>
        <Text strong>哪些事可以问我</Text>
        <ul className="me-kind-list">
          {data.kinds.map((kind) => (
            <li key={kind.kind} className={!kind.siteOn ? "is-off" : undefined}>
              <span className="me-plain-main"><strong>{kind.label}</strong><Text type="secondary" className="me-hint"> {kind.description}</Text></span>
              {!kind.siteOn ? <Tag>管理员关了</Tag> : kind.mutedUntil ? (
                <>
                  <Tag color="default">{kind.mutedUntil === "forever" ? "不问" : `${kind.mutedUntil} 前不问`}</Tag>
                  <Button size="small" type="link" onClick={() => void patch(`kind-${kind.kind}`, { unmuteKind: kind.kind })}>恢复</Button>
                </>
              ) : (
                <Select<number>
                  size="small"
                  placeholder="别问这类"
                  value={undefined}
                  style={{ width: 120 }}
                  onChange={(days) => void patch(`kind-${kind.kind}`, { muteKind: kind.kind, muteDays: days }, "这一类先不问了")}
                  options={[{ value: 3, label: "3 天内别问" }, { value: 30, label: "30 天内别问" }, { value: 0, label: "一直别问" }]}
                  aria-label={`别问${kind.label}`}
                />
              )}
            </li>
          ))}
        </ul>
      </div>
      <div className="me-stat-row">
        <StatTile label="问过你" value={sent} />
        <StatTile label="你答应了" value={accepted} tone="success" />
        <StatTile label="你说不用" value={data.stats.declined ?? 0} />
        <StatTile label="你没回" value={data.stats.ignored ?? 0} />
      </div>
      {data.history.length > 0 && (
        <ul className="me-plain-list">
          {data.history.slice(0, 8).map((row) => (
            <li key={row.id}>
              <Text type="secondary" className="me-hint me-mono">{row.at}</Text>
              <span className="me-plain-main">{row.statusLabel}{row.about.length > 0 && <Text type="secondary">：{row.about.join("、")}</Text>}</span>
              {row.outcomeLabel && <Tag>{row.outcomeLabel}</Tag>}
            </li>
          ))}
        </ul>
      )}
    </div>}
  </MeSection>;
}

export function MyChatSettingsPage(): React.JSX.Element {
  const { query, data, setData } = useResource<ChatSettings>(["me", "chat"], () => spaceApi.chat());
  const { busy, run } = useAction();

  return <div className="me-page">
    <PageHeader title="私聊设置" subtitle="Bot 在私聊里怎么跟你说话、什么时候可以找你。和私聊里直接说“以后叫我…/晚上别打扰”是同一份设置。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : <>
      {!data.agent.enabled && <Alert type="warning" showIcon message="管理员关闭了私聊 Agent" description="下面的设置会保存，等重新开启后生效。" />}
      <MeSection
        icon={<ControlOutlined />}
        title="自动回复"
        extra={data.agent.paused ? <Tag color="warning">已暂停</Tag> : <Tag color="success">正常</Tag>}
        hint={data.agent.paused ? "你暂停了 Bot 的私聊自动回复：它不会回你普通消息，提醒照常送达。" : "Bot 会直接回复你的私聊消息。想安静一阵可以暂停（和私聊发 /退出 一样）。"}
      >
        <div className="me-stat-row">
          <StatTile label="今天聊了" value={`${data.agent.turnsToday} 轮`} sub={`每天上限 ${data.agent.dailyTurns} 轮`} tone="brand" />
          <StatTile label="谁能直接聊" value={data.agent.friendsOnly ? "好友" : "所有人"} sub={data.agent.autoReply ? "不用先发 /对话" : "要先发 /对话"} />
          <div className="me-stat me-stat-action">
            {data.agent.paused ? (
              <Button type="primary" icon={<PlayCircleOutlined />} loading={busy === "pause"} onClick={async () => { const next = await run("pause", () => spaceApi.pauseChat(false), "恢复了"); if (next) setData(next); }}>恢复自动回复</Button>
            ) : (
              <Popconfirm title="暂停私聊自动回复？" description="之后在这里或私聊发 /对话 就能恢复。" okText="暂停" onConfirm={async () => { const next = await run("pause", () => spaceApi.pauseChat(true), "已暂停"); if (next) setData(next); }}>
                <Button icon={<PauseCircleOutlined />} loading={busy === "pause"}>暂停自动回复</Button>
              </Popconfirm>
            )}
          </div>
        </div>
      </MeSection>
      <MeSection icon={<MessageOutlined />} title="说话方式与打扰" hint={<>想改 Bot 的自称和性格，去 <Link to="/me/persona">私聊人设</Link>。</>}>
        <PrefsForm data={data} onSaved={setData} />
      </MeSection>
      <OffersCard />
    </>}
  </div>;
}
