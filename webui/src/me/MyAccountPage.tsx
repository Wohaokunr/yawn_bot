import { BulbOutlined, CommentOutlined, DatabaseOutlined, DownloadOutlined, KeyOutlined, LogoutOutlined, MessageOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Descriptions, Form, Input, Popconfirm, Progress, Typography } from "antd";
import { useState } from "react";
import { Link } from "react-router-dom";
import { setCsrfToken } from "../api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { EXPORT_URL, spaceApi } from "./me-space-api";
import { formatQqTime, passwordProblem, passwordStrength } from "./me-meta";
import { MyDevicesCards } from "./MyDevicesCards";
import "./me.css";

const { Text, Paragraph } = Typography;

const SOURCE_LABEL: Record<string, string> = {
  private_command: "私聊 /网页密码",
  setup_code: "网页 · 设置码",
  webui: "网页 · 修改密码",
};

/** 秒级时间戳 → 北京时间 “YYYY-MM-DD HH:mm”（和后端其他时间同口径） */
export function formatBeijing(epochSeconds: number): string {
  return formatQqTime(new Date((epochSeconds + 8 * 3600) * 1000).toISOString());
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
          <Descriptions.Item label="本次登录到期">
            {me ? formatBeijing(me.sessionExpiresAt) : "—"}
            {me?.sessionRemember && <Text type="secondary"> · 自动登录（使用中会顺延）</Text>}
          </Descriptions.Item>
          <Descriptions.Item label="在线设备">{me?.activeDeviceCount ?? "—"}</Descriptions.Item>
        </Descriptions>
        <Popconfirm
          title="退出所有设备？"
          description="包括这一台，所有登录都要重新输入密码。"
          okText="全部退出"
          okButtonProps={{ danger: true }}
          onConfirm={logoutAll}
        >
          <Button danger icon={<LogoutOutlined />} block>退出所有设备</Button>
        </Popconfirm>
      </Card>

      <MyDevicesCards onLoggedOut={onLoggedOut} />

      <Card size="small" title={<span><KeyOutlined /> 修改密码</span>}>
        {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} className="me-banner" />}
        <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void submit(values)}>
          <Form.Item name="current" label="当前密码" rules={[{ required: true, message: "请输入当前密码" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
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

      <Card size="small" title={<span><MessageOutlined /> 忘记密码 / 在私聊里管理</span>}>
        <Paragraph className="me-tip-list">
          <ul>
            <li>私聊 Bot 发 <Text code>/网页密码</Text>：拿一次性设置码，在登录页“用设置码设密码”。</li>
            <li>私聊 Bot 发 <Text code>/网页登录</Text>：看账号状态和你能管理的群。</li>
            <li><Text code>/网页登录 设备</Text> 看在线设备和最近登录；<Text code>/网页登录 提醒 新地点</Text>（或 每次 / 关闭）设置登录提醒。</li>
            <li><Text code>/网页登录 下线</Text> 退出所有设备；<Text code>/网页登录 注销</Text> 删除账号。</li>
            <li>也可以直接跟 Bot 说“帮我弄个网页登录的设置码”。</li>
          </ul>
          <Text type="secondary">密码和设置码都不要发给任何人，Bot 和管理员不会找你要。</Text>
        </Paragraph>
      </Card>
    </div>
  </div>;
}
