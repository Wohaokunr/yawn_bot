import {
  ApiOutlined,
  AuditOutlined,
  BookOutlined,
  CrownOutlined,
  DashboardOutlined,
  LogoutOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Flex,
  Form,
  Input,
  Layout,
  Menu,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { AgentAuditTable, AgentDetailPage, AgentGroupsPage } from "./agent";
import { api, ApiError, openStatusStream, setCsrfToken } from "./api";
import { FanqiePage } from "./fanqie";
import { GamesPage } from "./games";
import { formatTime, PageHeader, QueryErrorAlert, TablePagination, useApiQuery } from "./shared";
import type {
  FeatureState,
  GroupSummary,
  Member,
  Overview,
  UserSummary,
  WebAudit,
} from "./types";

const { Header, Sider, Content } = Layout;
const { Title, Text, Paragraph } = Typography;

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

  if (authenticated === null) return <div className="center-screen"><Spin size="large" /></div>;
  if (!authenticated) {
    return <Login onSuccess={(csrf) => { setCsrfToken(csrf); setAuthenticated(true); }} />;
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
          <Route path="fanqie" element={<FanqiePage />} />
          <Route path="agent" element={<AgentGroupsPage />} />
          <Route path="agent/:groupId" element={<AgentDetailPage />} />
          <Route path="audits" element={<WebAuditsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Routes>
    </AntApp>
  );
}

function Login({ onSuccess }: { onSuccess: (csrf: string) => void }): React.JSX.Element {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submit = async ({ token }: { token: string }) => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api<{ authenticated: boolean; csrfToken: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      onSuccess(data.csrfToken);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="login-page">
      <div className="petals" aria-hidden="true">{[0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} />)}</div>
      <Card className="login-card" variant="borderless">
        <div className="brand-mark"><RobotOutlined /></div>
        <Title level={2}>YawnBot 管理台</Title>
        <Paragraph type="secondary">使用部署时配置的运维 Token 登录</Paragraph>
        {error && <Alert type="error" message={error} showIcon closable onClose={() => setError("")} />}
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item name="token" label="运维 Token" rules={[{ required: true, message: "请输入运维 Token" }]}>
            <Input.Password autoFocus autoComplete="current-password" size="large" />
          </Form.Item>
          <Button type="primary" htmlType="submit" size="large" block loading={loading}>登录</Button>
        </Form>
      </Card>
    </div>
  );
}

function Shell({ onLogout }: { onLogout: () => void }): React.JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = AntApp.useApp();
  const [stream, setStream] = useState<"connecting" | "open" | "closed">("connecting");
  const selected = `/${location.pathname.split("/")[1] || "overview"}`;
  useEffect(() => openStatusStream((payload) => {
    if (payload.type === "snapshot" || payload.type === "overview.updated") {
      window.dispatchEvent(new CustomEvent("yawnbot-overview", { detail: payload.data }));
    }
    if (payload.type === "entity.changed") {
      window.dispatchEvent(new Event("yawnbot-entity-changed"));
    }
  }, setStream), []);
  const logout = async () => {
    try { await api("/auth/logout", { method: "POST" }); } finally { setCsrfToken(""); onLogout(); }
  };
  return (
    <Layout className="app-layout">
      <Sider breakpoint="lg" collapsedWidth="0" className="app-sider">
        <div className="brand"><RobotOutlined /><span>YawnBot</span></div>
        <Menu
          mode="inline"
          selectedKeys={[selected]}
          onClick={({ key }) => navigate(key)}
          items={[
            { key: "/overview", icon: <DashboardOutlined />, label: "运行概览" },
            { key: "/groups", icon: <TeamOutlined />, label: "群组与权限" },
            { key: "/users", icon: <UserOutlined />, label: "全局用户" },
            { key: "/games", icon: <CrownOutlined />, label: "对局中心" },
            { key: "/fanqie", icon: <BookOutlined />, label: "番茄小说" },
            { key: "/agent", icon: <ApiOutlined />, label: "Agent 管理" },
            { key: "/audits", icon: <AuditOutlined />, label: "操作审计" },
          ]}
        />
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space><SafetyCertificateOutlined /><Text>Core / Agent</Text><Tag className="status-tag" color={stream === "open" ? "green" : "orange"}><span className="live-dot" />{stream === "open" ? "实时连接" : "正在重连"}</Tag></Space>
          <Button icon={<LogoutOutlined />} onClick={logout}>退出</Button>
        </Header>
        <Content className="app-content"><Outlet /></Content>
      </Layout>
    </Layout>
  );
}

function StatCard({ icon, tone, title, value, suffix }: { icon: React.ReactNode; tone: "sakura" | "mint" | "sky" | "lavender"; title: string; value: number | string; suffix?: string }): React.JSX.Element {
  return (
    <Card className="stat-card">
      <Flex align="center" gap={16}>
        <span className={`stat-badge tone-${tone}`}>{icon}</span>
        <Statistic title={title} value={value} suffix={suffix} />
      </Flex>
    </Card>
  );
}

function OverviewPage(): React.JSX.Element {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api<Overview>("/overview").then((r) => setData(r.data)).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const update = (event: Event) => setData((event as CustomEvent<Overview>).detail);
    window.addEventListener("yawnbot-overview", update);
    return () => window.removeEventListener("yawnbot-overview", update);
  }, []);
  if (!data) return error ? <Alert type="error" message={error} /> : <Spin />;
  const counters = data.metrics.counters.length;
  const histograms = data.metrics.histograms.length;
  return <>
    <PageHeader title="运行概览" subtitle={`快照更新于 ${formatTime(data.generatedAt)}`} />
    <Row gutter={[16, 16]}>
      <Col xs={24} sm={12} xl={6}><StatCard tone="sakura" icon={<RobotOutlined />} title="在线 Bot" value={data.bots.length} suffix="个" /></Col>
      <Col xs={24} sm={12} xl={6}><StatCard tone="mint" icon={<TeamOutlined />} title="已知群组" value={data.counts.groups} suffix="个" /></Col>
      <Col xs={24} sm={12} xl={6}><StatCard tone="sky" icon={<UserOutlined />} title="已知用户" value={data.counts.users} suffix="人" /></Col>
      <Col xs={24} sm={12} xl={6}><StatCard tone="lavender" icon={<ApiOutlined />} title="启用 Agent" value={data.counts.enabledAgents} suffix="群" /></Col>
    </Row>
    <Row gutter={[16, 16]} className="section-row">
      <Col xs={24} lg={10}><Card title="插件状态">{data.plugins.map((plugin) => <Flex key={plugin.name} justify="space-between" className="status-line"><span>{plugin.name}</span><Tag color={plugin.state === "loaded" ? "green" : plugin.state === "failed" ? "red" : "default"}>{plugin.state}</Tag></Flex>)}</Card></Col>
      <Col xs={24} lg={14}><Card title="运行指标"><Descriptions column={2} items={[{ key: "c", label: "Counter 序列", children: counters }, { key: "h", label: "Histogram 序列", children: histograms }, { key: "bots", label: "Bot ID", children: data.bots.join(", ") || "未连接" }]} /></Card></Col>
      <Col span={24}><Card title="近期 Agent 操作"><AgentAuditTable data={data.recentAgentActions} /></Card></Col>
    </Row>
  </>;
}

function GroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const load = useCallback(() => api<GroupSummary[]>(`/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search]);
  const query = useApiQuery(load);
  const columns: ColumnsType<GroupSummary> = [
    { title: "群", render: (_, row) => <><Link to={`/groups/${row.groupId}`}>{row.groupName || "未命名群"}</Link><br /><Text type="secondary" copyable>{row.groupId}</Text></> },
    { title: "成员", dataIndex: "memberCount", width: 100 },
    { title: "Agent", dataIndex: "agentEnabled", width: 100, render: (value: boolean) => <Tag color={value ? "green" : "default"}>{value ? "开启" : "关闭"}</Tag> },
    { title: "最近活跃", dataIndex: "lastActiveAt", render: formatTime },
    { title: "操作", width: 100, render: (_, row) => <Link to={`/agent/${row.groupId}`}>Agent</Link> },
  ];
  return <><PageHeader title="群组与权限" subtitle="管理群级及成员级功能覆盖" extra={<Input.Search placeholder="搜索群名或群号" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table rowKey="groupId" loading={query.loading} columns={columns} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} />
  }</Card></>;
}

function FeatureEditor({ rows, onChange }: { rows: FeatureState[]; onChange: (key: string, value: boolean | null) => Promise<void> }): React.JSX.Element {
  const [saving, setSaving] = useState("");
  return <Table rowKey="key" pagination={false} dataSource={rows} columns={[
    { title: "功能", render: (_, row) => <><Text strong>{row.name}</Text><br /><Text type="secondary">{row.key}</Text></> },
    { title: "当前生效", render: (_, row) => <Tag color={row.effective ? "green" : "red"}>{row.effective ? "开启" : "关闭"}</Tag> },
    { title: "来源", dataIndex: "source", render: (source: string) => ({ default: "默认", group: "群设置", user: "用户覆盖", global_user: "全局用户" })[source] ?? source },
    { title: "覆盖", width: 180, render: (_, row) => <Select loading={saving === row.key} value={row.override === null ? "inherit" : row.override ? "on" : "off"} options={[{ value: "inherit", label: "继承" }, { value: "on", label: "显式开启" }, { value: "off", label: "显式关闭" }]} onChange={async (value) => { setSaving(row.key); try { await onChange(row.key, value === "inherit" ? null : value === "on"); } finally { setSaving(""); } }} /> },
  ]} />;
}

interface GroupDetailData {
  groupId: string;
  groupName?: string;
  memberCount: number;
  features: FeatureState[];
}

function GroupDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const { message } = AntApp.useApp();
  const groupLoad = useCallback(() => api<GroupDetailData>(`/groups/${groupId}`).then((r) => r.data), [groupId]);
  const groupQuery = useApiQuery(groupLoad);
  const [memberPage, setMemberPage] = useState(1);
  const [memberSearch, setMemberSearch] = useState("");
  // 成员走服务端搜索 + 分页:大群成员不止 100 人时也能翻页检索。
  const membersLoad = useCallback(() => api<Member[]>(`/groups/${groupId}/members?page=${memberPage}&pageSize=20&search=${encodeURIComponent(memberSearch)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, memberPage, memberSearch]);
  const membersQuery = useApiQuery(membersLoad);
  const [selectedMember, setSelectedMember] = useState<Member | null>(null);
  const [memberFeatures, setMemberFeatures] = useState<FeatureState[]>([]);
  const [featuresLoading, setFeaturesLoading] = useState(false);
  const openMember = async (member: Member) => {
    setSelectedMember(member);
    setFeaturesLoading(true);
    try {
      setMemberFeatures((await api<FeatureState[]>(`/groups/${groupId}/members/${member.userId}/features`)).data);
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "加载失败");
    } finally {
      setFeaturesLoading(false);
    }
  };
  const group = groupQuery.data;
  if (!group) return groupQuery.error ? <QueryErrorAlert error={groupQuery.error} onRetry={groupQuery.reload} /> : <Spin />;
  return <><PageHeader title={group.groupName || "未命名群"} subtitle={`群号 ${group.groupId} · ${group.memberCount} 名成员`} extra={<Link to="/groups">返回列表</Link>} /><Tabs items={[
    { key: "features", label: "群功能", children: <Card><FeatureEditor rows={group.features} onChange={async (feature, override) => { await api(`/groups/${groupId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); message.success("群功能已更新"); groupQuery.reload(); }} /></Card> },
    { key: "members", label: "成员", children: <Card>
      <Input.Search className="table-search" placeholder="搜索成员昵称或 QQ" allowClear onSearch={(v) => { setMemberSearch(v); setMemberPage(1); }} />
      {membersQuery.error && !membersQuery.data
        ? <QueryErrorAlert error={membersQuery.error} onRetry={membersQuery.reload} />
        : <Table rowKey="userId" loading={membersQuery.loading} dataSource={membersQuery.data?.rows ?? []} pagination={{ current: memberPage, pageSize: 20, total: membersQuery.data?.total ?? 0, showSizeChanger: false, onChange: setMemberPage }} columns={[{ title: "成员", render: (_, row: Member) => <>{row.groupNickname || row.nickname || "未知成员"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role" }, { title: "最近出现", dataIndex: "lastSeenAt", render: formatTime }, { title: "操作", render: (_, row: Member) => <Button type="link" onClick={() => openMember(row)}>功能权限</Button> }]} />}
    </Card> },
  ]} />
  <Drawer open={!!selectedMember} width={680} title={`${selectedMember?.groupNickname || selectedMember?.nickname || selectedMember?.userId} · 功能覆盖`} onClose={() => setSelectedMember(null)}>{featuresLoading ? <Spin /> : selectedMember && <FeatureEditor rows={memberFeatures} onChange={async (feature, override) => { const result = await api<FeatureState>(`/groups/${groupId}/members/${selectedMember.userId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); setMemberFeatures((current) => current.map((row) => row.key === feature ? result.data : row)); message.success("成员功能已更新"); }} />}</Drawer></>;
}

function UsersPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const load = useCallback(() => api<UserSummary[]>(`/users?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search]);
  const query = useApiQuery(load);
  const [selected, setSelected] = useState<UserSummary | null>(null); const [features, setFeatures] = useState<FeatureState[]>([]); const [featuresLoading, setFeaturesLoading] = useState(false);
  const open = async (user: UserSummary) => {
    setSelected(user); setFeaturesLoading(true);
    try { setFeatures((await api<FeatureState[]>(`/users/${user.userId}/features`)).data); }
    catch (reason) { message.error(reason instanceof Error ? reason.message : "加载失败"); }
    finally { setFeaturesLoading(false); }
  };
  return <><PageHeader title="全局用户" subtitle="管理私聊及跨群全局功能覆盖" extra={<Input.Search placeholder="搜索昵称或 QQ" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table rowKey="userId" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} columns={[{ title: "用户", render: (_, row: UserSummary) => <>{row.nickname || "未知用户"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "好感", dataIndex: "affinity" }, { title: "最近互动", dataIndex: "lastInteractionAt", render: formatTime }, { title: "操作", render: (_, row: UserSummary) => <Button type="link" onClick={() => open(row)}>全局功能</Button> }]} />
  }</Card><Drawer open={!!selected} width={680} title={`${selected?.nickname || selected?.userId} · 全局功能`} onClose={() => setSelected(null)}>{featuresLoading ? <Spin /> : selected && <FeatureEditor rows={features} onChange={async (feature, override) => { const result = await api<FeatureState>(`/users/${selected.userId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); setFeatures((current) => current.map((row) => row.key === feature ? result.data : row)); message.success("全局用户功能已更新"); }} />}</Drawer></>;
}

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

function WebAuditsPage(): React.JSX.Element {
  const [page, setPage] = useState(1); const [result, setResult] = useState("");
  const load = useCallback(() => api<WebAudit[]>(`/web-audits?page=${page}&pageSize=20&result=${result}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, result]);
  const query = useApiQuery(load);
  return <><PageHeader title="操作审计" subtitle="持久化记录 WebUI 配置修改与删除操作" extra={<Select value={result} onChange={(value) => { setResult(value); setPage(1); }} options={RESULT_OPTIONS} style={{ width: 120 }} />} /><Card>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <pre>{JSON.stringify(row.detail, null, 2)}</pre> }} columns={[{ title: "时间", dataIndex: "createdAt", render: formatTime }, { title: "动作", dataIndex: "action" }, { title: "资源", render: (_, row: WebAudit) => <>{row.resourceType}<br /><Text type="secondary">{row.resourceId || "—"}</Text></> }, { title: "会话指纹", dataIndex: "actorSession" }, { title: "结果", dataIndex: "result", render: (value: string) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> }, { title: "请求 ID", dataIndex: "requestId", ellipsis: true }]} />
  }</Card></>;
}

export default App;
