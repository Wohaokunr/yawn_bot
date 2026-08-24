import { RobotOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { api } from "./api";

const { Title, Paragraph } = Typography;

export function Login({ onSuccess }: { onSuccess: (csrf: string) => void }): React.JSX.Element {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async ({ token }: { token: string }) => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api<{ authenticated: boolean; csrfToken: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      onSuccess(data.csrfToken);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
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
          <Button type="primary" htmlType="submit" size="large" block loading={loading}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
