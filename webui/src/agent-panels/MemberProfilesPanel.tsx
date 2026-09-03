import {
  Alert, App as AntApp, AutoComplete, Button, Card, Empty, List, Popconfirm,
  Progress, Space, Spin, Table, Tag, Typography,
} from "antd";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { MemoryItem, MemorySubjectItem } from "../types";
import { MEMORY_TYPE_META, memberDisplayName, memoryTypeLabel, profileKeyLabel } from "../agent-meta";
import { MemoryEditDrawer, type MemoryFormValues } from "./MemoriesPanel";

const { Text, Paragraph } = Typography;
const PROFILE_TYPE_ORDER = ["core", "profile", "manual"];

export function MemberProfilesPanel({ groupId, readOnly = false }: { groupId: string; readOnly?: boolean }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const userId = searchParams.get("profiles.userId") ?? "";
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setDraft(userId); }, [userId]);

  const setUserId = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set("profiles.userId", value); else next.delete("profiles.userId");
    setSearchParams(next, { replace: true });
  };

  const subjectsQuery = useApiQuery({
    queryKey: ["agent-memory-subjects", groupId],
    fetcher: (signal) => api<MemorySubjectItem[]>(`/agent/groups/${groupId}/memories/subjects`, { signal }).then((r) => r.data),
    invalidation: {
      resources: ["agent_memory", "agent_member_data", "agent_group_data"],
      scope: { groupId },
    },
  });
  const subjects = subjectsQuery.data ?? [];
  const memberQuery = useApiQuery({
    queryKey: ["agent-member-profile", groupId, userId],
    fetcher: (signal) => userId
      ? api<MemoryItem[]>(`/agent/groups/${groupId}/memories?subjectUserId=${userId}&pageSize=100`, { signal }).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 }))
      : Promise.resolve({ rows: [] as MemoryItem[], total: 0 }),
    invalidation: {
      resources: ["agent_memory", "agent_member_data", "agent_group_data"],
      scope: { groupId },
    },
  });
  const rows = memberQuery.data?.rows ?? [];
  const grouped = PROFILE_TYPE_ORDER
    .map((type) => ({ type, items: rows.filter((row) => row.type === type) }))
    .filter((group) => group.items.length > 0);
  const counts = { profile: 0, core: 0, manual: 0 } as Record<string, number>;
  for (const row of rows) counts[row.type] = (counts[row.type] ?? 0) + 1;
  const subject = subjects.find((item) => item.userId === userId);
  const memberOptions = subjects.map((item) => ({
    value: item.userId,
    label: `${memberDisplayName(item.groupNickname, item.nickname, item.userId)}（${item.userId}）· 画像 ${item.counts.profile} / 核心 ${item.counts.core}`,
  }));

  const remove = async (id: string) => {
    if (readOnly || memberQuery.transitioning) return;
    await api(`/agent/groups/${groupId}/memories/${id}`, { method: "DELETE" });
    message.success("记忆已删除");
    memberQuery.reload(); subjectsQuery.reload();
  };
  const saveEdit = async (values: MemoryFormValues) => {
    if (readOnly || !editing || memberQuery.transitioning) return;
    setSaving(true);
    try {
      await api<MemoryItem>(`/agent/groups/${groupId}/memories/${editing.id}`, { method: "PUT", body: JSON.stringify({ ...values, version: editing.updatedAt }) });
      message.success("记忆已更新");
      setEditing(null);
      memberQuery.reload(); subjectsQuery.reload();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { message.warning(error.message); memberQuery.reload(); subjectsQuery.reload(); setEditing(null); }
      else message.error((error as Error).message);
    } finally { setSaving(false); }
  };

  return <>
    <Card title="成员画像" extra={<Space wrap>
      <AutoComplete
        value={draft}
        options={memberOptions}
        onChange={(value) => setDraft(value)}
        onSelect={(value) => setUserId(String(value))}
        onInputKeyDown={(event) => { if (event.key === "Enter" && draft.trim()) setUserId(draft.trim()); }}
        filterOption={(input, option) => `${String(option?.value ?? "")} ${String(option?.label ?? "")}`.toLowerCase().includes(input.toLowerCase())}
        placeholder="输入或选择成员 QQ"
        style={{ width: 320 }}
        allowClear
      />
      <Button type="primary" disabled={!draft.trim()} onClick={() => setUserId(draft.trim())}>查看画像</Button>
      {userId && <Button onClick={() => setUserId("")}>返回列表</Button>}
    </Space>}>
      <Alert type="info" showIcon className="section-alert" message={readOnly ? "这里只展示允许公开的成员画像；已退出记忆治理的成员不会出现。" : "画像由记忆整理自动生成，也可在「记忆」页手工新增；已退出记忆（隐私）的成员不展示。"} />
      {userId
        ? (memberQuery.error && !memberQuery.data
          ? <QueryErrorAlert error={memberQuery.error} onRetry={memberQuery.reload} />
          : <>
            <div className="ag-stat-line" style={{ marginBottom: 12 }}>
              <Space wrap size={[8, 8]}>
                <Text strong>{memberDisplayName(subject?.groupNickname, subject?.nickname, userId)}</Text>
                <Text type="secondary" copyable>{userId}</Text>
                {PROFILE_TYPE_ORDER.map((type) => <Tag key={type} color={MEMORY_TYPE_META[type]?.color}>{memoryTypeLabel(type)} × {counts[type] ?? 0}</Tag>)}
              </Space>
              <Text type="secondary">最近更新：{rows[0] ? formatTime(rows[0].updatedAt) : "—"}</Text>
            </div>
            {memberQuery.data && memberQuery.data.total > rows.length && <Alert type="warning" showIcon className="section-alert" message={`该成员共 ${memberQuery.data.total} 条记录，仅展示最近 100 条`} />}
            {memberQuery.loading
              ? <Spin />
              : rows.length === 0
                ? <Empty description="该成员暂无画像" />
                : grouped.map((group) => <Card key={group.type} size="small" className="section-row" title={<Space size={8}><Tag color={MEMORY_TYPE_META[group.type]?.color}>{memoryTypeLabel(group.type)}</Tag><Text type="secondary">{group.items.length} 条</Text></Space>}>
                  <List dataSource={group.items} renderItem={(row) => (
                    <List.Item actions={readOnly ? undefined : [
                      <Button key="edit" type="link" size="small" disabled={memberQuery.stale} onClick={() => setEditing(row)}>编辑</Button>,
                      <Popconfirm key="remove" title="删除这一条记忆？" onConfirm={() => remove(row.id)}><Button type="link" size="small" danger disabled={memberQuery.stale}>删除</Button></Popconfirm>,
                    ]}>
                      <List.Item.Meta
                        title={<Space wrap size={[8, 4]}>
                          <Text strong>{profileKeyLabel(row.key)}</Text>
                          {row.key !== profileKeyLabel(row.key) && <Text type="secondary">{row.key}</Text>}
                          <Text type="secondary">{readOnly ? `更新 ${formatTime(row.updatedAt)}` : `${row.sourceKind === "manual" ? "手工" : "自动"} · ${row.expiresAt ? `有效期至 ${formatTime(row.expiresAt)}` : "永久"} · 更新 ${formatTime(row.updatedAt)}`}</Text>
                        </Space>}
                        description={<>
                          <Paragraph copyable style={{ marginBottom: 8 }}>{row.content}</Paragraph>
                          <Space wrap size={[16, 4]}>
                            <Space size={6}>置信度<Progress percent={Math.round(row.confidence * 100)} size="small" style={{ width: 90 }} strokeColor="var(--ant-color-success)" /></Space>
                            {!readOnly && <Space size={6}>显著度<Progress percent={Math.round(row.salience * 100)} size="small" style={{ width: 90 }} /></Space>}
                          </Space>
                        </>}
                      />
                    </List.Item>
                  )} />
                </Card>)}
          </>)
        : (subjectsQuery.error && !subjectsQuery.data
          ? <QueryErrorAlert error={subjectsQuery.error} onRetry={subjectsQuery.reload} />
          : <Table rowKey="userId" loading={subjectsQuery.loading} dataSource={subjects} pagination={{ pageSize: 20, showSizeChanger: false }} locale={{ emptyText: <Empty description="暂无成员画像" /> }} columns={[
            { title: "成员", render: (_, row: MemorySubjectItem) => <>{memberDisplayName(row.groupNickname, row.nickname, row.userId)}<br /><Text type="secondary" copyable>{row.userId}</Text></> },
            { title: "成员画像", dataIndex: ["counts", "profile"], width: 100 },
            { title: "核心记忆", dataIndex: ["counts", "core"], width: 100 },
            { title: "置顶事实", dataIndex: ["counts", "manual"], width: 100 },
            { title: "最近更新", dataIndex: "updatedAt", render: formatTime, width: 170 },
            { title: "操作", width: 110, render: (_, row: MemorySubjectItem) => <Button type="link" disabled={subjectsQuery.stale} onClick={() => setUserId(row.userId)}>查看画像</Button> },
          ]} />)}
    </Card>
    {!readOnly && <MemoryEditDrawer memory={editing} saving={saving} onClose={() => setEditing(null)} onSave={saveEdit} />}
  </>;
}
