import { Alert, App as AntApp, Button, Card, Empty, Popconfirm, Table, Tag } from "antd";
import { api } from "../api";
import { formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { PrivacyItem } from "../types";

export function PrivacyPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({
    queryKey: ["agent-privacy", groupId],
    fetcher: (signal) => api<PrivacyItem[]>(`/agent/groups/${groupId}/privacy?pageSize=100`, { signal }).then((r) => r.data),
    invalidation: { resources: ["agent_privacy", "agent_group_data"], scope: { groupId } },
  });
  const toggle = async (userId: string, optedOut: boolean) => {
    if (query.stale) return;
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
      : <Table rowKey="userId" loading={query.loading} dataSource={query.data ?? []} locale={{ emptyText: <Empty description="暂无成员隐私记录" /> }} columns={[{ title: "用户 ID", dataIndex: "userId" }, { title: "状态", dataIndex: "optedOut", render: (value: boolean) => <Tag color={value ? "orange" : "green"}>{value ? "已退出记忆" : "已恢复"}</Tag> }, { title: "更新时间", dataIndex: "updatedAt", render: formatTime }, { title: "操作", render: (_, row: PrivacyItem) => row.optedOut ? <Button type="link" disabled={query.stale} onClick={() => toggle(row.userId, false)}>恢复记忆</Button> : <Popconfirm title={`让成员 ${row.userId} 退出 Agent 记忆？`} description="将立即清除其已沉淀的记忆、关系与消息，不可撤销。" onConfirm={() => toggle(row.userId, true)}><Button type="link" danger disabled={query.stale}>退出记忆</Button></Popconfirm> }]} />
  }</Card>;
}
