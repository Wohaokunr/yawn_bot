import {
  Alert,
  App as AntApp,
  AutoComplete,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Row,
  Segmented,
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
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, TablePagination, useApiQuery } from "./shared";
import { nodeDisplayName, RelationGraphView, relationTypeColor } from "./relation-graph";
import type {
  AgentAudit,
  AgentConfig,
  AgentMemoryStatus,
  AgentMessageItem,
  AgentRelationGraph,
  AgentRelationItem,
  GroupSummary,
  MemoryItem,
  Persona,
  PrivacyItem,
} from "./types";

const { Text, Paragraph } = Typography;

// 记忆类型标签：与后端 memory_type 口径对齐（summary/profile 为整理任务产出，manual 为运维手填）。
export const MEMORY_TYPE_META: Record<string, { label: string; color: string }> = {
  summary: { label: "群摘要", color: "geekblue" },
  profile: { label: "成员画像", color: "purple" },
  manual: { label: "置顶事实", color: "gold" },
};

export function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_META[type]?.label ?? type;
}

// 关系类型与来源口径：与后端 memory.py 的枚举/别名表对齐，自定义类型原样展示。
export const RELATION_TYPE_PRESETS = ["好友", "死党", "情侣", "伴侣", "亲属", "师徒", "同事", "同学", "搭子", "对立"];
const RELATION_SOURCE_META: Record<string, { label: string; color: string }> = {
  manual: { label: "手工", color: "gold" },
  auto: { label: "自动", color: "default" },
  mention: { label: "提及", color: "blue" },
  agent: { label: "Agent", color: "green" },
};

const MEMORY_ROLE_OPTIONS = [
  { value: "", label: "全部角色" },
  { value: "member", label: "成员" },
  { value: "admin", label: "管理员" },
  { value: "owner", label: "群主" },
  { value: "bot", label: "Bot" },
];

export function AgentGroupsPage(): React.JSX.Element {
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const load = useCallback(() => api<GroupSummary[]>(`/groups?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [page, search]);
  const query = useApiQuery(load);
  return <><PageHeader title="Agent 管理" subtitle="选择群组配置触发、人设、记忆和工具策略" extra={<Input.Search placeholder="搜索群组" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />} /><Card>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table rowKey="groupId" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} columns={[{ title: "群组", render: (_, row: GroupSummary) => <>{row.groupName || "未命名群"}<br /><Text type="secondary">{row.groupId}</Text></> }, { title: "成员", dataIndex: "memberCount" }, { title: "状态", render: (_, row: GroupSummary) => <Tag color={row.agentEnabled ? "green" : "default"}>{row.agentEnabled ? "开启" : "关闭"}</Tag> }, { title: "操作", render: (_, row: GroupSummary) => <Link to={`/agent/${row.groupId}`}>进入管理</Link> }]} />
  }</Card></>;
}

export function AgentDetailPage(): React.JSX.Element {
  const { groupId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") ?? "config";
  return <><PageHeader title={`Agent · ${groupId}`} subtitle="群级配置、人设、记忆与数据治理" extra={<Link to="/agent">返回 Agent 列表</Link>} /><Tabs destroyOnHidden activeKey={tab} onChange={(key) => setSearchParams(key === "config" ? {} : { tab: key }, { replace: true })} items={[
    { key: "config", label: "运行配置", children: <AgentConfigPanel groupId={groupId} /> },
    { key: "persona", label: "人设", children: <PersonaPanel groupId={groupId} /> },
    { key: "memories", label: "记忆", children: <MemoriesPanel groupId={groupId} /> },
    { key: "relations", label: "关系边", children: <RelationsPanel groupId={groupId} /> },
    { key: "messages", label: "消息记录", children: <AgentMessagesPanel groupId={groupId} /> },
    { key: "privacy", label: "隐私退出", children: <PrivacyPanel groupId={groupId} /> },
    { key: "audit", label: "工具审计", children: <AgentAuditsPanel groupId={groupId} /> },
  ]} /></>;
}

function AgentConfigPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [saving, setSaving] = useState(false);
  const load = useCallback(() => api<AgentConfig>(`/agent/groups/${groupId}/config`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load);
  useEffect(() => { if (query.data) form.setFieldsValue(query.data); }, [form, query.data]);
  const save = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      const result = await api<AgentConfig>(`/agent/groups/${groupId}/config`, { method: "PATCH", body: JSON.stringify({ ...values, version: query.data?.version }) });
      form.setFieldsValue(result.data);
      message.success("Agent 配置已保存");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); query.reload(); } else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  return <Card><Alert type="info" showIcon message={`今日主动发言 ${data.proactiveToday} 次；管理工具 ${data.adminToolsToday} 次`} /><Form form={form} layout="vertical" onFinish={save} className="settings-form"><Row gutter={16}><Col xs={24} md={8}><Form.Item name="enabled" label="启用 Agent" valuePropName="checked"><Switch /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="mediaCacheEnabled" label="媒体缓存" valuePropName="checked"><Switch /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="triggerMode" label="触发模式" rules={[{ required: true }]}><Select options={[{ value: "mention_only", label: "仅 @" }, { value: "mention_or_reply", label: "@ 或回复" }, { value: "explicit_wakeup", label: "@ 或显式唤醒" }, { value: "mention_or_proactive", label: "@ / 回复 / 唤醒 / 主动" }]} /></Form.Item></Col></Row><Row gutter={16}><Col xs={24} md={8}><Form.Item name="proactiveProbability" label="冷场暖场概率"><InputNumber min={0} max={1} step={0.05} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="proactiveActiveEnabled" label="热闹插话" valuePropName="checked"><Switch /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="proactiveActiveProbability" label="插话概率"><InputNumber min={0} max={1} step={0.02} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="proactiveActiveWindowMinutes" label="插话窗口（分钟）"><InputNumber min={1} max={1440} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="idleThresholdMinutes" label="冷场阈值（分钟）"><InputNumber min={1} max={10080} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="cooldownMinutes" label="冷却时间（分钟）"><InputNumber min={0} max={10080} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="dailyLimit" label="主动发言每日上限"><InputNumber min={0} max={1000} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="rawRetentionDays" label="原始消息保留天数"><InputNumber min={1} max={365} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="crossGroupVisibility" label="跨群记忆"><Select options={[{ value: "isolated", label: "群隔离" }, { value: "public_summary", label: "共享低风险公开摘要" }]} /></Form.Item></Col><Col xs={24} md={8}><Form.Item name="adminToolDailyLimit" label="管理工具每日上限"><InputNumber min={1} max={1000} /></Form.Item></Col></Row><Form.Item name="toolAllowlist" label="管理工具白名单"><Select mode="multiple" options={[{ value: "mute_member", label: "禁言成员" }, { value: "create_group_announcement", label: "发布群公告" }]} /></Form.Item><Button type="primary" htmlType="submit" loading={saving}>保存配置</Button></Form></Card>;
}

function PersonaPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp(); const [form] = Form.useForm(); const [saving, setSaving] = useState(false);
  const load = useCallback(() => api<Persona>(`/agent/groups/${groupId}/persona`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load);
  useEffect(() => { if (query.data) form.setFieldsValue({ enabled: query.data.enabled, overrides: query.data.overrides }); }, [form, query.data]);
  const save = async (values: { enabled: boolean; overrides?: Record<string, string> }) => {
    setSaving(true);
    try {
      await api<Persona>(`/agent/groups/${groupId}/persona`, { method: "PUT", body: JSON.stringify({ version: query.data?.version, enabled: values.enabled, overrides: values.overrides ?? {} }) });
      message.success("群级人设已保存");
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); query.reload(); } else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const reset = async () => {
    try {
      await api<Persona>(`/agent/groups/${groupId}/persona`, { method: "DELETE", headers: query.data?.version ? { "If-Match": query.data.version } : {} });
      message.success("已恢复全局默认人设");
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Spin />;
  return <Card><Form form={form} layout="vertical" onFinish={save}><Form.Item name="enabled" label="启用群级覆盖" valuePropName="checked"><Switch /></Form.Item><Row gutter={16}>{data.fields.map((field) => <Col xs={24} lg={12} key={field}><Form.Item name={["overrides", field]} label={field} extra={`全局值：${data.resolved[field]}`}><Input.TextArea maxLength={240} autoSize={{ minRows: 2, maxRows: 4 }} placeholder="留空则继承全局默认" showCount /></Form.Item></Col>)}</Row><Space><Button type="primary" htmlType="submit" loading={saving}>保存人设</Button><Popconfirm title="恢复全局默认人设？" onConfirm={reset}><Button>恢复默认</Button></Popconfirm></Space></Form></Card>;
}

// 记忆表单的可编辑字段；expiresInDays 为空表示永久有效。
interface MemoryFormValues {
  content: string;
  salience: number;
  confidence: number;
  expiresInDays: number | null;
}

function MemoriesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editForm] = Form.useForm();
  const [createForm] = Form.useForm();
  const load = useCallback(() => api<MemoryItem[]>(`/agent/groups/${groupId}/memories?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search]);
  const query = useApiQuery(load);
  const statusLoad = useCallback(() => api<AgentMemoryStatus>(`/agent/groups/${groupId}/memories/status`).then((r) => r.data), [groupId]);
  const statusQuery = useApiQuery(statusLoad);
  const status = statusQuery.data;
  const remove = async (id: string) => { await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" }); message.success("记忆已删除"); query.reload(); };
  const removeMember = async (userId: string) => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/members/${userId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条成员数据`); query.reload(); };
  const removeGroup = async () => { const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条群 Agent 数据`); query.reload(); };
  const exportData = async () => { const result = await api(`/agent/groups/${groupId}/memories/export`); const blob = new Blob([JSON.stringify(result.data, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `yawnbot-agent-${groupId}.json`; anchor.click(); URL.revokeObjectURL(url); };
  const compact = async () => {
    try {
      await api(`/agent/groups/${groupId}/memories/compact`, { method: "POST" });
      message.success("整理已在后台启动，完成后这里会自动刷新");
      // 整理含 LLM 摘要可达数十秒；延迟一轮再拉状态，配合 entity.changed 事件兜底。
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 8000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const rebuild = async () => {
    try {
      await api(`/agent/groups/${groupId}/memories/rebuild`, { method: "POST" });
      message.success("派生记忆重建已启动，手工记忆会保留");
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 3000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const openEdit = (row: MemoryItem) => {
    setEditing(row);
    editForm.setFieldsValue({
      content: row.content,
      salience: row.salience,
      confidence: row.confidence,
      expiresInDays: row.expiresAt ? Math.max(1, Math.ceil((new Date(row.expiresAt).getTime() - Date.now()) / 86400000)) : null,
    });
  };
  const saveEdit = async (values: MemoryFormValues) => {
    if (!editing) return;
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories/${editing.id}`, { method: "PUT", body: JSON.stringify({ ...values, version: editing.updatedAt }) });
      message.success("记忆已更新");
      setEditing(null);
      query.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); query.reload(); setEditing(null); } else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const saveCreate = async (values: MemoryFormValues & { type: string; key: string; subjectUserId?: number }) => {
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories`, { method: "POST", body: JSON.stringify(values) });
      message.success("记忆已新增");
      setCreating(false);
      createForm.resetFields();
      query.reload();
      statusQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  return <>
    <Row gutter={[12, 12]} className="section-row">
      <Col xs={12} md={6}><Card size="small"><Statistic title="有效记忆" value={status?.total ?? "—"} suffix="条" /></Card></Col>
      <Col xs={12} md={6}><Card size="small"><Statistic title="待整理消息" value={status?.pendingMessages ?? "—"} suffix="条" /></Card></Col>
      <Col xs={24} md={12}><Card size="small"><div className="ag-stat-line">
        <Space wrap size={[8, 8]}>{Object.entries(status?.countsByType ?? {}).map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {count}</Tag>)}{status && Object.keys(status.countsByType).length === 0 && <Text type="secondary">暂无记忆</Text>}</Space>
        <Text type="secondary">最后成功：{status?.lastSuccessAt ? formatTime(status.lastSuccessAt) : "尚未整理"}</Text>
      </div></Card></Col>
    </Row>
    {status?.lastError && <Alert type="error" showIcon closable message={`最近整理失败（连续 ${status.consecutiveFailures} 次）`} description={status.lastError} className="section-alert" />}
    {status?.rebuildRequired && <Alert type="warning" showIcon message="派生记忆正在重建" description="系统会按连续批次处理保留期内原始消息；手工记忆不会被覆盖。" className="section-alert" />}
    <Card title="公开/群级记忆" extra={<Space><Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增记忆</Button><Popconfirm title="立即整理本群记忆？" description="含 LLM 摘要，将在后台运行数十秒。" onConfirm={compact}><Button loading={status?.inFlight}>立即整理</Button></Popconfirm><Popconfirm title="重建全部自动派生记忆？" description="保留手工记忆，清除自动摘要/画像/关系后从短期消息重新生成。" onConfirm={rebuild}><Button>重建派生记忆</Button></Popconfirm><Button onClick={exportData}>导出 JSON</Button><Popconfirm title="清理整个群的消息、记忆、关系和媒体缓存？" description="此操作还会重置上下文游标，且不可撤销。" onConfirm={removeGroup}><Button danger>清理全群 Agent 数据</Button></Popconfirm></Space>}><Input.Search className="table-search" placeholder="搜索 key 或内容" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.content}</Paragraph> }} columns={[{ title: "记忆", render: (_, row: MemoryItem) => <><Text strong>{row.key}</Text><br /><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag><Text type="secondary"> · {row.visibility} · {row.sourceKind === "manual" ? "手工" : "自动"}</Text></> }, { title: "成员", dataIndex: "subjectUserId", render: (value?: string) => value || "群级" }, { title: "权重", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.salience * 100)} size="small" /> }, { title: "置信度", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" strokeColor="var(--ant-color-success)" /> }, { title: "有效期至", dataIndex: "expiresAt", render: (value?: string | null) => value ? formatTime(value) : "永久" }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: MemoryItem) => <Space><Button type="link" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" danger>删除</Button></Popconfirm>{row.subjectUserId && <Popconfirm title={`清理成员 ${row.subjectUserId} 的全部 Agent 数据？`} onConfirm={() => removeMember(row.subjectUserId!)}><Button type="link" danger>清理成员</Button></Popconfirm>}</Space> }]} />
    }</Card>
    <Drawer open={!!editing} width={520} title={`编辑记忆 · ${editing?.key ?? ""}`} onClose={() => setEditing(null)}>
      <Form form={editForm} layout="vertical" onFinish={saveEdit}>
        <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount /></Form.Item>
        <Row gutter={16}>
          <Col span={12}><Form.Item name="salience" label="显著度（注入优先级）" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
          <Col span={12}><Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Form.Item name="expiresInDays" label="有效期（天）"><InputNumber min={1} max={3650} placeholder="清空则永久有效" style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>保存</Button><Button onClick={() => setEditing(null)}>取消</Button></Space>
      </Form>
    </Drawer>
    <Drawer open={creating} width={520} title="新增记忆" onClose={() => setCreating(false)}>
      <Form form={createForm} layout="vertical" onFinish={saveCreate} initialValues={{ type: "manual", salience: 0.7, confidence: 0.9 }}>
        <Form.Item name="type" label="类型" rules={[{ required: true }]}><Select options={Object.entries(MEMORY_TYPE_META).map(([value, meta]) => ({ value, label: meta.label }))} /></Form.Item>
        <Form.Item name="key" label="Key（同类型下唯一）" rules={[{ required: true, message: "请输入 key" }]}><Input maxLength={128} placeholder="如 群规 / display_name" /></Form.Item>
        <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount /></Form.Item>
        <Form.Item name="subjectUserId" label="归属成员 QQ（画像类填写）"><InputNumber min={1} precision={0} placeholder="留空为群级" style={{ width: "100%" }} /></Form.Item>
        <Row gutter={16}>
          <Col span={12}><Form.Item name="salience" label="显著度"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
          <Col span={12}><Form.Item name="confidence" label="置信度"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Form.Item name="expiresInDays" label="有效期（天）"><InputNumber min={1} max={3650} placeholder="留空则永久有效" style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>新增</Button><Button onClick={() => setCreating(false)}>取消</Button></Space>
      </Form>
    </Drawer>
  </>;
}

function RelationsPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view") === "graph" ? "graph" : "table";
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [typeFilter, setTypeFilter] = useState("");
  const [creating, setCreating] = useState(false); const [editing, setEditing] = useState<AgentRelationItem | null>(null); const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm(); const [editForm] = Form.useForm();
  const setView = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "graph") next.set("view", "graph"); else next.delete("view");
    setSearchParams(next, { replace: true });
  };
  const load = useCallback(() => api<AgentRelationItem[]>(`/agent/groups/${groupId}/relations?page=${page}&pageSize=20&search=${encodeURIComponent(search)}${typeFilter ? `&type=${encodeURIComponent(typeFilter)}` : ""}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, typeFilter]);
  const query = useApiQuery(load);
  const graphLoad = useCallback(() => api<AgentRelationGraph>(`/agent/groups/${groupId}/relations/graph`).then((r) => r.data), [groupId]);
  const graphQuery = useApiQuery(graphLoad);
  const graph = graphQuery.data;
  const nodeByUserId = useMemo(() => new Map((graph?.nodes ?? []).map((node) => [node.userId, node])), [graph]);
  const linkedMemberCount = useMemo(() => (graph?.nodes ?? []).filter((node) => node.linked).length, [graph]);
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const edge of graph?.edges ?? []) counts.set(edge.type, (counts.get(edge.type) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [graph]);
  const lastSeen = useMemo(() => {
    let latest: string | null = null;
    for (const edge of graph?.edges ?? []) if (edge.lastSeenAt && (!latest || edge.lastSeenAt > latest)) latest = edge.lastSeenAt;
    return latest;
  }, [graph]);
  const typesLoad = useCallback(() => api<string[]>(`/agent/groups/${groupId}/relations/types`).then((r) => r.data), [groupId]);
  const typesQuery = useApiQuery(typesLoad);
  const typeOptions = Array.from(new Set([...RELATION_TYPE_PRESETS, ...(typesQuery.data ?? [])])).map((value) => ({ value, label: value }));
  const remove = async (id: string) => { await api(`/agent/groups/${groupId}/relations/${id}`, { method: "DELETE" }); message.success("关系边已删除"); query.reload(); typesQuery.reload(); graphQuery.reload(); };
  const saveCreate = async (values: { subjectUserId: number; objectUserId: number; type: string; note: string; confidence: number }) => {
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations`, { method: "POST", body: JSON.stringify(values) });
      message.success("关系边已新增"); setCreating(false); createForm.resetFields(); query.reload(); typesQuery.reload(); graphQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const openEdit = (row: AgentRelationItem) => { setEditing(row); editForm.setFieldsValue({ note: row.note, confidence: row.confidence }); };
  const saveEdit = async (values: { note: string; confidence: number }) => {
    if (!editing) return;
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations/${editing.id}`, { method: "PUT", body: JSON.stringify(values) });
      message.success("关系边已更新"); setEditing(null); query.reload(); graphQuery.reload();
    } catch (error) { message.error((error as Error).message); } finally { setSaving(false); }
  };
  const renderMemberCell = (value: string) => {
    const name = nodeDisplayName(nodeByUserId.get(value), value);
    return name !== value ? <>{name}<br /><Text type="secondary" copyable>{value}</Text></> : <Text copyable>{value}</Text>;
  };
  return <>
    <Row gutter={[12, 12]} className="section-row">
      <Col xs={12} md={6}><Card size="small"><Statistic title="关系边" value={graph ? graph.edges.length : "—"} suffix={graph?.meta.relationTruncated ? "+ 条（已截断）" : "条"} /></Card></Col>
      <Col xs={12} md={6}><Card size="small"><Statistic title="关系成员" value={graph ? linkedMemberCount : "—"} suffix="人" /></Card></Col>
      <Col xs={24} md={12}><Card size="small"><div className="ag-stat-line">
        <Space wrap size={[8, 8]}>{typeCounts.map(([type, count]) => <Tag key={type} color={relationTypeColor(type)}>{type} × {count}</Tag>)}{graph && typeCounts.length === 0 && <Text type="secondary">暂无关系记忆</Text>}{!graph && graphQuery.error && <Text type="secondary">图谱数据加载失败</Text>}</Space>
        <Text type="secondary">最近关系更新：{lastSeen ? formatTime(lastSeen) : "—"}</Text>
      </div></Card></Col>
    </Row>
    <Card title="成员关系边" extra={<Space wrap>
      <Segmented value={view} onChange={(value) => setView(String(value))} options={[{ value: "table", label: "列表视图" }, { value: "graph", label: "图谱视图" }]} />
      <Select value={typeFilter} onChange={(value) => { setTypeFilter(value); setPage(1); }} style={{ width: 140 }} options={[{ value: "", label: "全部类型" }, ...typeOptions]} />
      {view === "table" && <Input.Search className="table-search" placeholder="搜索成员 QQ 号" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />}
      <Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增关系边</Button>
    </Space>}>{
      view === "graph"
        ? (graphQuery.error && !graph
          ? <QueryErrorAlert error={graphQuery.error} onRetry={graphQuery.reload} />
          : graph
            ? <RelationGraphView graph={graph} typeFilter={typeFilter} onEditRelation={openEdit} onDeleteRelation={(edge) => remove(edge.id)} />
            : <div className="rg-loading-wrap"><Spin /></div>)
        : (query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: <Empty description="暂无关系记忆" /> }} columns={[
            { title: "主体", dataIndex: "subjectUserId", render: renderMemberCell },
            { title: "客体", dataIndex: "objectUserId", render: renderMemberCell },
            { title: "类型", dataIndex: "type", render: (value: string) => <Tag color={relationTypeColor(value)}>{value}</Tag> },
            { title: "备注", dataIndex: "note", ellipsis: true, render: (value: string) => value || <Text type="secondary">—</Text> },
            { title: "来源", dataIndex: "sourceKind", width: 90, render: (value: string) => <Tag color={RELATION_SOURCE_META[value]?.color}>{RELATION_SOURCE_META[value]?.label ?? value}</Tag> },
            { title: "置信度", render: (_, row: AgentRelationItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "证据数", dataIndex: "evidenceCount", width: 80 },
            { title: "最后见到", dataIndex: "lastSeenAt", render: formatTime, width: 170 },
            { title: "操作", width: 120, render: (_, row: AgentRelationItem) => <Space><Button type="link" size="small" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这条关系边？" onConfirm={() => remove(row.id)}><Button type="link" size="small" danger>删除</Button></Popconfirm></Space> },
          ]} />)
    }</Card>
    <Drawer open={creating} width={520} title="新增关系边" onClose={() => setCreating(false)}>
      <Form form={createForm} layout="vertical" onFinish={saveCreate} initialValues={{ confidence: 0.9 }}>
        <Row gutter={16}>
          <Col span={12}><Form.Item name="subjectUserId" label="主体 QQ" rules={[{ required: true, message: "请输入主体 QQ" }]}><InputNumber min={1} precision={0} style={{ width: "100%" }} /></Form.Item></Col>
          <Col span={12}><Form.Item name="objectUserId" label="客体 QQ" rules={[{ required: true, message: "请输入客体 QQ" }]}><InputNumber min={1} precision={0} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Form.Item name="type" label="类型" rules={[{ required: true, message: "请选择或输入类型" }]}><AutoComplete options={typeOptions} placeholder="如 好友 / 情侣 / 对立" filterOption={(input, option) => String(option?.value ?? "").includes(input)} /></Form.Item>
        <Form.Item name="note" label="备注"><Input maxLength={200} placeholder="一句话关系背景（可选）" /></Form.Item>
        <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>新增</Button><Button onClick={() => setCreating(false)}>取消</Button></Space>
      </Form>
    </Drawer>
    <Drawer open={!!editing} width={520} title={`编辑关系边 · ${editing?.type ?? ""}`} onClose={() => setEditing(null)}>
      <Form form={editForm} layout="vertical" onFinish={saveEdit}>
        <Alert type="info" showIcon className="section-alert" message="类型与两端成员属于边的唯一身份，如需调整请删除后重新新增。" />
        <Form.Item name="note" label="备注"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={200} showCount /></Form.Item>
        <Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item>
        <Space><Button type="primary" htmlType="submit" loading={saving}>保存</Button><Button onClick={() => setEditing(null)}>取消</Button></Space>
      </Form>
    </Drawer>
  </>;
}

function AgentMessagesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [role, setRole] = useState("");
  const load = useCallback(() => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&role=${role}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, role]);
  const query = useApiQuery(load);
  return <Card title="短期消息库" extra={<Select value={role} onChange={(value) => { setRole(value); setPage(1); }} options={MEMORY_ROLE_OPTIONS} style={{ width: 120 }} />}>
    <Alert type="info" showIcon className="section-alert" message="仅保留 rawRetentionDays 内的原始消息；隐私退出成员的消息不在此展示，到期由整理任务清除。" />
    <Input.Search className="table-search" placeholder="搜索消息内容或昵称" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.text}</Paragraph> }} columns={[{ title: "时间", dataIndex: "receivedAt", render: formatTime, width: 170 }, { title: "成员", render: (_, row: AgentMessageItem) => <>{row.senderName || "—"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role", width: 90, render: (value: string) => <Tag color={value === "bot" ? "blue" : value === "owner" ? "gold" : value === "admin" ? "cyan" : "default"}>{value}</Tag> }, { title: "内容", dataIndex: "text", ellipsis: true }]} />
    }</Card>;
}

function PrivacyPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const load = useCallback(() => api<PrivacyItem[]>(`/agent/groups/${groupId}/privacy?pageSize=100`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load);
  const toggle = async (userId: string, optedOut: boolean) => {
    try {
      await api(`/agent/groups/${groupId}/privacy/${userId}`, { method: "PATCH", body: JSON.stringify({ optedOut }) });
      message.success(optedOut ? "已退出记忆并清除其已有数据" : "已恢复记忆");
      query.reload();
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  return <Card><Alert type="info" showIcon message="成员也可以自行通过群命令 /Agent隐私 退出或恢复" description="退出会立即清除该成员已沉淀的记忆与消息；后续消息不再进入 Agent 上下文。" />{
    query.error && !query.data
      ? <div className="section-alert"><QueryErrorAlert error={query.error} onRetry={query.reload} /></div>
      : <Table rowKey="userId" loading={query.loading} dataSource={query.data ?? []} locale={{ emptyText: <Empty description="暂无成员隐私记录" /> }} columns={[{ title: "用户 ID", dataIndex: "userId" }, { title: "状态", dataIndex: "optedOut", render: (value: boolean) => <Tag color={value ? "orange" : "green"}>{value ? "已退出记忆" : "已恢复"}</Tag> }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: PrivacyItem) => row.optedOut ? <Button type="link" onClick={() => toggle(row.userId, false)}>恢复记忆</Button> : <Popconfirm title={`让成员 ${row.userId} 退出 Agent 记忆？`} description="将立即清除其已沉淀的记忆、关系与消息，不可撤销。" onConfirm={() => toggle(row.userId, true)}><Button type="link" danger>退出记忆</Button></Popconfirm> }]} />
  }</Card>;
}

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

export function AgentAuditTable({ data }: { data: AgentAudit[] }): React.JSX.Element {
  return <Table rowKey="id" size="small" pagination={false} dataSource={data} columns={[{ title: "时间", dataIndex: "createdAt", render: formatTime }, { title: "群", dataIndex: "groupId" }, { title: "工具", dataIndex: "toolName" }, { title: "结果", dataIndex: "result", render: (value: string) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> }, { title: "详情", dataIndex: "detail", ellipsis: true }]} />;
}

function AgentAuditsPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [page, setPage] = useState(1); const [result, setResult] = useState("");
  const load = useCallback(() => api<AgentAudit[]>(`/agent/audits?groupId=${groupId}&page=${page}&pageSize=20&result=${result}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, result]);
  const query = useApiQuery(load);
  return <Card extra={<Select value={result} onChange={(value) => { setResult(value); setPage(1); }} options={RESULT_OPTIONS} style={{ width: 120 }} />}>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <>
        <AgentAuditTable data={query.data?.rows ?? []} />
        <TablePagination current={page} total={query.data?.total ?? 0} onChange={setPage} />
      </>
  }</Card>;
}
