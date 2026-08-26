import { App as AntApp, Spin } from "antd";
import { lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./app-shell";
import { api, setCsrfToken } from "./api";
import type { AuthSessionData } from "./auth-session";
import { GuestHome } from "./guest-home";
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
const AgentGroupsPage = lazy(() =>
  import("./agent").then(({ AgentGroupsPage: Page }) => ({ default: Page })),
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

function App(): React.JSX.Element {
  const [session, setSession] = useState<AuthSessionData | null | undefined>(undefined);

  useEffect(() => {
    api<AuthSessionData>("/auth/session")
      .then(({ data }) => {
        setCsrfToken(data.csrfToken);
        setSession(data);
      })
      .catch(() => setSession(null));

    const lost = () => {
      setCsrfToken("");
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
          setCsrfToken(nextSession.csrfToken);
          setSession(nextSession);
        }}
      />
    );
  }
  if (session.role === "guest") {
    return <GuestHome session={session} onLogout={() => setSession(null)} />;
  }

  return (
    <AntApp>
      <Routes>
        <Route element={<Shell onLogout={() => setSession(null)} />}>
          <Route index element={<Navigate to="/overview" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="groups" element={<GroupsPage />} />
          <Route path="groups/:groupId" element={<GroupDetailPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="games" element={<GamesPage />} />
          <Route path="modules" element={<ModulesPage />} />
          <Route path="fanqie" element={<FanqiePage />} />
          <Route path="agent" element={<AgentGroupsPage />} />
          <Route path="agent/:groupId" element={<AgentDetailPage />} />
          <Route path="guest-access" element={<GuestAccessPage />} />
          <Route path="environment" element={<EnvironmentPage />} />
          <Route path="audits" element={<WebAuditsPage />} />
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
