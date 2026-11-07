// 「账号与数据」里的登录设备与登录记录（后端 self_service_routes.py：/me/devices*、/me/security/login-alert）。
import { DesktopOutlined, HistoryOutlined, LogoutOutlined, MobileOutlined, WarningOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Popconfirm, Select, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { setCsrfToken } from "../api";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { meApi, type LoginAlertMode, type MyDevice, type MySecurity } from "./me-api";
import { formatQqTime } from "./me-meta";

const { Text } = Typography;

const MOBILE = /iPhone|iPad|Android|鸿蒙/;

export function deviceIcon(device: string): React.JSX.Element {
  return MOBILE.test(device) ? <MobileOutlined /> : <DesktopOutlined />;
}

function DeviceRow({ item, onLogout, busy }: { item: MyDevice; onLogout: (item: MyDevice) => void; busy: boolean }): React.JSX.Element {
  return (
    <div className={`me-device-row${item.current ? " is-current" : ""}`}>
      <span className="me-device-icon" aria-hidden="true">{deviceIcon(item.device)}</span>
      <div className="me-device-main">
        <Space size={6} wrap>
          <Text strong>{item.device}</Text>
          {item.current && <Tag>这台设备</Tag>}
          {item.remember && <Tag>自动登录</Tag>}
        </Space>
        <Text type="secondary" className="me-device-meta">
          {item.lastLocation} · 最近活跃 {formatQqTime(item.lastSeenAt)} · 登录于 {formatQqTime(item.createdAt)}
        </Text>
      </div>
      <Popconfirm
        title={item.current ? "退出这台设备？" : "让这台设备下线？"}
        description={item.current ? "退出后要重新登录。" : "它马上就要重新输入密码才能登录。"}
        okText="下线"
        okButtonProps={{ danger: true }}
        onConfirm={() => onLogout(item)}
      >
        <Button danger={!item.current} loading={busy} className="me-device-action">{item.current ? "退出" : "下线"}</Button>
      </Popconfirm>
    </div>
  );
}

/**
 * 用词：「下线」= 让别的设备退出；「退出」= 这一台，或连同这一台的所有设备。
 * onLogoutAll 给了就在设备卡片底部放「退出所有设备」，账号页只在这一处放这个按钮。
 */
export function MyDevicesCards({ onLoggedOut, onLogoutAll }: { onLoggedOut: () => void; onLogoutAll?: () => Promise<void> }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({ queryKey: ["me", "devices"], fetcher: () => meApi.security() });
  const [override, setOverride] = useState<MySecurity | null>(null);
  const [busy, setBusy] = useState<string>("");
  const data = override ?? query.data;
  const others = (data?.devices ?? []).filter((item) => !item.current).length;

  const run = async (key: string, action: () => Promise<void>): Promise<void> => {
    setBusy(key);
    try {
      await action();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  };

  const logoutOne = (item: MyDevice) => run(item.id, async () => {
    const result = await meApi.logoutDevice(item.id);
    if (result.current) {
      setCsrfToken("", null);
      onLoggedOut();
      return;
    }
    if (result.devices) setOverride(result as MySecurity);
    message.success(`${item.device} 已下线`);
    query.reload();
  });

  const logoutOthers = () => run("others", async () => {
    const result = await meApi.logoutOthers();
    setOverride(result);
    message.success(result.loggedOut ? `已让其他 ${result.loggedOut} 台设备下线` : "没有其他设备在线");
    query.reload();
  });

  const changeAlert = (mode: LoginAlertMode) => run("alert", async () => {
    setOverride(await meApi.setLoginAlert(mode));
    message.success("登录提醒已更新");
  });

  return <>
    <Card
      size="small"
      title={<span><DesktopOutlined /> 登录设备{data ? ` · ${data.activeCount} 台在线` : ""}</span>}
      extra={others > 0 && (
        <Popconfirm title="让其他设备全部下线？" description="只保留这一台，不会改密码。" okText="全部下线" okButtonProps={{ danger: true }} onConfirm={() => void logoutOthers()}>
          <Button size="small" icon={<LogoutOutlined />} loading={busy === "others"}>下线其他设备</Button>
        </Popconfirm>
      )}
    >
      {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {data && data.devices.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有在线设备" />}
      <div className="me-device-list">
        {data?.devices.map((item) => <DeviceRow key={item.id} item={item} busy={busy === item.id} onLogout={(target) => void logoutOne(target)} />)}
      </div>
      {data && (
        <Text type="secondary" className="me-hint">
          {data.sessionRemember
            ? `这台设备开着自动登录：${data.rememberDays} 天内打开免登录，每次使用都会顺延。`
            : "这台设备没开自动登录：关掉浏览器后要重新登录。"}
          位置只显示 IP 段，不是精确地址。
        </Text>
      )}
      {onLogoutAll && (
        <div className="me-device-footer">
          <Popconfirm
            title="退出所有设备？"
            description="包括这一台，所有登录都要重新输入密码。"
            okText="全部退出"
            okButtonProps={{ danger: true }}
            onConfirm={onLogoutAll}
          >
            <Button danger type="text" icon={<LogoutOutlined />}>退出所有设备（包括这台）</Button>
          </Popconfirm>
        </div>
      )}
    </Card>

    <Card size="small" title={<span><HistoryOutlined /> 最近登录</span>}>
      {data && !data.alertAvailable && (
        <Alert className="me-banner" type="info" showIcon message="管理员暂时关闭了登录提醒，设置会保留，恢复后生效。" />
      )}
      <div className="me-alert-row">
        <Text>登录提醒</Text>
        <Select<LoginAlertMode>
          size="small"
          value={data?.alertMode}
          options={data?.alertModes}
          onChange={(mode) => void changeAlert(mode)}
          loading={busy === "alert"}
          disabled={!data}
          style={{ minWidth: 180 }}
          aria-label="登录提醒"
        />
      </div>
      {data && data.logins.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有登录记录" />}
      <ul className="me-login-list">
        {data?.logins.map((item, index) => (
          <li key={`${item.at ?? ""}-${index}`}>
            <span className="me-login-time">{formatQqTime(item.at)}</span>
            <span className="me-login-what">
              {item.device} · {item.location}
              {item.newLocation && <Tag color="orange" icon={<WarningOutlined />} className="me-login-flag">新地点</Tag>}
            </span>
            <Text type="secondary" className="me-login-method">{item.methodLabel}</Text>
          </li>
        ))}
      </ul>
      <Text type="secondary" className="me-hint">有不认识的登录？先「退出所有设备」，再改密码。私聊 Bot 发 /网页登录 设备 也能查看。</Text>
    </Card>
  </>;
}
