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

export function AgentMessagesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1); const [search, setSearch] = useState(""); const [role, setRole] = useState("");
  const load = useCallback(() => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&role=${role}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })), [groupId, page, search, role]);
  const query = useApiQuery(load);
  return <Card title="短期消息库" extra={<Select value={role} onChange={(value) => { setRole(value); setPage(1); }} options={MEMORY_ROLE_OPTIONS} style={{ width: 120 }} />}>
    <Alert type="info" showIcon className="section-alert" message="仅保留 rawRetentionDays 内的原始消息；隐私退出成员的消息不在此展示，到期由整理任务清除。" />
    <Input.Search className="table-search" placeholder="搜索消息内容或昵称" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.text}</Paragraph> }} columns={[{ title: "时间", dataIndex: "receivedAt", render: formatTime, width: 170 }, { title: "成员", render: (_, row: AgentMessageItem) => <>{row.senderName || "—"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role", width: 90, render: (value: string) => <Tag color={value === "bot" ? "blue" : value === "owner" ? "gold" : value === "admin" ? "cyan" : "default"}>{value}</Tag> }, { title: "内容", dataIndex: "text", ellipsis: true }, { title: "操作", width: 80, render: (_, row: AgentMessageItem) => row.role === "bot" ? null : <Button type="link" size="small" onClick={() => setSearchParams({ tab: "debug", messageId: row.messageId }, { replace: true })}>调试</Button> }]} />
    }</Card>;
}
