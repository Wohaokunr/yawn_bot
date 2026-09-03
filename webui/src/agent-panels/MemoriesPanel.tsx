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

export interface MemoryFormValues {
  content: string;
  salience: number;
  confidence: number;
  expiresInDays: number | null;
}

// 记忆编辑抽屉：记忆表格与成员画像面板共用，只改内容/权重/置信度/有效期。
export function MemoryEditDrawer({ memory, saving, onClose, onSave }: { memory: MemoryItem | null; saving: boolean; onClose: () => void; onSave: (values: MemoryFormValues) => void }): React.JSX.Element {
  const [form] = Form.useForm();
  useEffect(() => {
    if (memory) {
      form.setFieldsValue({
        content: memory.content,
        salience: memory.salience,
        confidence: memory.confidence,
        expiresInDays: memory.expiresAt ? Math.max(1, Math.ceil((new Date(memory.expiresAt).getTime() - Date.now()) / 86400000)) : null,
      });
    }
  }, [form, memory]);
  return <Drawer open={!!memory} width={520} title={`编辑记忆 · ${memory?.key ?? ""}`} onClose={onClose}>
    <Form form={form} layout="vertical" onFinish={(values) => onSave(values as MemoryFormValues)}>
      <Form.Item name="content" label="内容" rules={[{ required: true, message: "请输入内容" }]}><Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} maxLength={2000} showCount /></Form.Item>
      <Row gutter={16}>
        <Col span={12}><Form.Item name="salience" label="显著度（注入优先级）" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
        <Col span={12}><Form.Item name="confidence" label="置信度" rules={[{ required: true }]}><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
      </Row>
      <Form.Item name="expiresInDays" label="有效期（天）"><InputNumber min={1} max={3650} placeholder="清空则永久有效" style={{ width: "100%" }} /></Form.Item>
      <Space><Button type="primary" htmlType="submit" loading={saving}>保存</Button><Button onClick={onClose}>取消</Button></Space>
    </Form>
  </Drawer>;
}

export function MemoriesPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1); const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [createForm] = Form.useForm();
  const load = useCallback(() => api<MemoryItem[]>(`/agent/groups/${groupId}/memories?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search]);
  const query = useApiQuery(load, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const statusLoad = useCallback(
    () => readOnly
      ? Promise.resolve(null)
      : api<AgentMemoryStatus>(`/agent/groups/${groupId}/memories/status`).then((r) => r.data),
    [groupId, readOnly],
  );
  const statusQuery = useApiQuery(statusLoad, { resources: ["agent_memory", "agent_member_data", "agent_group_data"] });
  const status = readOnly ? null : statusQuery.data;
  const remove = async (id: string) => { if (readOnly) return; await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" }); message.success("记忆已删除"); query.reload(); };
  const removeMember = async (userId: string) => { if (readOnly) return; const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/members/${userId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条成员数据`); query.reload(); };
  const removeGroup = async () => { if (readOnly) return; const result = await api<{ deleted: number }>(`/agent/groups/${groupId}/data`, { method: "DELETE" }); message.success(`已清理 ${result.data.deleted} 条群 Agent 数据`); query.reload(); };
  const exportData = async () => { if (readOnly) return; const result = await api(`/agent/groups/${groupId}/memories/export`); const blob = new Blob([JSON.stringify(result.data, null, 2)], { type: "application/json" }); const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `yawnbot-agent-${groupId}.json`; anchor.click(); URL.revokeObjectURL(url); };
  const compact = async () => {
    if (readOnly) return;
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
    if (readOnly) return;
    try {
      await api(`/agent/groups/${groupId}/memories/rebuild`, { method: "POST" });
      message.success("派生记忆重建已启动，手工记忆会保留");
      setTimeout(() => { statusQuery.reload(); query.reload(); }, 3000);
    } catch (error) {
      message.error((error as Error).message);
    }
  };
  const openEdit = (row: MemoryItem) => { if (!readOnly) setEditing(row); };
  const saveEdit = async (values: MemoryFormValues) => {
    if (readOnly || !editing) return;
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
    if (readOnly) return;
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
  const pageRows = query.data?.rows ?? [];
  const pageTypeCounts = pageRows.reduce<Record<string, number>>((counts, row) => {
    counts[row.type] = (counts[row.type] ?? 0) + 1;
    return counts;
  }, {});
  return <>
    {readOnly ? (
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={12} md={6}><Card size="small"><Statistic title="可查看记忆" value={query.data?.total ?? (query.loading ? "—" : 0)} suffix="条" /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="本页显示" value={pageRows.length} suffix="条" /></Card></Col>
        <Col xs={24} md={12}><Card size="small"><Space wrap size={[8, 8]}>{Object.entries(pageTypeCounts).map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {count}</Tag>)}{!query.loading && pageRows.length === 0 && <Text type="secondary">暂无记忆</Text>}</Space></Card></Col>
      </Row>
    ) : <>
      <Row gutter={[12, 12]} className="section-row">
        <Col xs={12} md={6}><Card size="small"><Statistic title="有效记忆" value={status?.total ?? "—"} suffix="条" /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="待整理消息" value={status?.pendingMessages ?? "—"} suffix="条" /></Card></Col>
        <Col xs={24} md={12}><Card size="small"><div className="ag-stat-line">
          <Space wrap size={[8, 8]}>{Object.entries(status?.countsByType ?? {}).map(([type, count]) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {count}</Tag>)}{status && Object.keys(status.countsByType).length === 0 && <Text type="secondary">暂无记忆</Text>}</Space>
          <Text type="secondary">最后成功：{status?.lastSuccessAt ? formatTime(status.lastSuccessAt) : "尚未整理"}</Text>
        </div></Card></Col>
      </Row>
      {status && !status.runtimeEnabled && <Alert type="info" showIcon message="Agent 总开关已关闭，自动记忆已暂停" description="新群消息不会进入 Agent 记忆采集，定时整理也不会运行；已有记忆仍可查看、导出或手工维护。" className="section-alert" />}
      {status?.lastError && <Alert type="error" showIcon closable message={`最近整理失败（连续 ${status.consecutiveFailures} 次）`} description={status.lastError} className="section-alert" />}
      {status?.rebuildRequired && <Alert type="warning" showIcon message="派生记忆正在重建" description="系统会按连续批次处理保留期内原始消息；手工记忆不会被覆盖。" className="section-alert" />}
    </>}
    <Card title="公开/群级记忆" extra={readOnly ? undefined : <Space><Button type="primary" onClick={() => { setCreating(true); createForm.resetFields(); }}>新增记忆</Button><Popconfirm title="立即整理本群记忆？" description="含 LLM 摘要，将在后台运行数十秒。" onConfirm={compact}><Button loading={status?.inFlight}>立即整理</Button></Popconfirm><Popconfirm title="重建全部自动派生记忆？" description="保留手工记忆，清除自动摘要/画像/关系后从短期消息重新生成。" onConfirm={rebuild}><Button>重建派生记忆</Button></Popconfirm><Button onClick={exportData}>导出 JSON</Button><Popconfirm title="清理整个群的消息、记忆、关系和媒体缓存？" description="此操作还会重置上下文游标，且不可撤销。" onConfirm={removeGroup}><DangerActionButton>清理全群 Agent 数据</DangerActionButton></Popconfirm></Space>}><Input.Search className="table-search" placeholder="搜索 key 或内容" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table
          rowKey="id"
          loading={query.loading}
          dataSource={query.data?.rows ?? []}
          pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }}
          expandable={readOnly ? undefined : {
            expandedRowRender: (row: MemoryItem) => <Space orientation="vertical" size={6}>
              <Paragraph copyable style={{ marginBottom: 0 }}>{row.content}</Paragraph>
              <Text type="secondary">证据 {row.provenance.evidenceCount} 条 · 首次观察 {formatTime(row.provenance.firstObservedAt)} · 最近确认 {formatTime(row.provenance.lastConfirmedAt)}</Text>
              {row.evidenceMessageIds.length > 0 && <Text type="secondary" copyable={{ text: row.evidenceMessageIds.join(",") }}>证据消息：{row.evidenceMessageIds.join(", ")}</Text>}
            </Space>,
          }}
          columns={readOnly ? [
            { title: "记忆", render: (_, row: MemoryItem) => <Space orientation="vertical" size={4}><Space wrap><Text strong>{row.key}</Text><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag></Space><Paragraph style={{ marginBottom: 0 }}>{row.content}</Paragraph></Space> },
            { title: "归属成员", dataIndex: "subjectUserId", width: 150, render: (value?: string | null) => value ? <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSearchParams({ tab: "profiles", userId: value }, { replace: true })}>{value}</Button> : "群级" },
            { title: "置信度", width: 150, render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" /> },
            { title: "更新时间", dataIndex: "updatedAt", width: 180, render: formatTime },
          ] : [
            { title: "记忆", render: (_, row: MemoryItem) => <><Text strong>{row.key}</Text><br /><Tag color={MEMORY_TYPE_META[row.type]?.color}>{memoryTypeLabel(row.type)}</Tag><Text type="secondary"> · {row.visibility} · {row.sourceKind === "manual" ? "手工" : "自动"}</Text></> },
            { title: "成员", dataIndex: "subjectUserId", render: (value?: string) => value ? <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setSearchParams({ tab: "profiles", userId: value }, { replace: true })}>{value}</Button> : "群级" },
            { title: "权重", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.salience * 100)} size="small" /> },
            { title: "置信度", render: (_, row: MemoryItem) => <Progress percent={Math.round(row.confidence * 100)} size="small" strokeColor="var(--ant-color-success)" /> },
            { title: "有效期至", dataIndex: "expiresAt", render: (value?: string | null) => value ? formatTime(value) : "永久" },
            { title: "更新时间", dataIndex: "updatedAt", render: formatTime },
            { title: "操作", render: (_, row: MemoryItem) => <Space><Button type="link" onClick={() => openEdit(row)}>编辑</Button><Popconfirm title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" danger>删除</Button></Popconfirm>{row.subjectUserId && <Popconfirm title={`清理成员 ${row.subjectUserId} 的全部 Agent 数据？`} onConfirm={() => removeMember(row.subjectUserId!)}><Button type="link" danger>清理成员</Button></Popconfirm>}</Space> },
          ]}
        />
    }</Card>
    {!readOnly && <><MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />
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
    </Drawer></>}
  </>;
}

// 画像分组展示顺序：core 为反复确认晋升的不过期事实，置前展示。
