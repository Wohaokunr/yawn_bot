import { LogoutOutlined, SafetyCertificateOutlined, TeamOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Space, Tag, Typography } from "antd";
import { api, setCsrfToken } from "./api";
import type { AuthSessionData } from "./auth-session";

const { Title, Paragraph, Text } = Typography;

export function GuestHome({
  session,
  onLogout,
}: {
  session: AuthSessionData;
  onLogout: () => void;
}): React.JSX.Element {
  const logout = async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken("");
      onLogout();
    }
  };

  return (
    <div className="login-page">
      <div className="petals" aria-hidden="true">
        {[0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} />)}
      </div>
      <Card className="login-card" variant="borderless">
        <div className="brand-mark"><TeamOutlined /></div>
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <Space wrap>
              <Title level={2} style={{ marginBottom: 0 }}>访客模式</Title>
              <Tag icon={<SafetyCertificateOutlined />}>只读会话</Tag>
            </Space>
            <Paragraph type="secondary" style={{ marginTop: 8 }}>
              当前会话已按 guest 角色隔离，不能进入运维管理台或调用现有管理 API。
            </Paragraph>
          </div>
          <Alert
            type="info"
            showIcon
            message="访客策略已生效"
            description="当前会话已绑定管理员签发时的凭据版本。管理员关闭访客登录或轮换访问码后，本会话会立即失效；群聊授权也会按最新白名单实时判定。"
          />
          <Text type="secondary">
            当前能力：{session.capabilities.guestGroupRead ? "可访问管理员开放的群聊只读区域" : "尚未开放群聊只读能力"}
          </Text>
          <Button icon={<LogoutOutlined />} onClick={() => void logout()} block>
            退出访客模式
          </Button>
        </Space>
      </Card>
    </div>
  );
}
