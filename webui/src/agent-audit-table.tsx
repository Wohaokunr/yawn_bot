import { Table, Tag } from "antd";
import { Link } from "react-router-dom";
import { AUDIT_OUTCOME_META, auditOutcome, auditOutcomeLabel } from "./agent-meta";
import { formatTime } from "./shared";
import type { AgentAudit } from "./types";

export function AgentAuditTable({ data }: { data: AgentAudit[] }): React.JSX.Element {
  return (
    <Table
      rowKey="id"
      size="small"
      pagination={false}
      dataSource={data}
      columns={[
        { title: "时间", dataIndex: "createdAt", render: formatTime },
        { title: "群", dataIndex: "groupId" },
        { title: "工具", dataIndex: "toolName" },
        {
          title: "结果",
          dataIndex: "result",
          render: (value: string, row: AgentAudit) => (
            <Tag color={AUDIT_OUTCOME_META[auditOutcome(row)].color} title={value}>{auditOutcomeLabel(row)}</Tag>
          ),
        },
        { title: "详情", dataIndex: "detail", ellipsis: true },
        {
          title: "Trace",
          width: 80,
          render: (_value: unknown, row: AgentAudit) => row.traceId
            ? <Link to={`/agent/${row.groupId}?tab=debug&debug.trace=${encodeURIComponent(row.traceId)}`}>查看</Link>
            : <span>—</span>,
        },
      ]}
    />
  );
}
