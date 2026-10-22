import {
  AuditOutlined,
  BookOutlined,
  BugOutlined,
  DashboardOutlined,
  IdcardOutlined,
  LockOutlined,
  MessageOutlined,
  SettingOutlined,
  ShareAltOutlined,
  SmileOutlined,
} from "@ant-design/icons";
import { Spin } from "antd";
import { lazy, Suspense, useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "./api";
import { AgentCockpit } from "./agent-workspace/AgentCockpit";
import { AgentStatusBar } from "./agent-workspace/AgentStatusBar";
import { AGENT_NAV, AGENT_NAV_ORDER, AGENT_TAB_KEYS } from "./agent-workspace/nav";
import { useVisiblePolling } from "./agent-workspace/polling";
import { useAgentTabShortcuts } from "./agent-workspace/shortcuts";
import { GroupWorkspaceSwitch } from "./group-workspace";
import { PageHeader, useApiQuery } from "./shared";
import type { AgentDiagnostics } from "./types";

const LazyAgentConfigPanel = lazy(() =>
  import("./agent-panels/AgentConfigPanel").then(({ AgentConfigPanel }) => ({ default: AgentConfigPanel })),
);
const LazyPersonaPanel = lazy(() =>
  import("./agent-panels/PersonaPanel").then(({ PersonaPanel }) => ({ default: PersonaPanel })),
);
const LazyMemoriesPanel = lazy(() =>
  import("./agent-panels/MemoriesPanel").then(({ MemoriesPanel }) => ({ default: MemoriesPanel })),
);
const LazyMemberProfilesPanel = lazy(() =>
  import("./agent-panels/MemberProfilesPanel").then(({ MemberProfilesPanel }) => ({ default: MemberProfilesPanel })),
);
const LazyRelationsPanel = lazy(() =>
  import("./agent-panels/RelationsPanel").then(({ RelationsPanel }) => ({ default: RelationsPanel })),
);
const LazyAgentMessagesPanel = lazy(() =>
  import("./agent-panels/AgentMessagesPanel").then(({ AgentMessagesPanel }) => ({ default: AgentMessagesPanel })),
);
const LazyAgentDebugger = lazy(() =>
  import("./agent-debug/AgentDebugger").then(({ AgentDebugger }) => ({ default: AgentDebugger })),
);
const LazyPrivacyPanel = lazy(() =>
  import("./agent-panels/PrivacyPanel").then(({ PrivacyPanel }) => ({ default: PrivacyPanel })),
);
const LazyAgentAuditsPanel = lazy(() =>
  import("./agent-panels/AgentAuditsPanel").then(({ AgentAuditsPanel }) => ({ default: AgentAuditsPanel })),
);

const NAV_ICONS: Record<string, React.ReactNode> = {
  overview: <DashboardOutlined />,
  config: <SettingOutlined />,
  persona: <SmileOutlined />,
  memories: <BookOutlined />,
  profiles: <IdcardOutlined />,
  relations: <ShareAltOutlined />,
  messages: <MessageOutlined />,
  debug: <BugOutlined />,
  privacy: <LockOutlined />,
  audit: <AuditOutlined />,
};

// 诊断数据每 30 秒在页面可见时刷新一次，状态条与驾驶舱共用这一份。
const DIAGNOSTICS_POLL_MS = 30_000;
const AUTO_REFRESH_KEY = "yawnbot.agent.autoRefresh";

function readAutoRefresh(): boolean {
  try {
    return window.localStorage.getItem(AUTO_REFRESH_KEY) !== "off";
  } catch {
    return true;
  }
}

function writeAutoRefresh(value: boolean): void {
  try {
    window.localStorage.setItem(AUTO_REFRESH_KEY, value ? "on" : "off");
  } catch {
    // 隐私模式等场景写不进去就只在本次会话生效。
  }
}

interface AgentGroupDetail {
  groupId: string;
  groupName?: string | null;
  memberCount: number;
}

function AgentPanelFallback(): React.JSX.Element {
  return <div style={{ display: "grid", minHeight: 180, placeItems: "center" }}><Spin /></div>;
}

function panel(content: React.ReactNode): React.JSX.Element {
  return <Suspense fallback={<AgentPanelFallback />}>{content}</Suspense>;
}

function shortcutDigit(key: string): string {
  const index = AGENT_NAV_ORDER.indexOf(key);
  return index === 9 ? "0" : String(index + 1);
}

function navBadge(key: string, diagnostics: AgentDiagnostics | null): "error" | "warning" | null {
  if (!diagnostics) return null;
  if (key === "overview") {
    if (diagnostics.blockers.some((item) => item.severity === "error")) return "error";
    if (diagnostics.blockers.some((item) => item.severity === "warning")) return "warning";
  }
  if (key === "memories" && (diagnostics.memory.rebuildRequired || diagnostics.memory.consecutiveFailures > 0)) {
    return "warning";
  }
  return null;
}

export function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") ?? "overview";
  const tab = AGENT_TAB_KEYS.has(requestedTab) ? requestedTab : "overview";
  // 访问过的面板保持挂载，切走再回来时草稿、调试基准等本地状态都还在。
  const [visited, setVisited] = useState<Set<string>>(() => new Set([tab]));
  const groupQuery = useApiQuery({
    queryKey: ["agent-group-detail", groupId],
    fetcher: (signal) => api<AgentGroupDetail>(`/groups/${groupId}`, { signal }).then((response) => response.data),
    invalidation: {
      resources: ["agent_group_data", "agent_config"],
      scope: { groupId },
    },
  });
  const diagnosticsQuery = useApiQuery({
    queryKey: ["agent-diagnostics", groupId],
    fetcher: (signal) => api<AgentDiagnostics>(
      `/agent/groups/${groupId}/diagnostics`,
      { signal },
    ).then((response) => response.data),
    invalidation: {
      resources: ["agent_config", "agent_memory", "agent_group_data"],
      scope: { groupId },
    },
  });
  const [autoRefresh, setAutoRefresh] = useState(readAutoRefresh);
  useVisiblePolling(diagnosticsQuery.reload, autoRefresh ? DIAGNOSTICS_POLL_MS : null);

  useEffect(() => {
    if (requestedTab === tab) return;
    const next = new URLSearchParams(searchParams);
    next.delete("tab");
    setSearchParams(next, { replace: true });
  }, [requestedTab, searchParams, setSearchParams, tab]);

  useEffect(() => {
    setVisited((current) => current.has(tab) ? current : new Set(current).add(tab));
  }, [tab]);

  const navigate = (key: string, extra?: Record<string, string>) => {
    if (!AGENT_TAB_KEYS.has(key)) return;
    const next = new URLSearchParams(searchParams);
    if (key === "overview") next.delete("tab"); else next.set("tab", key);
    for (const [name, value] of Object.entries(extra ?? {})) next.set(name, value);
    setSearchParams(next, { replace: true });
  };

  useAgentTabShortcuts((index) => {
    const key = AGENT_NAV_ORDER[index];
    if (key) navigate(key);
  });

  const groupName = groupQuery.data?.groupName?.trim() || "未命名群";
  const title = groupQuery.data ? `Agent · ${groupName}（${groupId}）` : `Agent · ${groupId}`;
  const diagnostics = diagnosticsQuery.data;

  const panels: Record<string, () => React.ReactNode> = {
    overview: () => <AgentCockpit
      groupId={groupId}
      groupLabel={`${groupName}（${groupId}）`}
      diagnosticsQuery={diagnosticsQuery}
      autoRefresh={autoRefresh}
      onAutoRefreshChange={(value) => {
        setAutoRefresh(value);
        writeAutoRefresh(value);
      }}
      onNavigate={navigate}
    />,
    config: () => panel(<LazyAgentConfigPanel groupId={groupId} />),
    persona: () => panel(<LazyPersonaPanel groupId={groupId} />),
    memories: () => panel(<LazyMemoriesPanel groupId={groupId} />),
    profiles: () => panel(<LazyMemberProfilesPanel groupId={groupId} />),
    relations: () => panel(<LazyRelationsPanel groupId={groupId} />),
    messages: () => panel(<LazyAgentMessagesPanel groupId={groupId} />),
    debug: () => panel(<LazyAgentDebugger groupId={groupId} />),
    privacy: () => panel(<LazyPrivacyPanel groupId={groupId} />),
    audit: () => panel(<LazyAgentAuditsPanel groupId={groupId} />),
  };

  return <>
    <PageHeader
      title={title}
      subtitle="群级运行状态、知识、调试与数据治理"
      extra={<GroupWorkspaceSwitch groupId={groupId} active="agent" />}
    />
    <AgentStatusBar groupId={groupId} diagnosticsQuery={diagnosticsQuery} onNavigate={navigate} />
    <div className="agent-ws">
      <nav className="agent-ws-nav" aria-label="Agent 工作台导航">
        <div role="tablist" aria-orientation="vertical" className="agent-ws-nav-list">
          {AGENT_NAV.map((section) => (
            <div key={section.key} className="agent-ws-nav-group">
              <div className="agent-ws-nav-heading">{section.label}</div>
              {section.items.map((item) => {
                const badge = navBadge(item.key, diagnostics);
                const active = item.key === tab;
                return (
                  <button
                    key={item.key}
                    type="button"
                    role="tab"
                    id={`agent-ws-tab-${item.key}`}
                    aria-selected={active}
                    aria-controls={`agent-ws-panel-${item.key}`}
                    className={`agent-ws-nav-item${active ? " is-active" : ""}`}
                    title={`${item.hint}（Alt+${shortcutDigit(item.key)}）`}
                    onClick={() => navigate(item.key)}
                  >
                    <span className="agent-ws-nav-icon" aria-hidden="true">{NAV_ICONS[item.key]}</span>
                    <span className="agent-ws-nav-label">{item.label}</span>
                    {badge && <span className={`agent-ws-nav-badge is-${badge}`} aria-label={badge === "error" ? "有阻塞" : "需要关注"} />}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
        <div className="agent-ws-nav-tip">Alt + 数字键快速切换</div>
      </nav>
      <div className="agent-ws-content">
        {[...visited].filter((key) => key in panels).map((key) => (
          <section
            key={key}
            role="tabpanel"
            id={`agent-ws-panel-${key}`}
            aria-labelledby={`agent-ws-tab-${key}`}
            hidden={key !== tab}
            className="agent-ws-panel"
          >
            {panels[key]()}
          </section>
        ))}
      </div>
    </div>
  </>;
}
