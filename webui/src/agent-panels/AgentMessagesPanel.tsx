import { Alert, Button, Card, Input, Select, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentMessageItem } from "../types";
import { MEMORY_ROLE_OPTIONS } from "../agent-meta";

const { Text, Paragraph } = Typography;

export function AgentMessagesPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const query = useApiQuery({
    queryKey: ["agent-messages", groupId, page, search, role],
    fetcher: (signal) => api<AgentMessageItem[]>(
      `/agent/groups/${groupId}/messages?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&role=${role}`,
      { signal },
    ).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
  });

  const openDebugger = (messageId: string): void => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "debug");
    next.set("debug.messageId", messageId);
    setSearchParams(next, { replace: true });
  };

  return <Card title="短期消息库" extra={<Select value={role} onChange={(value) => { setRole(value); setPage(1); }} options={MEMORY_ROLE_OPTIONS} style={{ width: 120 }} />}>
    <Alert type="info" showIcon className="section-alert" message="仅保留 rawRetentionDays 内的原始消息；隐私退出成员的消息不在此展示，到期由整理任务清除。" />
    <Input.Search className="table-search" placeholder="搜索消息内容或昵称" allowClear onSearch={(v) => { setSearch(v); setPage(1); }} />{
      query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : <Table rowKey="id" loading={query.loading} dataSource={query.data?.rows ?? []} pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} expandable={{ expandedRowRender: (row) => <Paragraph copyable>{row.text}</Paragraph> }} columns={[{ title: "时间", dataIndex: "receivedAt", render: formatTime, width: 170 }, { title: "成员", render: (_, row: AgentMessageItem) => <>{row.senderName || "—"}<br /><Text type="secondary" copyable>{row.userId}</Text></> }, { title: "角色", dataIndex: "role", width: 90, render: (value: string) => <Tag color={value === "bot" ? "blue" : value === "owner" ? "gold" : value === "admin" ? "cyan" : "default"}>{value}</Tag> }, { title: "内容", dataIndex: "text", ellipsis: true }, { title: "操作", width: 80, render: (_, row: AgentMessageItem) => row.role === "bot" ? null : <Button type="link" size="small" disabled={query.stale} onClick={() => openDebugger(row.messageId)}>调试</Button> }]} />
    }</Card>;
}
