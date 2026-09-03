import {
  Alert, App as AntApp, AutoComplete, Button, Card, Col, Descriptions, Drawer, Empty,
  Form, Input, InputNumber, List, Popconfirm, Progress, Row, Segmented, Select, Space,
  Spin, Statistic, Switch, Table, Tag, Timeline, Typography,
} from "antd";
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AgentAuditTable } from "../agent-audit-table";
import { TraceCompareView } from "../agent-debug/TraceWorkspace";
import { api, ApiError } from "../api";
import { nodeDisplayName, relationTypeColor } from "../relation-meta";
import {
  DangerActionButton, formatTime, QueryErrorAlert, SaveStatus, TablePagination,
  useApiQuery, useUnsavedChanges,
} from "../shared";
import type {
  AgentAudit, AgentConfig, AgentDebugResponse, AgentMemoryStatus, AgentMessageItem,
  AgentRelationGraph, AgentRelationItem, MemoryItem, MemorySubjectItem, Persona,
  PersonaProfile, PrivacyItem,
} from "../types";
import {
  MEMORY_ROLE_OPTIONS, MEMORY_TYPE_META, PERSONA_SOCIAL_TRAITS, PERSONA_STYLE_TRAITS,
  PERSONA_TRAIT_META, PERSONA_TRIAL_SCENARIOS, PROFILE_KEY_META, PROFILE_KEY_META as _PROFILE_KEY_META,
  RELATION_SOURCE_META, RELATION_TYPE_PRESETS, memberDisplayName, mergePersonaPreset,
  personaBehaviorPreview, personaDraftSummary, personaEmotionExpressionPreview,
  profileKeyLabel, memoryTypeLabel,
} from "../agent-meta";

const { Text, Paragraph } = Typography;
const LazyRelationGraphView = lazy(() =>
  import("../relation-graph").then(({ RelationGraphView }) => ({ default: RelationGraphView })),
);

export function RelationsPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
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
  const query = useApiQuery(load, { resources: ["agent_relation", "agent_member_data", "agent_group_data"] });
  const graphLoad = useCallback(() => api<AgentRelationGraph>(`/agent/groups/${groupId}/relations/graph`).then((r) => r.data), [groupId]);
  const graphQuery = useApiQuery(graphLoad, { resources: ["agent_relation", "agent_member_data", "agent_group_data"] });
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
  const typesQuery = useApiQuery(typesLoad, { resources: ["agent_relation"] });
  const typeOptions = Array.from(new Set([...RELATION_TYPE_PRESETS, ...(typesQuery.data ?? [])])).map((value) => ({ value, label: value }));
  const remove = async (id: string) => { if (readOnly) return; await api(`/agent/groups/${groupId}/relations/${id}`, { method: "DELETE" }); message.success("关系边已删除"); query.reload(); typesQuery.reload(); graphQuery.reload(); };
  const saveCreate = async (values: { subjectUserId: number; objectUserId: number; type: string; note: string; confidence: number }) => {
    if (readOnly) return;
    setSaving(true);
    try {
      await api<AgentRelationItem>(`/agent/groups/${groupId}/relations`, { method: "POST", body: JSON.stringify(values) });
      message.success("关系边已新增"); setCreating(false); createForm.resetFields(); query.reload(); typesQuery.reload(); graphQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) message.warning(error.message);
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };
  const openEdit = (row: AgentRelationItem) => { if (readOnly) return; setEditing(row); editForm.setFieldsValue({ note: row.note, confidence: row.confidence }); };
  const saveEdit = async (values: { note: string; confidence: number }) => {
    if (readOnly || !editing) return;
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
      {!readOnly && <Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增关系边</Button>}
    </Space>}>{
      view === "graph"
        ? (graphQuery.error && !graph
          ? <QueryErrorAlert error={graphQuery.error} onRetry={graphQuery.reload} />
          : graph
            ? <Suspense fallback={<div className="rg-loading-wrap"><Spin /></div>}><LazyRelationGraphView graph={graph} typeFilter={typeFilter} readOnly={readOnly} onEditRelation={readOnly ? undefined : openEdit} onDeleteRelation={readOnly ? undefined : (edge) => remove(edge.id)} /></Suspense>
            : <div className="rg-loading-wrap"><Spin /></div>)
        : (query.error && !query.data
          ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
          : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: <Empty description="暂无关系记忆" /> }} columns={readOnly ? [
            { title: "主体", dataIndex: "subjectUserId", render: renderMemberCell },
            { title: "客体", dataIndex: "objectUserId", render: renderMemberCell },
            { title: "类型", dataIndex: "type", render: (value: string) => <Tag color={relationTypeColor(value)}>{value}</Tag> },
            { title: "备注", dataIndex: "note", ellipsis: true, render: (value: string) => value || <Text type="secondary">—</Text> },
            { title: "置信度", render: (_, row: AgentRelationItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "最后见到", dataIndex: "lastSeenAt", render: formatTime, width: 170 },
          ] : [
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
    {!readOnly && <><Drawer open={creating} width={520} title="新增关系边" onClose={() => setCreating(false)}>
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
    </Drawer></>}
  </>;
}
