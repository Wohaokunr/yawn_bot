import { Empty, Input, Select, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { scheduleApi } from "./schedule-api";
import { formatDurationMs, formatLocal, MODE_META, RESULT_FILTERS, resultColor } from "./schedule-meta";
import { traceLink } from "./TasksTab";
import type { ScheduleRunItem } from "./types";

export function RunsTab({ onOpenTask }: { onOpenTask: (taskId: number) => void }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const group = searchParams.get("schedules.runGroup") ?? "";
  const result = searchParams.get("schedules.result") ?? "";
  const [draftGroup, setDraftGroup] = useState(group);
  useEffect(() => setDraftGroup(group), [group]);
  const patch = (values: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, values), { replace: true });

  const runsQuery = useApiQuery<{ ready: boolean; items: ScheduleRunItem[] }>({
    queryKey: ["schedule-runs", group, result],
    fetcher: (signal) => scheduleApi.runs({ group, result, limit: 200 }, signal),
    invalidation: { resources: ["schedules"] },
  });

  const columns: ColumnsType<ScheduleRunItem> = [
    {
      title: "时间",
      dataIndex: "at",
      width: 170,
      render: (value: string, row) => (
        <div className="sch-task-cell">
          <span>{formatLocal(value)}</span>
          {row.planned && row.planned !== value && <span className="sbx-muted">计划 {formatLocal(row.planned)}</span>}
        </div>
      ),
    },
    {
      title: "任务",
      key: "task",
      render: (_, row) => (
        <div className="sch-task-cell">
          <button type="button" className="sch-link" onClick={() => onOpenTask(row.taskId)}>#{row.taskId} {row.title}</button>
          <span className="sbx-muted">{row.groupName ?? row.groupId} · {MODE_META[row.mode]?.label ?? row.mode}</span>
        </div>
      ),
    },
    {
      title: "结果",
      key: "result",
      width: 170,
      render: (_, row) => <Tag color={resultColor(row.result)}>{row.label ?? row.result}</Tag>,
    },
    {
      title: "说明",
      key: "detail",
      render: (_, row) => (
        <div className="sch-task-cell">
          {row.preview && <span className="sch-history-preview">{row.preview}</span>}
          {row.error && <span className="sch-bad">{row.error}</span>}
          <span className="sbx-muted">
            {row.ms != null ? formatDurationMs(row.ms) : ""}
            {row.late ? ` · 迟到 ${row.late} 分钟` : ""}
            {row.trace && <> · <Link to={traceLink(row.groupId, row.trace)}>执行轨迹</Link></>}
          </span>
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="sbx-toolbar">
        <Input
          allowClear
          value={draftGroup}
          onChange={(event) => setDraftGroup(event.target.value)}
          onPressEnter={() => patch({ "schedules.runGroup": draftGroup.trim() || null })}
          onBlur={() => draftGroup.trim() !== group && patch({ "schedules.runGroup": draftGroup.trim() || null })}
          placeholder="群号"
          style={{ width: 130 }}
          aria-label="按群筛选运行记录"
        />
        <Select value={result} onChange={(value) => patch({ "schedules.result": value || null })} options={RESULT_FILTERS} style={{ width: 170 }} aria-label="按结果筛选" />
        <span className="sbx-muted">每个任务保留最近若干条记录（策略里可调）</span>
      </div>
      {runsQuery.error && <QueryErrorAlert error={runsQuery.error} onRetry={runsQuery.reload} />}
      <Table
        size="small"
        rowKey={(row) => `${row.taskId}-${row.at}-${row.result}`}
        loading={runsQuery.loading}
        columns={columns}
        dataSource={runsQuery.data?.items ?? []}
        pagination={{ pageSize: 30, hideOnSinglePage: true, showSizeChanger: false }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有运行记录" /> }}
        scroll={{ x: 760 }}
      />
    </div>
  );
}
