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

export function PrivacyPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const load = useCallback(() => api<PrivacyItem[]>(`/agent/groups/${groupId}/privacy?pageSize=100`).then((r) => r.data), [groupId]);
  const query = useApiQuery(load, { resources: ["agent_privacy", "agent_group_data"] });
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
