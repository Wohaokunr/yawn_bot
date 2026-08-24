import { App as AntApp, Spin } from "antd";
import { lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./app-shell";
import { api, setCsrfToken } from "./api";
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
const WebAuditsPage = lazy(() =>
  import("./audits").then(({ WebAuditsPage: Page }) => ({ default: Page })),
);

function App(): React.JSX.Element {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);

  useEffect(() => {
    api<{ authenticated: boolean; csrfToken: string }>("/auth/session")
      .then(({ data }) => {
        setCsrfToken(data.csrfToken);
        setAuthenticated(true);
      })
      .catch(() => setAuthenticated(false));

    const lost = () => setAuthenticated(false);
    window.addEventListener("yawnbot-auth-lost", lost);
    return () => window.removeEventListener("yawnbot-auth-lost", lost);
  }, []);

  if (authenticated === null) {
    return <div className="center-screen"><Spin size="large" /></div>;
  }
  if (!authenticated) {
    return (
      <Login
        onSuccess={(csrf) => {
          setCsrfToken(csrf);
          setAuthenticated(true);
        }}
      />
    );
  }

  return (
    <AntApp>
      <Routes>
        <Route element={<Shell onLogout={() => setAuthenticated(false)} />}>
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
