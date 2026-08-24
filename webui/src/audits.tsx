import { Card, Select, Table, Tag, Typography } from "antd";
import { useCallback, useState } from "react";
import { api } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type { WebAudit } from "./types";

const { Text } = Typography;

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

export function WebAuditsPage(): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [result, setResult] = useState("");
  const load = useCallback(
    () => api<WebAudit[]>(`/web-audits?page=${page}&pageSize=20&result=${result}`)
      .then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [page, result],
  );
  const query = useApiQuery(load);

  return <>
    <PageHeader
      title="操作审计"
      subtitle="持久化记录 WebUI 配置修改与删除操作"
      extra={(
        <Select
          value={result}
          onChange={(value) => {
            setResult(value);
            setPage(1);
          }}
          options={RESULT_OPTIONS}
          style={{ width: 120 }}
        />
      )}
    />
    <Card>
      {query.error && !query.data
        ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
        : (
          <Table
            rowKey="id"
            loading={query.loading}
            dataSource={query.data?.rows ?? []}
            pagination={{
              current: page,
              pageSize: 20,
              total: query.data?.total ?? 0,
              showSizeChanger: false,
              onChange: setPage,
            }}
            expandable={{
              expandedRowRender: (row) => <pre>{JSON.stringify(row.detail, null, 2)}</pre>,
            }}
            columns={[
              { title: "时间", dataIndex: "createdAt", render: formatTime },
              { title: "动作", dataIndex: "action" },
              {
                title: "资源",
                render: (_, row: WebAudit) => <>
                  {row.resourceType}
                  <br />
                  <Text type="secondary">{row.resourceId || "—"}</Text>
                </>,
              },
              { title: "会话指纹", dataIndex: "actorSession" },
              {
                title: "结果",
                dataIndex: "result",
                render: (value: string) => (
                  <Tag color={value === "success" ? "green" : "red"}>{value}</Tag>
                ),
              },
              { title: "请求 ID", dataIndex: "requestId", ellipsis: true },
            ]}
          />
        )}
    </Card>
  </>;
}
