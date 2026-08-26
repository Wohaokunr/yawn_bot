import {
  KeyOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
} from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Segmented, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { api } from "./api";
import type { AuthSessionData } from "./auth-session";

const { Title, Paragraph, Text } = Typography;
type LoginMode = "admin" | "guest";

export function Login({ onSuccess }: { onSuccess: (session: AuthSessionData) => void }): React.JSX.Element {
  const [mode, setMode] = useState<LoginMode>("admin");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [form] = Form.useForm<{ credential: string }>();
  const isGuest = mode === "guest";

  const switchMode = (value: string | number) => {
    setMode(value as LoginMode);
    setError("");
    form.resetFields();
  };

  const submit = async ({ credential }: { credential: string }) => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api<AuthSessionData>(isGuest ? "/auth/guest" : "/auth/login", {
        method: "POST",
        body: JSON.stringify({ token: credential }),
      });
      onSuccess(data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : isGuest ? "访客登录失败" : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="petals" aria-hidden="true">
        {[0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} />)}
      </div>
      <Card className="login-card" variant="borderless">
        <div className="brand-mark">{isGuest ? <TeamOutlined /> : <RobotOutlined />}</div>
        <Space direction="vertical" size={4} style={{ width: "100%", marginBottom: 18 }}>
          <Space align="center" wrap>
            <Title level={2} style={{ margin: 0 }}>YawnBot WebUI</Title>
            {isGuest && <Tag color="blue" icon={<SafetyCertificateOutlined />}>只读</Tag>}
          </Space>
          <Paragraph type="secondary" style={{ margin: 0 }}>
            {isGuest
              ? "使用管理员签发的访客访问码，只查看被授权群聊的记忆、成员画像与关系边。"
              : "使用部署时配置的运维 Token 进入完整管理台。"}
          </Paragraph>
        </Space>

        <Segmented
          block
          className="login-mode-switch"
          value={mode}
          onChange={switchMode}
          options={[
            { value: "admin", label: <Space size={6}><KeyOutlined />运维登录</Space> },
            { value: "guest", label: <Space size={6}><TeamOutlined />访客登录</Space> },
          ]}
        />

        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            closable
            onClose={() => setError("")}
          />
        )}

        <Form form={form} layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item
            name="credential"
            label={isGuest ? "访客访问码" : "运维 Token"}
            rules={[{ required: true, message: isGuest ? "请输入访客访问码" : "请输入运维 Token" }]}
          >
            <Input.Password
              autoFocus
              autoComplete="current-password"
              size="large"
              prefix={isGuest ? <TeamOutlined /> : <KeyOutlined />}
              placeholder={isGuest ? "输入访客访问码" : "输入运维 Token"}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={loading}>
            {isGuest ? "进入访客视图" : "进入管理台"}
          </Button>
        </Form>

        <div className="login-mode-note">
          <Text type="secondary">
            {isGuest
              ? "访客会话没有写权限，管理员关闭访客访问或轮换凭据后会立即失效。"
              : "运维登录保持原有权限与行为不变。"}
          </Text>
        </div>
      </Card>
    </div>
  );
}
