import { Card, Select, Spin } from "antd";
import { useState } from "react";
import { AgentAuditTable } from "../agent-audit-table";
import { api } from "../api";
import { QueryErrorAlert, TablePagination, useApiQuery } from "../shared";
import type { AgentAudit } from "../types";

const RESULT_OPTIONS = [
  { value: "", label: "全部结果" },
  { value: "success", label: "成功" },
  { value: "failed", label: "失败" },
];

export function AgentAuditsPanel({ groupId }: { groupId: string }): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [result, setResult] = useState("");
  const query = useApiQuery({
    queryKey: ["agent-audits", groupId, page, result],
    fetcher: (signal) => api<AgentAudit[]>(
      `/agent/audits?groupId=${groupId}&page=${page}&pageSize=20&result=${result}`,
      { signal },
    ).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
  });
  return <Card extra={<Select value={result} onChange={(value) => { setResult(value); setPage(1); }} options={RESULT_OPTIONS} style={{ width: 120 }} />}>{
    query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Spin spinning={query.loading}>
        <AgentAuditTable data={query.data?.rows ?? []} />
        <TablePagination current={page} total={query.data?.total ?? 0} onChange={setPage} />
      </Spin>
  }</Card>;
}
