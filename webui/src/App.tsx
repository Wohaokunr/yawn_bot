import { App as AntApp, Spin } from "antd";
import { lazy, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./app-shell";
import { api, setCsrfToken } from "./api";
import type { AuthSessionData, LoginInfo } from "./auth-session";
import { Login } from "./login";
import { OverviewPage } from "./overview";

const GroupsPage = lazy(() =>
  import("./access-pages").then(({ GroupsPage: Page }) => ({ default: Page })),
);
const GroupDetailPage = lazy(() =>
  import("./access-pages").then(({ GroupDetailPage: Page }) => ({ default: Page })),
);
const UsersPage = lazy(() =>
  import("./access-pages").then(({ UsersPage: Page }) => ({ default: Page })),
);
const AgentDetailPage = lazy(() =>
  import("./agent").then(({ AgentDetailPage: Page }) => ({ default: Page })),
);
const GamesPage = lazy(() =>
  import("./games").then(({ GamesPage: Page }) => ({ default: Page })),
);
const ModulesPage = lazy(() =>
  import("./modules").then(({ ModulesPage: Page }) => ({ default: Page })),
);
const FanqiePage = lazy(() =>
  import("./fanqie").then(({ FanqiePage: Page }) => ({ default: Page })),
);
const EnvironmentPage = lazy(() =>
  import("./environment").then(({ EnvironmentPage: Page }) => ({ default: Page })),
);
const GuestAccessPage = lazy(() =>
  import("./guest-access").then(({ GuestAccessPage: Page }) => ({ default: Page })),
);
const WebAuditsPage = lazy(() =>
  import("./audits").then(({ WebAuditsPage: Page }) => ({ default: Page })),
);
const SchedulesPage = lazy(() =>
  import("./schedules/SchedulesPage").then(({ SchedulesPage: Page }) => ({ default: Page })),
);
const PrivateAgentPage = lazy(() =>
  import("./private-agent/PrivateAgentPage").then(({ PrivateAgentPage: Page }) => ({ default: Page })),
);
const SandboxPage = lazy(() =>
  import("./sandbox/SandboxPage").then(({ SandboxPage: Page }) => ({ default: Page })),
);
const WebSearchPage = lazy(() =>
  import("./web-search/WebSearchPage").then(({ WebSearchPage: Page }) => ({ default: Page })),
);
const StickersPage = lazy(() =>
  import("./stickers/StickersPage").then(({ StickersPage: Page }) => ({ default: Page })),
);
const GuestGroupsPage = lazy(() =>
  import("./guest-home").then(({ GuestGroupsPage: Page }) => ({ default: Page })),
);
const MySchedulesPage = lazy(() =>
  import("./me/MySchedulesPage").then(({ MySchedulesPage: Page }) => ({ default: Page })),
);
const MeHomePage = lazy(() =>
  import("./me/MeHomePage").then(({ MeHomePage: Page }) => ({ default: Page })),
);
const MyGroupPage = lazy(() =>
  import("./me/MyGroupPage").then(({ MyGroupPage: Page }) => ({ default: Page })),
);
const MyPersonaPage = lazy(() =>
  import("./me/MyPersonaPage").then(({ MyPersonaPage: Page }) => ({ default: Page })),
);
const MyAccountPage = lazy(() =>
  import("./me/MyAccountPage").then(({ MyAccountPage: Page }) => ({ default: Page })),
);
const MyAssistantPage = lazy(() =>
  import("./me/MyAssistantPage").then(({ MyAssistantPage: Page }) => ({ default: Page })),
);
const MyChatSettingsPage = lazy(() =>
  import("./me/MyChatSettingsPage").then(({ MyChatSettingsPage: Page }) => ({ default: Page })),
);
const MyMemoryPage = lazy(() =>
  import("./me/MyMemoryPage").then(({ MyMemoryPage: Page }) => ({ default: Page })),
);
const MyChatsPage = lazy(() =>
  import("./me/MyChatsPage").then(({ MyChatsPage: Page }) => ({ default: Page })),
);
const MyAffinityPage = lazy(() =>
  import("./me/MyAffinityPage").then(({ MyAffinityPage: Page }) => ({ default: Page })),
);
const MyTasksPage = lazy(() =>
  import("./me/MyTasksPage").then(({ MyTasksPage: Page }) => ({ default: Page })),
);
const MyGroupsPage = lazy(() =>
  import("./me/MyGroupsPage").then(({ MyGroupsPage: Page }) => ({ default: Page })),
);
const MyGamesPage = lazy(() =>
  import("./me/MyGamesPage").then(({ MyGamesPage: Page }) => ({ default: Page })),
);
const GuestGroupPage = lazy(() =>
  import("./guest-home").then(({ GuestGroupPage: Page }) => ({ default: Page })),
);

/** 已登录时每隔多久读一次会话：后端在剩余有效期不到一半时顺延，打开着的页面不会突然掉线。 */
export const KEEPALIVE_MS = 10 * 60 * 1000;

function keepSessionAlive(): void {
  api<AuthSessionData>("/auth/session")
    .then(({ data }) => setCsrfToken(data.csrfToken, data.role))
    // 401 已由 api() 广播 yawnbot-auth-lost；网络抖动等下一次再试
    .catch(() => undefined);
}

/** QQ 账号刚登录时提示一次“上次登录 / 这次是新地点”。 */
export function loginNoticeText(info: LoginInfo): { title: string; description: string; warn: boolean } | null {
  const previous = info.previous;
  if (info.newLocation) {
    return {
      title: "这次是在新的地点登录",
      description: `${info.device} · ${info.location}。Bot 会私聊提醒你；当前在线 ${info.activeCount} 台设备，可在「账号与数据」里查看和下线。`,
      warn: true,
    };
  }
  if (!previous) return null;
  const when = previous.at ? previous.at.slice(0, 16).replace("T", " ") : "未知时间";
  return {
    title: "欢迎回来",
    description: `上次登录：${when} · ${previous.device} · ${previous.location}。当前在线 ${info.activeCount} 台设备。`,
    warn: false,
  };
}

function LoginNotice({ info }: { info?: LoginInfo }): null {
  const { notification } = AntApp.useApp();
  const shown = useRef(false);
  useEffect(() => {
    if (!info || shown.current) return;
    shown.current = true;
    const text = loginNoticeText(info);
    if (!text) return;
    (text.warn ? notification.warning : notification.info)({ message: text.title, description: text.description, placement: "topRight", duration: 8 });
  }, [info, notification]);
  return null;
}

function App(): React.JSX.Element {
  const [session, setSession] = useState<AuthSessionData | null | undefined>(undefined);
  const [notice, setNotice] = useState<string | undefined>(undefined);

  useEffect(() => {
    api<AuthSessionData>("/auth/session")
      .then(({ data }) => {
        setCsrfToken(data.csrfToken, data.role);
        setSession(data);
      })
      .catch(() => setSession(null));

    const lost = () => {
      setCsrfToken("", null);
      setSession((previous) => {
        // 用着用着掉线（被下线 / 过期 / 改了密码）才提示；首次打开没登录不提示
        if (previous) setNotice("登录已失效：可能已过期、在别的设备上被下线或改了密码，请重新登录。");
        return null;
      });
    };
    window.addEventListener("yawnbot-auth-lost", lost);
    return () => window.removeEventListener("yawnbot-auth-lost", lost);
  }, []);

  const loggedIn = Boolean(session);
  useEffect(() => {
    if (!loggedIn) return undefined;
    const timer = window.setInterval(keepSessionAlive, KEEPALIVE_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") keepSessionAlive();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [loggedIn]);

  if (session === undefined) {
    return <div className="center-screen"><Spin size="large" /></div>;
  }
  if (session === null) {
    return (
      <Login
        notice={notice}
        onSuccess={(nextSession) => {
          setCsrfToken(nextSession.csrfToken, nextSession.role);
          setNotice(undefined);
          setSession(nextSession);
        }}
      />
    );
  }
  if (session.role === "user") {
    // QQ 个人账号：只有“我的空间”，其余路径一律回到 /me（后端同样拒绝越权接口）
    const logout = () => setSession(null);
    return (
      <AntApp>
        <LoginNotice info={session.login} />
        <Routes>
          <Route element={<Shell session={session} onLogout={logout} />}>
            <Route index element={<Navigate to="/me" replace />} />
            <Route path="me" element={<MeHomePage />} />
            <Route path="me/groups" element={<MyGroupsPage />} />
            <Route path="me/groups/:groupId" element={<MyGroupPage />} />
            <Route path="me/assistant" element={<MyAssistantPage />} />
            <Route path="me/tasks" element={<MyTasksPage />} />
            <Route path="me/chat" element={<MyChatSettingsPage />} />
            <Route path="me/memory" element={<MyMemoryPage />} />
            <Route path="me/chats" element={<MyChatsPage />} />
            <Route path="me/affinity" element={<MyAffinityPage />} />
            <Route path="me/games" element={<MyGamesPage />} />
            <Route path="me/persona" element={<MyPersonaPage />} />
            <Route path="me/schedules" element={<MySchedulesPage />} />
            <Route path="me/account" element={<MyAccountPage onLoggedOut={logout} />} />
          </Route>
          <Route path="*" element={<Navigate to="/me" replace />} />
        </Routes>
      </AntApp>
    );
  }
  if (session.role === "guest") {
    return (
      <AntApp>
        <Routes>
          <Route element={<Shell session={session} onLogout={() => setSession(null)} />}>
            <Route index element={<Navigate to="/guest" replace />} />
            <Route path="guest" element={<GuestGroupsPage />} />
            <Route path="guest/:groupId" element={<GuestGroupPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/guest" replace />} />
        </Routes>
      </AntApp>
    );
  }

  return (
    <AntApp>
      <Routes>
        <Route element={<Shell session={session} onLogout={() => setSession(null)} />}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="groups" element={<GroupsPage />} />
          <Route path="groups/:groupId" element={<GroupDetailPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="games" element={<GamesPage />} />
          <Route path="modules" element={<ModulesPage />} />
          <Route path="fanqie" element={<FanqiePage />} />
          <Route path="agent" element={<Navigate to="/groups" replace />} />
          <Route path="agent/:groupId" element={<AgentDetailPage />} />
          <Route path="guest-access" element={<GuestAccessPage />} />
          <Route path="environment" element={<EnvironmentPage />} />
          <Route path="audits" element={<WebAuditsPage />} />
          <Route path="sandbox" element={<SandboxPage />} />
          <Route path="web-search" element={<WebSearchPage />} />
          <Route path="stickers" element={<StickersPage />} />
          <Route path="schedules" element={<SchedulesPage />} />
          <Route path="private-agent" element={<PrivateAgentPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </AntApp>
  );
}

export {
  AI_OUTCOME_META,
  aiOutcomeMeta,
  fanqieSummary,
  formatLatency,
  formatRate,
  formatUptime,
} from "./overview";

export default App;
