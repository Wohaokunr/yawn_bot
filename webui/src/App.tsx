import { App as AntApp, Spin } from "antd";
import { lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./app-shell";
import { api, setCsrfToken } from "./api";
import type { AuthSessionData } from "./auth-session";
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
const GuestGroupPage = lazy(() =>
  import("./guest-home").then(({ GuestGroupPage: Page }) => ({ default: Page })),
);

function App(): React.JSX.Element {
  const [session, setSession] = useState<AuthSessionData | null | undefined>(undefined);

  useEffect(() => {
    api<AuthSessionData>("/auth/session")
      .then(({ data }) => {
        setCsrfToken(data.csrfToken, data.role);
        setSession(data);
      })
      .catch(() => setSession(null));

    const lost = () => {
      setCsrfToken("", null);
      setSession(null);
    };
    window.addEventListener("yawnbot-auth-lost", lost);
    return () => window.removeEventListener("yawnbot-auth-lost", lost);
  }, []);

  if (session === undefined) {
    return <div className="center-screen"><Spin size="large" /></div>;
  }
  if (session === null) {
    return (
      <Login
        onSuccess={(nextSession) => {
          setCsrfToken(nextSession.csrfToken, nextSession.role);
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
        <Routes>
          <Route element={<Shell session={session} onLogout={logout} />}>
            <Route index element={<Navigate to="/me" replace />} />
            <Route path="me" element={<MeHomePage />} />
            <Route path="me/groups/:groupId" element={<MyGroupPage />} />
            <Route path="me/persona" element={<MyPersonaPage />} />
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
