import { BellOutlined, ControlOutlined, MessageOutlined, PauseCircleOutlined, PlayCircleOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, Button, Form, Input, InputNumber, Popconfirm, Segmented, Select, Skeleton, Switch, Tag, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type ChatPrefs, type ChatSettings, type OfferState, spaceApi } from "./me-space-api";
import { EMOJI_OPTIONS, isBirthday, isClock, quietLabel, REPLY_LENGTH_OPTIONS } from "./me-space-meta";
import { MeSection, StatTile, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

function clockRule(label: string) {
  return { validator: async (_rule: unknown, value: string) => { if (value && !isClock(value)) throw new Error(`${label}写成 HH:MM，比如 07:30`); } };
}

type PrefKey = keyof ChatPrefs;
/** 选项类（分段、开关）一改就存；文字类在离开输入框或回车时存。整页没有“保存”按钮。 */
const CHOICE_KEYS: PrefKey[] = ["replyLength", "emoji", "proactive"];
const QUIET_KEYS: PrefKey[] = ["quietStart", "quietEnd"];

const PRESETS: Array<{ key: string; title: string; hint: string; patch: Partial<ChatPrefs> }> = [
  { key: "brief", title: "干脆利落", hint: "回得短，不用表情，不主动找你", patch: { replyLength: "short", emoji: "none", proactive: false } },
  { key: "normal", title: "刚刚好", hint: "正常长度，偶尔用表情", patch: { replyLength: "normal", emoji: "some", proactive: true } },
  { key: "chatty", title: "话痨模式", hint: "可以多说，表情多一点，会主动找你", patch: { replyLength: "long", emoji: "lots", proactive: true } },
  { key: "night", title: "晚上别吵", hint: "23:00 到 07:30 免打扰", patch: { quietStart: "23:00", quietEnd: "07:30" } },
];

/** 一键套一组常用设置；只提交和现在不一样的字段。 */
function Presets({ data, onSaved }: { data: ChatSettings; onSaved: (next: ChatSettings) => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const prefs = data.prefs as unknown as Record<string, unknown>;
  return <div className="me-preset-row" role="group" aria-label="一键套用">
    {PRESETS.map((preset) => {
      const changes = Object.fromEntries(Object.entries(preset.patch).filter(([key, value]) => prefs[key] !== value)) as Partial<ChatPrefs>;
      const active = Object.keys(changes).length === 0;
      return <button key={preset.key} type="button" className="me-preset" disabled={busy !== null} aria-pressed={active} onClick={async () => {
        if (active) return;
        const next = await run(`preset-${preset.key}`, () => spaceApi.patchChatPrefs(changes), `换成「${preset.title}」了`);
        if (next) onSaved(next);
      }}>
        <strong>{preset.title}{active && <Tag color="success" className="me-inline-tag">现在就是</Tag>}</strong>
        <span>{preset.hint}</span>
      </button>;
    })}
  </div>;
}

function PrefsForm({ data, onSaved }: { data: ChatSettings; onSaved: (next: ChatSettings) => void }): React.JSX.Element {
  const [form] = Form.useForm<ChatPrefs>();
  const { run } = useAction();
  const saved = useRef(data.prefs);
  // 服务端数据变了（刷新、别处改过）时，只同步用户没在改的字段，免得冲掉正在输入的内容
  useEffect(() => {
    const current = form.getFieldsValue(true) as Partial<ChatPrefs>;
    const sync: Partial<ChatPrefs> = {};
    for (const key of Object.keys(data.prefs) as PrefKey[]) {
      if (current[key] === undefined || current[key] === saved.current[key]) (sync as Record<string, unknown>)[key] = data.prefs[key];
    }
    form.setFieldsValue(sync);
    saved.current = data.prefs;
  }, [data, form]);

  const saveFields = async (keys: PrefKey[]): Promise<void> => {
    try {
      await form.validateFields(keys);
    } catch {
      return; // 输入框下方已经写明哪里不对
    }
    // 只交改过的字段；清空的输入框交 ""，后端当作“关掉”
    const changes: Partial<ChatPrefs> = {};
    for (const key of keys) {
      const value = form.getFieldValue(key) as ChatPrefs[PrefKey] | undefined;
      const normalized = typeof value === "string" ? value.trim().replace("：", ":") : value;
      if (normalized !== saved.current[key]) (changes as Record<string, unknown>)[key] = normalized ?? "";
    }
    if (Object.keys(changes).length === 0) return;
    const next = await run(`save-${keys.join("-")}`, () => spaceApi.patchChatPrefs(changes), "已保存，下一句私聊就生效");
    if (next) onSaved(next);
  };
  const textField = (key: PrefKey) => ({
    onBlur: () => void saveFields(QUIET_KEYS.includes(key) ? QUIET_KEYS : [key]),
    onPressEnter: () => void saveFields(QUIET_KEYS.includes(key) ? QUIET_KEYS : [key]),
  });

  return <Form
    form={form}
    layout="vertical"
    requiredMark={false}
    initialValues={data.prefs}
    onValuesChange={(changed: Partial<ChatPrefs>) => {
      const keys = (Object.keys(changed) as PrefKey[]).filter((key) => CHOICE_KEYS.includes(key));
      if (keys.length) void saveFields(keys);
    }}
    className="me-prefs-form"
  >
    <div className="me-form-grid">
      <Form.Item name="callMe" label="怎么称呼你" extra="留空就用你的群名片/昵称">
        <Input maxLength={16} placeholder="比如：小猫、老板" allowClear {...textField("callMe")} />
      </Form.Item>
      <Form.Item name="replyLength" label="回复长短">
        <Segmented options={REPLY_LENGTH_OPTIONS.map((item) => ({ value: item.value, label: item.label }))} />
      </Form.Item>
      <Form.Item name="emoji" label="表情符号">
        <Segmented options={EMOJI_OPTIONS.map((item) => ({ value: item.value, label: item.label }))} />
      </Form.Item>
      <Form.Item name="birthday" label="生日（月-日）" extra="生日当天 Bot 会私聊祝你" rules={[{ validator: async (_rule, value: string) => { if (value && !isBirthday(value)) throw new Error("写成 MM-DD，比如 03-14"); } }]}>
        <Input maxLength={5} placeholder="MM-DD" allowClear {...textField("birthday")} />
      </Form.Item>
    </div>
    <div className="me-subhead"><BellOutlined /> 主动消息</div>
    <div className="me-form-grid">
      <Form.Item name="proactive" label="允许 Bot 主动找你" valuePropName="checked" extra={data.agent.proactiveAllowed ? "关掉后早安、签到提醒、生日祝福都不发；你自己设的提醒照常送达" : "管理员暂时关掉了全站的主动消息"}>
        <Switch disabled={!data.agent.proactiveAllowed} />
      </Form.Item>
      <Form.Item name="morningGreeting" label="早安问候时间" extra="留空不发" rules={[clockRule("早安时间")]}>
        <Input maxLength={5} placeholder="07:30" allowClear {...textField("morningGreeting")} />
      </Form.Item>
      <Form.Item name="checkinNudge" label="提醒我签到" extra="到点还没签到就私聊提醒，留空不提醒" rules={[clockRule("签到提醒")]}>
        <Input maxLength={5} placeholder="21:00" allowClear {...textField("checkinNudge")} />
      </Form.Item>
    </div>
    <div className="me-subhead"><StopOutlined /> 免打扰</div>
    <div className="me-form-grid">
      <Form.Item name="quietStart" label="从" rules={[clockRule("开始时间"), ({ getFieldValue }) => ({ validator: async (_rule, value: string) => { if (Boolean(value) !== Boolean(getFieldValue("quietEnd"))) throw new Error("开始和结束要一起填，或一起清空"); } })]} dependencies={["quietEnd"]}>
        <Input maxLength={5} placeholder="23:00" allowClear {...textField("quietStart")} />
      </Form.Item>
      <Form.Item name="quietEnd" label="到" rules={[clockRule("结束时间")]}>
        <Input maxLength={5} placeholder="08:00" allowClear {...textField("quietEnd")} />
      </Form.Item>
      <Form.Item label="现在">
        <Text type="secondary">{quietLabel(data.prefs.quietStart, data.prefs.quietEnd)}{data.inQuietHours && <Tag color="purple" className="me-inline-tag">正在免打扰</Tag>}</Text>
      </Form.Item>
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
  const commitHours = (): void => {
    if (hours === null) { setHours(data.everyHours); return; }
    if (hours !== data.everyHours) void patch("every", { everyHours: hours }, "已保存间隔");
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
          <InputNumber
            size="small"
            min={data.minEveryHours}
            max={48}
            step={0.5}
            value={hours}
            onChange={(value) => setHours(value)}
            onBlur={commitHours}
            onPressEnter={commitHours}
            disabled={busy === "every"}
            aria-label="间隔小时"
          />
          小时问一次
          {data.everyHours !== data.defaultEveryHours && <Button size="small" type="link" onClick={() => void patch("every", { resetInterval: true }, `已恢复默认 ${data.defaultEveryHours} 小时`)}>恢复默认 {data.defaultEveryHours} 小时</Button>}
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
            <Select<number>
              size="small"
              placeholder="先别问我…"
              value={undefined}
              loading={busy === "snooze"}
              style={{ width: 140 }}
              onChange={(value) => void patch("snooze", { snoozeHours: value }, "好的，这段时间不问你")}
              options={[3, 24, 72, 168].map((value) => ({ value, label: value < 24 ? `${value} 小时内别问` : `${value / 24} 天内别问` }))}
              aria-label="暂停主动提议"
            />
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
      <MeSection icon={<MessageOutlined />} title="说话方式与打扰" hint={<>改完自动保存：选项点一下就生效，文字填好后点别处或回车。想改 Bot 的自称和性格，去 <Link to="/me/persona">私聊人设</Link>。</>}>
        <Presets data={data} onSaved={setData} />
        <PrefsForm data={data} onSaved={setData} />
      </MeSection>
      <OffersCard />
    </>}
  </div>;
}
