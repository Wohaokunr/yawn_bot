import { KeyOutlined, LogoutOutlined, MessageOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Descriptions, Form, Input, Popconfirm, Progress, Typography } from "antd";
import { useState } from "react";
import { setCsrfToken } from "../api";
import { PageHeader, QueryErrorAlert, useApiQuery } from "../shared";
import { meApi } from "./me-api";
import { formatQqTime, passwordProblem, passwordStrength } from "./me-meta";
import "./me.css";

const { Text, Paragraph } = Typography;

const SOURCE_LABEL: Record<string, string> = {
  private_command: "私聊 /网页密码",
  setup_code: "网页 · 设置码",
  webui: "网页 · 修改密码",
};

export function MyAccountPage({ onLoggedOut }: { onLoggedOut: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({ queryKey: ["me"], fetcher: () => meApi.profile() });
  const [form] = Form.useForm<{ current: string; next: string; confirm: string }>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const next = Form.useWatch("next", form) ?? "";
  const strength = passwordStrength(next);
  const me = query.data;

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
    <PageHeader title="账号安全" subtitle="你的网页个人账号：QQ 号 + 你自己设的密码" onRefresh={query.reload} refreshing={query.refreshing} />
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
          <Descriptions.Item label="本次登录到期">{me ? formatQqTime(new Date(me.sessionExpiresAt * 1000).toISOString()) : "—"}</Descriptions.Item>
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

      <Card size="small" title={<span><MessageOutlined /> 忘记密码 / 在私聊里管理</span>}>
        <Paragraph className="me-tip-list">
          <ul>
            <li>私聊 Bot 发 <Text code>/网页密码</Text>：拿一次性设置码，在登录页“用设置码设密码”。</li>
            <li>私聊 Bot 发 <Text code>/网页登录</Text>：看账号状态和你能管理的群。</li>
            <li><Text code>/网页登录 下线</Text> 退出所有设备；<Text code>/网页登录 注销</Text> 删除账号。</li>
            <li>也可以直接跟 Bot 说“帮我弄个网页登录的设置码”。</li>
          </ul>
          <Text type="secondary">密码和设置码都不要发给任何人，Bot 和管理员不会找你要。</Text>
        </Paragraph>
      </Card>
    </div>
  </div>;
}
