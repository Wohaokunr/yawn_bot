import {
  ApiOutlined,
  AuditOutlined,
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
  Empty,
  Flex,
  Form,
  Input,
  InputNumber,
  Layout,
  Menu,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, openStatusStream, setCsrfToken } from "./api";
import { GamesPage } from "./games";
import { formatTime, PageHeader, useEntityRefresh } from "./shared";
import type {
  AgentAudit,
  AgentConfig,
  FeatureState,
  GroupSummary,
  Member,
  MemoryItem,
  Overview,
  Persona,
  PrivacyItem,
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
  const [rows, setRows] = useState<GroupSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const load = useCallback(() => { setLoading(true); api<GroupSummary[]>(`/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }).finally(() => setLoading(false)); }, [page, search]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const columns: ColumnsType<GroupSummary> = [
    { title: "群", render: (_, row) => <><Link to={`/groups/${row.groupId}`}>{row.groupName || "未命名群"}</Link><br /><Text type="secondary" copyable>{row.groupId}</Text></> },
    { title: "成员", dataIndex: "memberCount", width: 100 },
    { title: "Agent", dataIndex: "agentEnabled", width: 100, render: (value: boolean) => <Tag color={value ? "green" : "default"}>{value ? "开启" : "关闭"}</Tag> },
    { title: "最近活跃", dataIndex: "lastActiveAt", render: formatTime },
    { title: "操作", width: 100, render: (_, row) => <Link to={`/agent/${row.groupId}`}>Agent</Link> },
  ];
  return <><PageHeader title="群组与权限" subtitle="管理群级及成员级功能覆盖" extra={<Input.Search placeholder="搜索群名或群号" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card><Table rowKey="groupId" loading={loading} columns={columns} dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} /></Card></>;
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

function GroupDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const { message } = AntApp.useApp();
  const [group, setGroup] = useState<{ groupId: string; groupName?: string; memberCount: number; features: FeatureState[] } | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [memberFeatures, setMemberFeatures] = useState<FeatureState[]>([]);
  const [selectedMember, setSelectedMember] = useState<Member | null>(null);
  const load = useCallback(async () => { const [g, m] = await Promise.all([api<typeof group>(`/groups/${groupId}`), api<Member[]>(`/groups/${groupId}/members?pageSize=100`)]); setGroup(g.data); setMembers(m.data); }, [groupId]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const openMember = async (member: Member) => { setSelectedMember(member); const result = await api<FeatureState[]>(`/groups/${groupId}/members/${member.userId}/features`); setMemberFeatures(result.data); };
  if (!group) return <Spin />;
  return <><PageHeader title={group.groupName || "未命名群"} subtitle={`群号 ${group.groupId} · ${group.memberCount} 名成员`} extra={<Link to="/groups">返回列表</Link>} /><Tabs items={[
    { key: "features", label: "群功能", children: <Card><FeatureEditor rows={group.features} onChange={async (feature, override) => { await api(`/groups/${groupId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); message.success("群功能已更新"); await load(); }} /></Card> },
    { key: "members", label: "成员", children: <Card><Table rowKey="userId" dataSource={members} pagination={{ pageSize: 20 }} columns={[{ title: "成员", render: (_, row: Member) => <>{row.groupNickname || row.nickname || "未知成员"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role" }, { title: "最近出现", dataIndex: "lastSeenAt", render: formatTime }, { title: "操作", render: (_, row: Member) => <Button type="link" onClick={() => openMember(row)}>功能权限</Button> }]} /></Card> },
  ]} />
  <Drawer open={!!selectedMember} width={680} title={`${selectedMember?.groupNickname || selectedMember?.nickname || selectedMember?.userId} · 功能覆盖`} onClose={() => setSelectedMember(null)}>{selectedMember && <FeatureEditor rows={memberFeatures} onChange={async (feature, override) => { const result = await api<FeatureState>(`/groups/${groupId}/members/${selectedMember.userId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); setMemberFeatures((current) => current.map((row) => row.key === feature ? result.data : row)); message.success("成员功能已更新"); }} />}</Drawer></>;
}

function UsersPage(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<UserSummary[]>([]); const [total, setTotal] = useState(0); const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<UserSummary | null>(null); const [features, setFeatures] = useState<FeatureState[]>([]);
  const load = useCallback(() => api<UserSummary[]>(`/users?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [page, search]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const open = async (user: UserSummary) => { setSelected(user); setFeatures((await api<FeatureState[]>(`/users/${user.userId}/features`)).data); };
  return <><PageHeader title="全局用户" subtitle="管理私聊及跨群全局功能覆盖" extra={<Input.Search placeholder="搜索昵称或 QQ" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card><Table rowKey="userId" dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} columns={[{ title: "用户", render: (_, row: UserSummary) => <>{row.nickname || "未知用户"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "好感", dataIndex: "affinity" }, { title: "最近互动", dataIndex: "lastInteractionAt", render: formatTime }, { title: "操作", render: (_, row: UserSummary) => <Button type="link" onClick={() => open(row)}>全局功能</Button> }]} /></Card><Drawer open={!!selected} width={680} title={`${selected?.nickname || selected?.userId} · 全局功能`} onClose={() => setSelected(null)}>{selected && <FeatureEditor rows={features} onChange={async (feature, override) => { const result = await api<FeatureState>(`/users/${selected.userId}/features/${feature}`, { method: "PATCH", body: JSON.stringify({ override }) }); setFeatures((current) => current.map((row) => row.key === feature ? result.data : row)); message.success("全局用户功能已更新"); }} />}</Drawer></>;
}

function AgentGroupsPage(): React.JSX.Element {
  const [rows, setRows] = useState<GroupSummary[]>([]); const [search, setSearch] = useState("");
  const load = useCallback(() => api<GroupSummary[]>(`/groups?pageSize=100&search=${encodeURIComponent(search)}`).then((r) => setRows(r.data)), [search]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  return <><PageHeader title="Agent 管理" subtitle="选择群组配置触发、人设、记忆和工具策略" extra={<Input.Search placeholder="搜索群组" allowClear onSearch={setSearch} />} /><Card><Table rowKey="groupId" dataSource={rows} pagination={{ pageSize: 20 }} columns={[{ title: "群组", render: (_, row: GroupSummary) => <>{row.groupName || "未命名群"}<br /><Text type="secondary">{row.groupId}</Text></> }, { title: "成员", dataIndex: "memberCount" }, { title: "状态", render: (_, row: GroupSummary) => <Tag color={row.agentEnabled ? "green" : "default"}>{row.agentEnabled ? "开启" : "关闭"}</Tag> }, { title: "操作", render: (_, row: GroupSummary) => <Link to={`/agent/${row.groupId}`}>进入管理</Link> }]} /></Card></>;
}

function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  return <><PageHeader title={`Agent · ${groupId}`} subtitle="群级配置、人设和数据治理" extra={<Link to="/agent">返回 Agent 列表</Link>} /><Tabs destroyOnHidden items={[
    { key: "config", label: "运行配置", children: <AgentConfigPanel groupId={groupId} /> },
    { key: "persona", label: "人设", children: <PersonaPanel groupId={groupId} /> },
    { key: "memories", label: "记忆", children: <MemoriesPanel groupId={groupId} /> },
    { key: "privacy", label: "隐私退出", children: <PrivacyPanel groupId={groupId} /> },
    { key: "audit", label: "工具审计", children: <AgentAuditsPanel groupId={groupId} /> },
  ]} /></>;
}

function AgentConfigPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [data, setData] = useState<AgentConfig | null>(null); const [saving, setSaving] = useState(false);
  const load = useCallback(() => api<AgentConfig>(`/agent/groups/${groupId}/config`).then((r) => { setData(r.data); form.setFieldsValue(r.data); }), [form, groupId]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const save = async (values: Record<string, unknown>) => { setSaving(true); try { const result = await api<AgentConfig>(`/agent/groups/${groupId}/config`, { method: "PATCH", body: JSON.stringify({ ...values, version: data?.version }) }); setData(result.data); form.setFieldsValue(result.data); message.success("Agent 配置已保存"); } catch (error) { if (error instanceof ApiError && error.status === 409) { message.warning(error.message); await load(); } else message.error((error as Error).message); } finally { setSaving(false); } };
  if (!data) return <Spin />;
  return <Card><Alert type="info" showIcon message={`今日主动发言 ${data.proactiveToday} 次；管理工具 ${data.adminToolsToday} 次`} /><Form form={form} layout="vertical" onFinish={save} className="settings-form"><Row gutter={16}><Col xs={24} md={8}><Form.Item name="enabled" label="启用 Agent" valuePropName="checked"><Switch /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="mediaCacheEnabled" label="媒体缓存" valuePropName="checked"><Switch /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="triggerMode" label="触发模式" rules={[{ required: true }]}><Select options={[{ value: "mention_only", label: "仅 @" }, { value: "mention_or_reply", label: "@ 或回复" }, { value: "explicit_wakeup", label: "@ 或显式唤醒" }, { value: "mention_or_proactive", label: "@ / 回复 / 唤醒 / 主动" }]} /></Form.Item></Col></Row><Row gutter={16}><Col xs={24} md={8}><Form.Item name="proactiveProbability" label="主动概率"><InputNumber min={0} max={1} step={0.05} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="idleThresholdMinutes" label="冷场阈值（分钟）"><InputNumber min={1} max={10080} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="cooldownMinutes" label="冷却时间（分钟）"><InputNumber min={0} max={10080} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="dailyLimit" label="主动发言每日上限"><InputNumber min={0} max={1000} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="rawRetentionDays" label="原始消息保留天数"><InputNumber min={1} max={365} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="adminToolDailyLimit" label="管理工具每日上限"><InputNumber min={1} max={1000} /></Form.Item></Col></Row><Form.Item name="toolAllowlist" label="管理工具白名单"><Select mode="multiple" options={[{ value: "mute_member", label: "禁言成员" }, { value: "create_group_announcement", label: "发布群公告" }]} /></Form.Item><Button type="primary" htmlType="submit" loading={saving}>保存配置</Button></Form></Card>;
}

function PersonaPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [data, setData] = useState<Persona | null>(null); const [saving, setSaving] = useState(false);
  const load = useCallback(() => api<Persona>(`/agent/groups/${groupId}/persona`).then((r) => { setData(r.data); form.setFieldsValue({ enabled: r.data.enabled, overrides: r.data.overrides }); }), [form, groupId]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const save = async (values: { enabled: boolean; overrides?: Record<string, string> }) => { setSaving(true); try { const result = await api<Persona>(`/agent/groups/${groupId}/persona`, { method: "PUT", body: JSON.stringify({ version: data?.version, enabled: values.enabled, overrides: values.overrides ?? {} }) }); setData(result.data); message.success("群级人设已保存"); } catch (error) { if (error instanceof ApiError && error.status === 409) { message.warning(error.message); await load(); } else message.error((error as Error).message); } finally { setSaving(false); } };
  const reset = async () => { const result = await api<Persona>(`/agent/groups/${groupId}/persona`, { method: "DELETE", headers: data?.version ? { "If-Match": data.version } : {} }); setData(result.data); form.setFieldsValue({ enabled: true, overrides: {} }); message.success("已恢复全局默认人设"); };
  if (!data) return <Spin />;
  return <Card><Form form={form} layout="vertical" onFinish={save} initialValues={{ enabled: data.enabled }}><Form.Item name="enabled" label="启用群级覆盖" valuePropName="checked"><Switch /></Form.Item><Row gutter={16}>{data.fields.map((field) => <Col xs={24} lg={12} key={field}><Form.Item name={["overrides", field]} label={field} extra={`全局值：${data.resolved[field]}`}><Input.TextArea maxLength={240} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="留空则继承全局默认" showCount /></Form.Item></Col>)}</Row><Space><Button type="primary" htmlType="submit" loading={saving}>保存人设</Button><Popconfirm title="恢复全局默认人设？" onConfirm={reset}><Button>恢复默认</Button></Popconfirm></Space></Form></Card>;
}

function MemoriesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [rows, setRows] = useState<MemoryItem[]>([]); const [page, setPage] = useState(1); const [total, setTotal] = useState(0); const [search, setSearch] = useState("");
  const load = useCallback(() => api<MemoryItem[]>(`/agent/groups/${groupId}/memories?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [groupId, page, search]);
  useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  const remove = async (id: string) => { await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" }); message.success("记忆已删除"); await load(); };
  const removeMember = async (userId: string) => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/members/${userId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条成员数据`); await load(); };
  const removeGroup = async () => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条群 Agent 数据`); await load(); };
  const exportData = async () => { const result = await api(`/agent/groups/${groupId}/memories/export`); const blob = new Blob([JSON.stringify(result.data, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `yawnbot-agent-${groupId}.json`; anchor.click(); URL.revokeObjectURL(url); };
  return <Card title="公开/群级记忆" extra={<Space><Button onClick={exportData}>导出 JSON</Button><Popconfirm title="清理整个群的消息、记忆、关系和媒体缓存？" description="此操作还会重置上下文游标，且不可撤销。" onConfirm={removeGroup}><Button danger>清理全群 Agent 数据</Button></Popconfirm></Space>}><Input.Search className="table-search" placeholder="搜索 key 或内容" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} /><Table rowKey="id" dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.content}</Paragraph> }} columns={[{ title: "记忆", render: (_, row: MemoryItem) => <><Text strong>{row.key}</Text><br /><Text type="secondary">{row.type} · {row.visibility}</Text></> }, { title: "成员", dataIndex: "subjectUserId", render: (value?: string) => value || "群级" }, { title: "权重", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.salience * 100)} size="small" /> }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: MemoryItem) => <Space><Popconfirm title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" danger>删除</Button></Popconfirm>{row.subjectUserId && <Popconfirm title={`清理成员 ${row.subjectUserId} 的全部 Agent 数据？`} onConfirm={() => removeMember(row.subjectUserId!)}><Button type="link" danger>清理成员</Button></Popconfirm>}</Space> }]} /></Card>;
}

function PrivacyPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [rows, setRows] = useState<PrivacyItem[]>([]); const load = useCallback(() => api<PrivacyItem[]>(`/agent/groups/${groupId}/privacy?pageSize=100`).then((r) => setRows(r.data)), [groupId]); useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  return <Card><Alert type="info" showIcon message="成员隐私状态只读" description="退出和恢复由成员本人通过群命令操作；管理台仅用于确认数据治理状态。" /><Table rowKey="userId" dataSource={rows} locale={{ emptyText: <Empty description="暂无成员隐私记录" /> }} columns={[{ title: "用户 ID", dataIndex: "userId" }, { title: "状态", dataIndex: "optedOut", render: (value: boolean) => <Tag color={value ? "orange" : "green"}>{value ? "已退出记忆" : "已恢复"}</Tag> }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }]} /></Card>;
}

function AgentAuditTable({ data }: { data: AgentAudit[] }): React.JSX.Element {
  return <Table rowKey="id" size="small" pagination={false} dataSource={data} columns={[{ title: "时间", dataIndex: "createdAt", render: formatTime }, { title: "群", dataIndex: "groupId" }, { title: "工具", dataIndex: "toolName" }, { title: "结果", dataIndex: "result", render: (value: string) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> }, { title: "详情", dataIndex: "detail", ellipsis: true }]} />;
}

function AgentAuditsPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [rows, setRows] = useState<AgentAudit[]>([]); const [page, setPage] = useState(1); const [total, setTotal] = useState(0); const load = useCallback(() => api<AgentAudit[]>(`/agent/audits?groupId=${groupId}&page=${page}&pageSize=20`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [groupId, page]); useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  return <Card><AgentAuditTable data={rows} /><TablePagination current={page} total={total} onChange={setPage} /></Card>;
}

function TablePagination({ current, total, onChange }: { current: number; total: number; onChange: (page: number) => void }): React.JSX.Element {
  if (total <= 20) return <></>;
  const pagination: TablePaginationConfig = { current, total, pageSize: 20, showSizeChanger: false, onChange };
  return <Table rowKey="placeholder" columns={[]} dataSource={[]} showHeader={false} pagination={pagination} className="pagination-only" />;
}

function WebAuditsPage(): React.JSX.Element {
  const [rows, setRows] = useState<WebAudit[]>([]); const [page, setPage] = useState(1); const [total, setTotal] = useState(0); const [result, setResult] = useState("");
  const load = useCallback(() => api<WebAudit[]>(`/web-audits?page=${page}&pageSize=20&result=${result}`).then((r) => { setRows(r.data); setTotal(r.meta.total ?? 0); }), [page, result]); useEffect(() => { void load(); }, [load]); useEntityRefresh(load);
  return <><PageHeader title="操作审计" subtitle="持久化记录 WebUI 配置修改与删除操作" extra={<Select value={result} onChange={(value) => { setResult(value); setPage(1); }} options={[{ value: "", label: "全部结果" }, { value: "success", label: "成功" }, { value: "failed", label: "失败" }]} />} /><Card><Table rowKey="id" dataSource={rows} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <pre>{JSON.stringify(row.detail, null, 2)}</pre> }} columns={[{ title: "时间", dataIndex: "createdAt", render: formatTime }, { title: "动作", dataIndex: "action" }, { title: "资源", render: (_, row: WebAudit) => <>{row.resourceType}<br /><Text type="secondary">{row.resourceId || "—"}</Text></> }, { title: "会话指纹", dataIndex: "actorSession" }, { title: "结果", dataIndex: "result", render: (value: string) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> }, { title: "请求 ID", dataIndex: "requestId", ellipsis: true }]} /></Card></>;
}

export default App;
