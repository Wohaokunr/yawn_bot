import {
  ApiOutlined,
  AuditOutlined,
  BookOutlined,
  CrownOutlined,
  DashboardOutlined,
  LogoutOutlined,
  MenuOutlined,
  ReadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Breadcrumb, Button, Drawer, Layout, Menu, Space, Spin, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import { Suspense, useEffect, useMemo, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, openStatusStream, setCsrfToken } from "./api";
import { confirmDiscardChanges, type EntityChangeDetail } from "./shared";

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

type MenuItem = Required<MenuProps>["items"][number];

const NAV_ITEMS: MenuItem[] = [
  {
    type: "group",
    label: "运行监控",
    children: [
      { key: "/overview", icon: <DashboardOutlined />, label: "运行概览" },
      { key: "/audits", icon: <AuditOutlined />, label: "操作审计" },
    ],
  },
  {
    type: "group",
    label: "用户与 Agent",
    children: [
      { key: "/groups", icon: <TeamOutlined />, label: "群组与权限" },
      { key: "/users", icon: <UserOutlined />, label: "全局用户" },
      { key: "/agent", icon: <ApiOutlined />, label: "Agent 管理" },
    ],
  },
  {
    type: "group",
    label: "游戏与内容",
    children: [
      { key: "/games", icon: <CrownOutlined />, label: "对局中心" },
      { key: "/modules", icon: <BookOutlined />, label: "模组库" },
      { key: "/fanqie", icon: <ReadOutlined />, label: "番茄小说" },
    ],
  },
  {
    type: "group",
    label: "系统设置",
    children: [
      { key: "/environment", icon: <SettingOutlined />, label: "环境配置" },
    ],
  },
];

const BREADCRUMB_LABELS: Record<string, string> = {
  overview: "运行概览",
  audits: "操作审计",
  groups: "群组与权限",
  users: "全局用户",
  games: "对局中心",
  modules: "模组库",
  fanqie: "番茄小说",
  agent: "Agent 管理",
  environment: "环境配置",
};

function breadcrumbItems(pathname: string): { title: React.ReactNode }[] {
  const segments = pathname.split("/").filter(Boolean);
  const items: { title: React.ReactNode }[] = [{ title: <Link to="/overview">管理台</Link> }];
  if (segments.length === 0) return items;
  const root = segments[0];
  const rootLabel = BREADCRUMB_LABELS[root] ?? root;
  items.push({ title: segments.length > 1 ? <Link to={`/${root}`}>{rootLabel}</Link> : rootLabel });
  if (segments.length > 1) {
    items.push({ title: root === "agent" ? `群 ${segments[1]}` : segments[1] });
  }
  return items;
}

export function Shell({ onLogout }: { onLogout: () => void }): React.JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const [stream, setStream] = useState<"connecting" | "open" | "closed">("connecting");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [dirtyCount, setDirtyCount] = useState(0);
  const selected = `/${location.pathname.split("/")[1] || "overview"}`;
  const crumbs = useMemo(() => breadcrumbItems(location.pathname), [location.pathname]);

  useEffect(() => openStatusStream((payload) => {
    if (payload.type === "snapshot" || payload.type === "overview.updated") {
      window.dispatchEvent(new CustomEvent("yawnbot-overview", { detail: payload.data }));
    }
    if (payload.type === "entity.changed") {
      window.dispatchEvent(new CustomEvent<EntityChangeDetail>("yawnbot-entity-changed", {
        detail: payload.data as EntityChangeDetail,
      }));
    }
  }, setStream), []);

  useEffect(() => {
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<{ count: number }>).detail;
      setDirtyCount(detail?.count ?? 0);
    };
    window.addEventListener("yawnbot-dirty-state", listener);
    return () => window.removeEventListener("yawnbot-dirty-state", listener);
  }, []);

  const go = (key: string) => {
    if (!confirmDiscardChanges()) return;
    setMobileOpen(false);
    navigate(key);
  };

  const logout = async () => {
    if (!confirmDiscardChanges()) return;
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken("");
      onLogout();
    }
  };

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={[selected]}
      onClick={({ key }) => go(String(key))}
      items={NAV_ITEMS}
      className="admin-menu"
    />
  );

  return (
    <Layout className="app-layout">
      <Sider width={236} className="app-sider desktop-sider">
        <div className="brand"><RobotOutlined /><span>YawnBot</span></div>
        {menu}
      </Sider>
      <Drawer
        placement="left"
        width={280}
        open={mobileOpen}
        onClose={() => setMobileOpen(false)}
        className="mobile-nav-drawer"
        title={<div className="brand drawer-brand"><RobotOutlined /><span>YawnBot</span></div>}
      >
        {menu}
      </Drawer>
      <Layout>
        <Header className="app-header">
          <Space>
            <Button
              className="mobile-nav-trigger"
              type="text"
              icon={<MenuOutlined />}
              aria-label="打开导航"
              onClick={() => setMobileOpen(true)}
            />
            <SafetyCertificateOutlined />
            <Text>Core / Agent</Text>
            <Tag className="status-tag" color={stream === "open" ? "green" : "orange"}>
              <span className="live-dot" />
              {stream === "open" ? "实时连接" : "正在重连"}
            </Tag>
            {dirtyCount > 0 && <Tag color="gold">{dirtyCount} 处未保存</Tag>}
          </Space>
          <Button icon={<LogoutOutlined />} onClick={() => void logout()}>退出</Button>
        </Header>
        <Content className="app-content">
          <Breadcrumb className="app-breadcrumb" items={crumbs} />
          <Suspense fallback={<div className="route-loading"><Spin size="large" /></div>}>
            <Outlet />
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}
