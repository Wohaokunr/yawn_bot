import { Table, Tag } from "antd";
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
          render: (value: string) => (
            <Tag color={value === "success" ? "green" : "red"}>{value}</Tag>
          ),
        },
        { title: "详情", dataIndex: "detail", ellipsis: true },
      ]}
    />
  );
}
