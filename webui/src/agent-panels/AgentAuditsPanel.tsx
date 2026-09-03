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

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

export function AgentAuditsPanel({ groupId }: { groupId: string }): React.JSX.Element {
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
