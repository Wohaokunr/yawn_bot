import { RobotOutlined, TeamOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Divider, Form, Input, Typography } from "antd";
import { useState } from "react";
import { api } from "./api";
import type { AuthSessionData } from "./auth-session";

const { Title, Paragraph } = Typography;

export function Login({ onSuccess }: { onSuccess: (session: AuthSessionData) => void }): React.JSX.Element {
  const [adminLoading, setAdminLoading] = useState(false);
  const [guestLoading, setGuestLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async ({ token }: { token: string }) => {
    setAdminLoading(true);
    setError("");
    try {
      const { data } = await api<AuthSessionData>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      onSuccess(data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setAdminLoading(false);
    }
  };

  const guestLogin = async ({ guestToken }: { guestToken: string }) => {
    setGuestLoading(true);
    setError("");
    try {
      const { data } = await api<AuthSessionData>("/auth/guest", {
        method: "POST",
        body: JSON.stringify({ token: guestToken }),
      });
      onSuccess(data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "访客登录失败");
    } finally {
      setGuestLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="petals" aria-hidden="true">
        {[0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} />)}
      </div>
      <Card className="login-card" variant="borderless">
        <div className="brand-mark"><RobotOutlined /></div>
        <Title level={2}>YawnBot 管理台</Title>
        <Paragraph type="secondary">使用部署时配置的运维 Token 登录</Paragraph>
        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            closable
            onClose={() => setError("")}
          />
        )}
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item
            name="token"
            label="运维 Token"
            rules={[{ required: true, message: "请输入运维 Token" }]}
          >
            <Input.Password autoFocus autoComplete="current-password" size="large" />
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={adminLoading}>
            运维登录
          </Button>
        </Form>
        <Divider plain>或</Divider>
        <Form layout="vertical" onFinish={guestLogin} requiredMark={false}>
          <Form.Item
            name="guestToken"
            label="访客访问码"
            rules={[{ required: true, message: "请输入访客访问码" }]}
          >
            <Input.Password autoComplete="current-password" size="large" />
          </Form.Item>
          <Button
            icon={<TeamOutlined />}
            htmlType="submit"
            size="large"
            block
            loading={guestLoading}
            disabled={adminLoading}
          >
            访客登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
