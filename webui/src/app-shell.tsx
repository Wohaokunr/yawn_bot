import {
  EyeOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuOutlined,
  MenuUnfoldOutlined,
  RobotOutlined,
  SearchOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Breadcrumb, Button, Drawer, Layout, Menu, Spin, Tag, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, openStatusStream, setCsrfToken } from "./api";
import type { AuthSessionData } from "./auth-session";
import { CommandPalette } from "./command-palette";
import { ScrollRegion } from "./layout";
import { ADMIN_NAV, GUEST_NAV, homePathForRole, type NavEntry, navForRole, selectedNavKey, USER_NAV } from "./nav-config";
import { confirmDiscardChanges, type EntityChangeDetail } from "./shared";

const { Header, Sider, Content } = Layout;
type MenuItem = Required<MenuProps>["items"][number];

const SIDER_COLLAPSED_KEY = "yawnbot.sider.collapsed";

function menuItems(entries: NavEntry[]): MenuItem[] {
  const groups = new Map<string, NavEntry[]>();
  for (const entry of entries) groups.set(entry.group, [...(groups.get(entry.group) ?? []), entry]);
  return [...groups.entries()].map(([label, children]) => ({
    type: "group" as const,
    label,
    children: children.map((entry) => ({ key: entry.key, icon: entry.icon, label: entry.label })),
  }));
}

const ADMIN_NAV_ITEMS = menuItems(ADMIN_NAV);
const GUEST_NAV_ITEMS = menuItems(GUEST_NAV);
const USER_NAV_ITEMS = menuItems(USER_NAV);

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDER_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeCollapsed(value: boolean): void {
  try {
    window.localStorage.setItem(SIDER_COLLAPSED_KEY, value ? "1" : "0");
  } catch {
    // 隐私模式等场景下无法持久化，忽略即可
  }
}

const BREADCRUMB_LABELS: Record<string, string> = {
  overview: "运行概览",
  sandbox: "代码沙箱",
  "web-search": "网页搜索",
  stickers: "表情包",
  schedules: "定时任务",
  audits: "操作审计",
  groups: "群组",
  users: "用户",
  games: "对局中心",
  modules: "模组库",
  fanqie: "番茄小说",
  agent: "群组",
  "guest-access": "访客访问",
  environment: "环境配置",
  guest: "可查看群聊",
  me: "我的空间",
};

const USER_BREADCRUMB_LABELS: Record<string, string> = {
  groups: "我管理的群",
  persona: "私聊人设",
  account: "账号安全",
};

function breadcrumbItems(pathname: string, role: AuthSessionData["role"]): { title: React.ReactNode }[] {
  const segments = pathname.split("/").filter(Boolean);
  const rootPath = homePathForRole(role);
  const rootTitle = role === "guest" ? "访客视图" : role === "user" ? "我的空间" : "管理台";
  if (role === "user") {
    const items: { title: React.ReactNode }[] = [{ title: <Link to={rootPath}>{rootTitle}</Link> }];
    const sub = segments[1];
    if (sub && USER_BREADCRUMB_LABELS[sub]) {
      items.push({ title: USER_BREADCRUMB_LABELS[sub] });
      if (sub === "groups" && segments[2]) items.push({ title: `群 ${segments[2]}` });
    }
    return items;
  }
  const items: { title: React.ReactNode }[] = [{ title: <Link to={rootPath}>{rootTitle}</Link> }];
  if (segments.length === 0) return items;
  const root = segments[0];
  const rootLabel = BREADCRUMB_LABELS[root] ?? root;
  const rootPathForCrumb = root === "agent" ? "/groups" : `/${root}`;
  if (root !== "overview" || role === "guest") {
    items.push({ title: segments.length > 1 ? <Link to={rootPathForCrumb}>{rootLabel}</Link> : rootLabel });
  }
  if (segments.length > 1) items.push({ title: `群 ${segments[1]}` });
  if (segments.length > 1 && (root === "agent" || root === "groups")) {
    items.push({ title: root === "agent" ? "Agent 工作台" : "成员与权限" });
  }
  return items;
}

export function Shell({
  session,
  onLogout,
}: {
  session: AuthSessionData;
  onLogout: () => void;
}): React.JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const isGuest = session.role === "guest";
  const isUser = session.role === "user";
  // 访客和个人账号都没有管理员实时流、群组搜索与“未保存”跟踪
  const limited = isGuest || isUser;
  const canUseAdminStream = !limited && session.capabilities.realtimeAdminStream;
  const [stream, setStream] = useState<"connecting" | "open" | "closed">(
    canUseAdminStream ? "connecting" : "closed",
  );
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [dirtyCount, setDirtyCount] = useState(0);
  const scrollPositions = useRef(new Map<string, number>());
  const selected = selectedNavKey(location.pathname, session.role);
  const crumbs = useMemo(
    () => breadcrumbItems(location.pathname, session.role),
    [location.pathname, session.role],
  );
  const scrollKey = useMemo(() => {
    const tab = new URLSearchParams(location.search).get("tab") ?? "";
    return `${location.pathname}?tab=${tab}`;
  }, [location.pathname, location.search]);
  const navItems = isGuest ? GUEST_NAV_ITEMS : isUser ? USER_NAV_ITEMS : ADMIN_NAV_ITEMS;
  const navEntries: NavEntry[] = navForRole(session.role);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((current) => {
      writeCollapsed(!current);
      return !current;
    });
  }, []);

  // Ctrl/⌘ + K 全局唤起命令面板
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!canUseAdminStream) {
      setStream("closed");
      return undefined;
    }
    return openStatusStream((payload) => {
      if (payload.type === "snapshot" || payload.type === "overview.updated") {
        window.dispatchEvent(new CustomEvent("yawnbot-overview", { detail: payload.data }));
      }
      if (payload.type === "entity.changed") {
        window.dispatchEvent(new CustomEvent<EntityChangeDetail>("yawnbot-entity-changed", {
          detail: payload.data as EntityChangeDetail,
        }));
      }
    }, setStream);
  }, [canUseAdminStream]);

  useEffect(() => {
    if (isGuest) return undefined;
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<{ count: number }>).detail;
      setDirtyCount(detail?.count ?? 0);
    };
    window.addEventListener("yawnbot-dirty-state", listener);
    return () => window.removeEventListener("yawnbot-dirty-state", listener);
  }, [isGuest]);

  useEffect(() => {
    const region = document.querySelector<HTMLElement>(".app-content-scroll");
    if (!region) return undefined;
    const restore = () => {
      region.scrollTop = scrollPositions.current.get(scrollKey) ?? 0;
    };
    const useAnimationFrame = typeof window.requestAnimationFrame === "function";
    const ticket = useAnimationFrame
      ? window.requestAnimationFrame(restore)
      : window.setTimeout(restore, 0);
    return () => {
      if (useAnimationFrame) window.cancelAnimationFrame(ticket);
      else window.clearTimeout(ticket);
      scrollPositions.current.set(scrollKey, region.scrollTop);
    };
  }, [scrollKey]);

  const canLeave = () => isGuest || confirmDiscardChanges();

  const go = (key: string) => {
    if (!canLeave()) return;
    setMobileOpen(false);
    navigate(key);
  };

  const logout = async () => {
    if (!canLeave()) return;
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setCsrfToken("", null);
      onLogout();
    }
  };

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={[selected]}
      onClick={({ key }) => go(String(key))}
      items={navItems}
      className="admin-menu"
    />
  );

  const mascot = (
    <div className="sider-mascot" aria-hidden="true">
      <span className="sider-mascot-face">=ω=</span>
      <span className="sider-mascot-text">
        <strong>{isGuest ? "只读巡视中" : isUser ? "欢迎回来" : "今日也在值班"}</strong>
        <span>{isGuest ? "访客视图" : isUser ? `QQ ${session.userId ?? ""}` : stream === "open" ? "实时连接已就绪" : "等待实时连接"}</span>
      </span>
    </div>
  );

  return (
    <Layout className="app-layout">
      <Sider
        width={236}
        collapsedWidth={72}
        collapsed={collapsed}
        trigger={null}
        className={`app-sider desktop-sider${collapsed ? " is-collapsed" : ""}`}
      >
        <div className="brand"><RobotOutlined /><span>YawnBot</span></div>
        {menu}
        {!collapsed && mascot}
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
      <Layout className="app-main-layout">
        <Header className="app-header">
          <div className="app-header-left">
            <Button
              className="mobile-nav-trigger"
              type="text"
              icon={<MenuOutlined />}
              aria-label="打开导航"
              onClick={() => setMobileOpen(true)}
            />
            <Tooltip title={collapsed ? "展开侧栏" : "收起侧栏"}>
              <Button
                className="sider-toggle"
                type="text"
                icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
                onClick={toggleCollapsed}
              />
            </Tooltip>
            <Breadcrumb className="app-breadcrumb" items={crumbs} />
          </div>
          <div className="app-header-right">
            <button
              type="button"
              className="palette-trigger"
              onClick={() => setPaletteOpen(true)}
              aria-label="搜索页面或群组"
            >
              <SearchOutlined />
              <span className="palette-trigger-text">{limited ? "搜索页面" : "搜索页面或群组"}</span>
              <kbd className="palette-kbd">Ctrl K</kbd>
            </button>
            {isGuest ? (
              <Tag color="blue" icon={<EyeOutlined />}>访客 · 只读</Tag>
            ) : isUser ? (
              <Tag className="me-header-tag" icon={<UserOutlined />}>QQ {session.userId}</Tag>
            ) : (
              <Tag className="status-tag" color={stream === "open" ? "green" : "orange"}>
                <span className="live-dot" />
                {stream === "open" ? "实时连接" : "正在重连"}
              </Tag>
            )}
            {!limited && dirtyCount > 0 && <Tag color="gold">{dirtyCount} 处未保存</Tag>}
            <Tooltip title={isGuest ? "退出访客" : "退出登录"}>
              <Button
                className="logout-button"
                icon={<LogoutOutlined />}
                aria-label={isGuest ? "退出访客" : "退出"}
                onClick={() => void logout()}
              >
                <span className="logout-text">{isGuest ? "退出访客" : "退出"}</span>
              </Button>
            </Tooltip>
          </div>
        </Header>
        <Content className="app-content-shell">
          <ScrollRegion className="app-content-scroll">
            <main className="app-content">
              <Suspense fallback={<div className="route-loading"><Spin size="large" /></div>}>
                <Outlet />
              </Suspense>
            </main>
          </ScrollRegion>
        </Content>
      </Layout>
      <CommandPalette
        open={paletteOpen}
        entries={navEntries}
        searchGroups={!limited}
        onClose={() => setPaletteOpen(false)}
        onNavigate={go}
      />
    </Layout>
  );
}
