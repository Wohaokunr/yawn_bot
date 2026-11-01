import {
  KeyOutlined,
  LockOutlined,
  NumberOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Alert, Button, Card, Form, Input, Segmented, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { api } from "./api";
import type { AuthSessionData } from "./auth-session";
import { isQqNumber, normalizeSetupCode, passwordProblem } from "./me/me-meta";
import "./me/me-login.css";

const { Title, Paragraph, Text } = Typography;
type LoginMode = "user" | "admin" | "guest";
type UserStep = "login" | "setup";

interface LoginValues {
  credential?: string;
  qq?: string;
  password?: string;
  code?: string;
  confirm?: string;
}

const MODE_COPY: Record<LoginMode, { kicker: string; intro: string; note: string; submit: string }> = {
  user: {
    kicker: "MY SPACE",
    intro: "用 QQ 号和你在私聊里设的密码登录：管理你当群主/管理员的群，调整 Bot 和你私聊时的人设。",
    note: "还没有密码？私聊 Bot 发 /网页密码 拿一次性设置码，或直接发 /网页密码 新密码。",
    submit: "登录我的空间",
  },
  admin: {
    kicker: "CONTROL ROOM",
    intro: "使用部署时配置的运维 Token 进入完整管理台。",
    note: "运维登录保持原有权限与行为不变。",
    submit: "进入管理台",
  },
  guest: {
    kicker: "GUEST PASS",
    intro: "使用管理员签发的访客访问码，只查看被授权群聊的记忆、成员画像与关系边。",
    note: "访客会话没有写权限，管理员关闭访客访问或轮换凭据后会立即失效。",
    submit: "进入访客视图",
  },
};

const MODE_KEY = "yawnbot.login.mode";

/** 默认仍是运维登录；私聊里发的链接带 ?login=qq，或上次用过 QQ 账号，就直接打开 QQ 账号。 */
export function initialLoginMode(search = window.location.search): LoginMode {
  const wanted = new URLSearchParams(search).get("login");
  if (wanted === "qq" || wanted === "user") return "user";
  if (wanted === "guest") return "guest";
  try {
    const stored = window.localStorage.getItem(MODE_KEY);
    if (stored === "user" || stored === "guest" || stored === "admin") return stored;
  } catch {
    // 隐私模式下读不到偏好，按默认
  }
  return "admin";
}

function rememberMode(mode: LoginMode): void {
  try {
    window.localStorage.setItem(MODE_KEY, mode);
  } catch {
    // 忽略
  }
}

export function Login({ onSuccess }: { onSuccess: (session: AuthSessionData) => void }): React.JSX.Element {
  const [mode, setMode] = useState<LoginMode>(() => initialLoginMode());
  const [step, setStep] = useState<UserStep>("login");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [form] = Form.useForm<LoginValues>();
  const isGuest = mode === "guest";
  const isUser = mode === "user";
  const isSetup = isUser && step === "setup";
  const copy = MODE_COPY[mode];

  const switchMode = (value: string | number) => {
    setMode(value as LoginMode);
    setStep("login");
    setError("");
    form.resetFields();
  };

  const switchStep = (next: UserStep) => {
    setStep(next);
    setError("");
    const qq = form.getFieldValue("qq") as string | undefined;
    form.resetFields();
    if (qq) form.setFieldValue("qq", qq);
  };

  const submit = async (values: LoginValues) => {
    setLoading(true);
    setError("");
    try {
      let path = isGuest ? "/auth/guest" : "/auth/login";
      let body: Record<string, string> = { token: values.credential ?? "" };
      if (isUser) {
        const qq = (values.qq ?? "").trim();
        path = isSetup ? "/auth/user/setup" : "/auth/user";
        body = isSetup
          ? { qq, code: normalizeSetupCode(values.code ?? ""), password: values.password ?? "" }
          : { qq, password: values.password ?? "" };
      }
      const { data } = await api<AuthSessionData>(path, { method: "POST", body: JSON.stringify(body) });
      rememberMode(mode);
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
        <div className="brand-mark">{isGuest ? <TeamOutlined /> : isUser ? <UserOutlined /> : <RobotOutlined />}</div>
        <Space direction="vertical" size={4} style={{ width: "100%", marginBottom: 18 }}>
          <span className="login-kicker">✦ {copy.kicker}</span>
          <Space align="center" wrap>
            <Title level={2} style={{ margin: 0 }}>YawnBot WebUI</Title>
            {isGuest && <Tag color="blue" icon={<SafetyCertificateOutlined />}>只读</Tag>}
          </Space>
          <Paragraph type="secondary" style={{ margin: 0 }}>{copy.intro}</Paragraph>
        </Space>

        <Segmented
          block
          className="login-mode-switch"
          value={mode}
          onChange={switchMode}
          options={[
            { value: "admin", label: <Space size={6}><KeyOutlined />运维登录</Space> },
            { value: "user", label: <Space size={6}><UserOutlined />QQ 账号</Space> },
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

        <Form form={form} layout="vertical" onFinish={submit} requiredMark={false} key={`${mode}-${step}`}>
          {isUser ? (
            <>
              {isSetup && (
                <Alert
                  className="login-setup-hint"
                  type="info"
                  showIcon
                  message="私聊 Bot 发 /网页密码（不带参数）拿 8 位设置码，10 分钟内有效、只能用一次。"
                />
              )}
              <Form.Item
                name="qq"
                label="QQ 号"
                rules={[
                  { required: true, message: "请输入 QQ 号" },
                  { validator: async (_rule, value: string) => { if (value && !isQqNumber(value)) throw new Error("QQ 号格式不对"); } },
                ]}
              >
                <Input autoFocus inputMode="numeric" autoComplete="username" size="large" prefix={<UserOutlined />} placeholder="你的 QQ 号" maxLength={12} />
              </Form.Item>
              {isSetup && (
                <Form.Item name="code" label="设置码" rules={[{ required: true, message: "请输入私聊里收到的设置码" }]}>
                  <Input autoComplete="one-time-code" size="large" prefix={<NumberOutlined />} placeholder="8 位设置码" maxLength={16} className="login-code-input" />
                </Form.Item>
              )}
              <Form.Item
                name="password"
                label={isSetup ? "新密码" : "密码"}
                extra={isSetup ? "至少 8 位，混用字母、数字、符号里的两种；别用 QQ 号" : undefined}
                rules={[
                  { required: true, message: isSetup ? "请设置新密码" : "请输入密码" },
                  ...(isSetup
                    ? [{ validator: async (_rule: unknown, value: string) => { const problem = value ? passwordProblem(value, form.getFieldValue("qq") as string) : null; if (problem) throw new Error(problem); } }]
                    : []),
                ]}
              >
                <Input.Password autoComplete={isSetup ? "new-password" : "current-password"} size="large" prefix={<LockOutlined />} placeholder={isSetup ? "设置一个新密码" : "私聊里设的密码"} />
              </Form.Item>
              {isSetup && (
                <Form.Item
                  name="confirm"
                  label="再输一次"
                  dependencies={["password"]}
                  rules={[
                    { required: true, message: "请再输一次新密码" },
                    ({ getFieldValue }) => ({ validator: async (_rule, value: string) => { if (value && value !== getFieldValue("password")) throw new Error("两次输入的不一样"); } }),
                  ]}
                >
                  <Input.Password autoComplete="new-password" size="large" prefix={<LockOutlined />} placeholder="确认新密码" />
                </Form.Item>
              )}
            </>
          ) : (
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
          )}
          <Button type="primary" htmlType="submit" size="large" block loading={loading}>
            {isSetup ? "设置密码并登录" : copy.submit}
          </Button>
        </Form>

        {isUser && (
          <div className="login-user-switch">
            {isSetup
              ? <Button type="link" size="small" onClick={() => switchStep("login")}>已经有密码？直接登录</Button>
              : <Button type="link" size="small" onClick={() => switchStep("setup")}>第一次用 / 忘记密码？用设置码设密码</Button>}
          </div>
        )}

        <div className="login-mode-note">
          <Text type="secondary">{isSetup ? "设置码只能用一次；设好密码后，之前登录的设备会全部退出。" : copy.note}</Text>
        </div>
      </Card>
    </div>
  );
}
