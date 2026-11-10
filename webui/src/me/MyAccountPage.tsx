import { BgColorsOutlined, BulbOutlined, CommentOutlined, DatabaseOutlined, DeleteOutlined, DownloadOutlined, KeyOutlined, MessageOutlined, SafetyCertificateOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Collapse, Descriptions, Form, Input, Modal, Progress, Segmented, Select, Switch, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { setCsrfToken } from "../api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { EXPORT_URL, spaceApi } from "./me-space-api";
import { formatQqTime, passwordProblem, passwordStrength } from "./me-meta";
import { MyDevicesCards } from "./MyDevicesCards";
import { USER_NAV } from "../nav-config";
import { announceMeChanged, copyText, type Density, type FontScale, useLocalPrefs, useUiPrefs } from "./hub-ui";
import { type ClearSection, hubApi } from "./me-hub-api";
import { GO_KEYS, SHORTCUTS, suggestPassword } from "./me-hub-meta";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text, Paragraph } = Typography;

const SOURCE_LABEL: Record<string, string> = {
  private_command: "私聊 /网页密码",
  setup_code: "网页 · 设置码",
  webui: "网页 · 修改密码",
};

/** 秒级时间戳 → 北京时间 “YYYY-MM-DD HH:mm”（和后端其他时间同口径）；缺值或 0 显示 “—”，不显示 1970 年 */
export function formatBeijing(epochSeconds: number | null | undefined): string {
  if (!epochSeconds || !Number.isFinite(epochSeconds) || epochSeconds <= 0) return "—";
  return formatQqTime(new Date((epochSeconds + 8 * 3600) * 1000).toISOString());
}

const CLEARABLE: Array<{ section: ClearSection; label: string; count: string }> = [
  { section: "todos", label: "待办", count: "todos" },
  { section: "notes", label: "笔记", count: "notes" },
  { section: "countdowns", label: "倒数日", count: "countdowns" },
  { section: "moods", label: "心情", count: "moods" },
];

/** 显示偏好：密度、字号、少动效只存在这台设备；打开网页先到哪一页跟着账号走。 */
function DisplayCard(): React.JSX.Element {
  const { local, setLocal } = useLocalPrefs();
  const { prefs, save } = useUiPrefs();
  const landings = (prefs?.landings ?? ["/me"]).map((key) => ({ value: key, label: USER_NAV.find((entry) => entry.key === key)?.label ?? key }));
  return <Card size="small" title={<span><BgColorsOutlined /> 显示偏好</span>}>
    <div className="me-pref-grid">
      <div className="me-pref-item">
        <strong>排版</strong>
        <Segmented<Density> value={local.density} onChange={(density) => setLocal({ density })} options={[{ value: "cozy", label: "宽松" }, { value: "compact", label: "紧凑" }]} aria-label="排版密度" />
        <Text type="secondary" className="me-hint">紧凑一屏能看到更多行。</Text>
      </div>
      <div className="me-pref-item">
        <strong>字号</strong>
        <Segmented<FontScale> value={local.font} onChange={(font) => setLocal({ font })} options={[{ value: "s", label: "小" }, { value: "m", label: "标准" }, { value: "l", label: "大" }]} aria-label="字号" />
        <Text type="secondary" className="me-hint">只调“我的空间”里的正文。</Text>
      </div>
      <div className="me-pref-item">
        <strong>少一点动效</strong>
        <Switch checked={local.calm} onChange={(calm) => setLocal({ calm })} aria-label="少一点动效" />
        <Text type="secondary" className="me-hint">关掉过渡和闪烁；系统开了“减少动态效果”时会自动关。</Text>
      </div>
      <div className="me-pref-item">
        <strong>打开网页先到</strong>
        <Select value={prefs?.landing ?? "/me"} onChange={(landing) => save({ landing })} options={landings} disabled={!prefs} aria-label="打开网页先到哪一页" className="me-full" />
        <Text type="secondary" className="me-hint">这一项跟着账号走，换设备也一样。</Text>
      </div>
    </div>
    <Paragraph type="secondary" className="me-hint">前三项只存在这台设备。深浅色在右上角切换。主页上显示哪些板块，在主页右上角“自定义主页”里调。{prefs?.onboardingDismissed && <Button type="link" size="small" onClick={() => save({ onboardingDismissed: false })}>重新显示上手清单</Button>}</Paragraph>
  </Card>;
}

/** 整类清空：要亲手输入类别名才放行。 */
function ClearCard({ counts, onCleared }: { counts: Record<string, number>; onCleared: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [target, setTarget] = useState<(typeof CLEARABLE)[number] | null>(null);
  const [typed, setTyped] = useState("");
  const [working, setWorking] = useState(false);
  const submit = async (): Promise<void> => {
    if (!target || typed.trim() !== target.label) return;
    setWorking(true);
    try {
      const result = await hubApi.clearSection(target.section, typed.trim());
      message.success(`清掉了 ${result.cleared} 条${target.label}`);
      announceMeChanged();
      onCleared();
      setTarget(null);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
    }
  };
  return <Card size="small" title={<span><DeleteOutlined /> 清空某一类</span>}>
    <Paragraph type="secondary" className="me-hint">一次删掉一整类，删了找不回来。建议先在左边导出一份。提醒在<Link to="/me/schedules">我的定时任务</Link>里一条条管。</Paragraph>
    <ul className="me-danger-list">
      {CLEARABLE.map((row) => (
        <li key={row.section}>
          <span className="me-plain-main"><strong>{row.label}</strong><Text type="secondary" className="me-hint">现在有 {counts[row.count] ?? 0} 条</Text></span>
          <Button danger size="small" disabled={!(counts[row.count] ?? 0)} onClick={() => { setTyped(""); setTarget(row); }}>清空{row.label}</Button>
        </li>
      ))}
    </ul>
    <Modal open={target !== null} title={target ? `清空全部${target.label}？` : ""} okText="清空" okButtonProps={{ danger: true, disabled: !target || typed.trim() !== target.label, loading: working }} onOk={() => void submit()} onCancel={() => setTarget(null)} destroyOnHidden>
      {target && <div className="me-stack">
        <Text>会删掉全部 {counts[target.count] ?? 0} 条{target.label}，Bot 在私聊里也翻不到了。这一步不能撤销。</Text>
        <Input autoFocus value={typed} onChange={(event) => setTyped(event.target.value)} onPressEnter={() => void submit()} placeholder={`输入“${target.label}”确认`} aria-label={`输入${target.label}确认`} />
      </div>}
    </Modal>
  </Card>;
}

export function MyAccountPage({ onLoggedOut }: { onLoggedOut: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({ queryKey: ["me"], fetcher: () => meApi.profile() });
  const [form] = Form.useForm<{ current: string; next: string; confirm: string }>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const next = Form.useWatch("next", form) ?? "";
  const strength = passwordStrength(next);
  const me = query.data;
  const dash = useApiQuery({ queryKey: ["me", "dashboard"], fetcher: () => spaceApi.dashboard() });
  const counts = dash.data?.counts ?? {};
  const [suggested, setSuggested] = useState("");
  const useSuggested = async (): Promise<void> => {
    form.setFieldsValue({ next: suggested, confirm: suggested });
    message[(await copyText(suggested)) ? "success" : "info"]("填好了，记得把它存进密码管理器");
  };

  const submit = async (values: { current: string; next: string }): Promise<void> => {
    setSaving(true);
    setError("");
    try {
      const result = await meApi.changePassword(values.current, values.next);
      setCsrfToken(result.csrfToken, "user");
      form.resetFields();
      message.success("密码已修改，其他设备已退出登录");
      query.reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  const logoutAll = async (): Promise<void> => {
    try {
      await meApi.logoutAll();
    } finally {
      setCsrfToken("", null);
      onLoggedOut();
    }
  };

  return <div className="me-page">
    <PageHeader title="账号与数据" subtitle="你的网页个人账号（QQ 号 + 你自己设的密码），以及你在 Bot 这里留下的数据" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !me && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    <h3 className="me-account-heading">安全</h3>
    <div className="me-account-grid">
      <Card size="small" title={<span><SafetyCertificateOutlined /> 账号信息</span>}>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="QQ 号">{me?.userId ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="昵称">{me?.nickname ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="开通于">{formatQqTime(me?.account?.createdAt)}</Descriptions.Item>
          <Descriptions.Item label="密码设置于">
            {formatQqTime(me?.account?.passwordChangedAt)}
            {me?.account?.passwordSource && <Text type="secondary"> · {SOURCE_LABEL[me.account.passwordSource] ?? me.account.passwordSource}</Text>}
          </Descriptions.Item>
          <Descriptions.Item label="上次登录">{formatQqTime(me?.account?.lastLoginAt)}</Descriptions.Item>
          {me?.sessionExpiresAt ? (
            <Descriptions.Item label="本次登录到期">
              {formatBeijing(me.sessionExpiresAt)}
              {me.sessionRemember && <Text type="secondary"> · 自动登录（使用中会顺延）</Text>}
            </Descriptions.Item>
          ) : null}
        </Descriptions>
      </Card>

      <MyDevicesCards onLoggedOut={onLoggedOut} onLogoutAll={logoutAll} />

      <Card size="small" title={<span><KeyOutlined /> 修改密码</span>}>
        {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} className="me-banner" />}
        <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void submit(values)}>
          <Form.Item name="current" label="当前密码" rules={[{ required: true, message: "请输入当前密码" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <div className="me-suggest">
            <Button size="small" icon={<ThunderboltOutlined />} onClick={() => setSuggested(suggestPassword())}>帮我想一个</Button>
            {suggested && <><code>{suggested}</code><Button size="small" type="link" onClick={() => void useSuggested()}>用这个并复制</Button></>}
          </div>
          <Form.Item
            name="next"
            label="新密码"
            extra="至少 8 位，混用字母、数字、符号里的两种；别用 QQ 号"
            rules={[
              { required: true, message: "请输入新密码" },
              { validator: async (_rule, value: string) => { const problem = value ? passwordProblem(value, me?.userId) : null; if (problem) throw new Error(problem); } },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {next && <Progress className="me-strength" percent={(strength.score + 1) * 25} showInfo format={() => strength.label} size="small" />}
          <Form.Item
            name="confirm"
            label="再输一次"
            dependencies={["next"]}
            rules={[
              { required: true, message: "请再输一次新密码" },
              ({ getFieldValue }) => ({ validator: async (_rule, value: string) => { if (value && value !== getFieldValue("next")) throw new Error("两次输入的不一样"); } }),
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={saving} block>修改密码</Button>
        </Form>
      </Card>
    </div>

    <h3 className="me-account-heading">显示</h3>
    <DisplayCard />
    <h3 className="me-account-heading">数据</h3>
    <div className="me-account-grid">
      <Card size="small" title={<span><DatabaseOutlined /> 我的数据</span>}>
        <Descriptions column={2} size="small">
          <Descriptions.Item label="待办">{counts.todos ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="笔记">{counts.notes ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="私聊提醒">{counts.reminders ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="倒数日">{counts.countdowns ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="心情记录">{counts.moods ?? "—"}</Descriptions.Item>
          <Descriptions.Item label="后台任务">{dash.data?.tasks.total ?? "—"}</Descriptions.Item>
        </Descriptions>
        <Paragraph type="secondary" className="me-hint">导出一份 JSON：小本本、提醒、私聊偏好、Bot 记得你的事、好感流水、各群签到、对话目录。不含密码。</Paragraph>
        <Button type="primary" icon={<DownloadOutlined />} href={EXPORT_URL} block>导出我的数据</Button>
        <div className="me-link-row">
          <Link to="/me/memory"><BulbOutlined /> 管理 Bot 的记忆</Link>
          <Link to="/me/chats"><CommentOutlined /> 管理聊天记录</Link>
        </div>
      </Card>
      <ClearCard counts={counts} onCleared={dash.reload} />
    </div>
    <h3 className="me-account-heading">帮助</h3>
    <Collapse
      className="me-account-help"
      items={[{
        key: "help",
        label: <span><MessageOutlined /> 忘记密码、删除账号：在私聊里也能管</span>,
        children: <div className="me-tip-list">
          <ul>
            <li>忘记密码：私聊 Bot 发 <Text code>/网页密码</Text> 拿一次性设置码，再到登录页点“用设置码设密码”。</li>
            <li>看账号状态和你能管理的群：发 <Text code>/网页登录</Text>。</li>
            <li>删除网页账号：发 <Text code>/网页登录 注销</Text>。只删网页账号的密码和登录设备，之后想用再发 /网页密码 重新设置。</li>
            <li>记不住指令也没关系，直接跟 Bot 说“帮我弄个网页登录的设置码”。</li>
          </ul>
          <Text type="secondary">密码和设置码都不要发给任何人，Bot 和管理员不会找你要。</Text>
        </div>,
      }, {
        key: "keys",
        label: <span><ThunderboltOutlined /> 键盘快捷键</span>,
        children: <div>
          <ul className="me-key-list">{SHORTCUTS.map((row) => <li key={row.keys}><kbd>{row.keys}</kbd><span>{row.what}</span></li>)}</ul>
          <Text type="secondary" className="me-hint">先按 G，再按：</Text>
          <div className="me-key-grid">{Object.entries(GO_KEYS).map(([key, target]) => <span key={key}><kbd>{key.toUpperCase()}</kbd> {target.label}</span>)}</div>
        </div>,
      }]}
    />
  </div>;
}
