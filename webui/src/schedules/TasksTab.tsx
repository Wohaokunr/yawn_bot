import {
  CaretRightOutlined,
  DeleteOutlined,
  EditOutlined,
  PauseOutlined,
  PlusOutlined,
  SearchOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Descriptions, Drawer, Empty, Input, Popconfirm, Select, Skeleton, Space, Table, Tag, Timeline, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { scheduleApi } from "./schedule-api";
import type { BulkAction, TaskAction } from "./schedule-api";
import {
  formatDurationMs,
  formatLocal,
  MODE_META,
  relativeTo,
  resultColor,
  sourceLabel,
  STATUS_FILTERS,
  STATUS_META,
} from "./schedule-meta";
import { TaskEditor } from "./TaskEditor";
import type { ScheduleStatusData, ScheduleTask, ScheduleTaskDetail, ScheduleTasksPage } from "./types";

const { Paragraph } = Typography;
const PAGE_SIZE = 20;

export function traceLink(groupId: string, traceId: string): string {
  return `/agent/${groupId}?tab=debug&debug.trace=${encodeURIComponent(traceId)}`;
}

function NextRun({ task, now }: { task: ScheduleTask; now: string }): React.JSX.Element {
  if (!task.nextRunAt) return <span className="sbx-muted">{task.statusReason ?? "—"}</span>;
  return (
    <span className="sch-next">
      <span>{formatLocal(task.nextRunAt)}</span>
      <small>{relativeTo(task.nextRunAt, now)}</small>
    </span>
  );
}

function RowActions({ task, onAction, onEdit }: { task: ScheduleTask; onAction: (task: ScheduleTask, action: TaskAction) => void; onEdit: (task: ScheduleTask) => void }): React.JSX.Element {
  const open = task.status === "active" || task.status === "paused";
  return (
    <span className="sbx-row-actions" onClick={(event) => event.stopPropagation()}>
      {open && (
        <Tooltip title="编辑">
          <Button size="small" type="text" icon={<EditOutlined />} aria-label={`编辑任务 ${task.id}`} onClick={() => onEdit(task)} />
        </Tooltip>
      )}
      {task.status === "active" && (
        <Tooltip title="暂停">
          <Button size="small" type="text" icon={<PauseOutlined />} aria-label={`暂停任务 ${task.id}`} onClick={() => onAction(task, "pause")} />
        </Tooltip>
      )}
      {task.status === "paused" && (
        <Tooltip title="恢复">
          <Button size="small" type="text" icon={<CaretRightOutlined />} aria-label={`恢复任务 ${task.id}`} onClick={() => onAction(task, "resume")} />
        </Tooltip>
      )}
      {open && (
        <Popconfirm title="取消这个定时任务？" description="取消后不会再执行，记录会保留。" onConfirm={() => onAction(task, "cancel")} okText="取消任务" cancelText="再想想">
          <Tooltip title="取消任务">
            <Button size="small" type="text" danger icon={<StopOutlined />} aria-label={`取消任务 ${task.id}`} />
          </Tooltip>
        </Popconfirm>
      )}
    </span>
  );
}

export function TaskDetailDrawer({
  taskId,
  now,
  onClose,
  onEdit,
  onChanged,
}: {
  taskId: number | null;
  now: string;
  onClose: () => void;
  onEdit: (task: ScheduleTaskDetail) => void;
  onChanged: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<ScheduleTaskDetail | null>({
    queryKey: ["schedule-task", taskId],
    fetcher: (signal) => (taskId ? scheduleApi.task(taskId, signal) : Promise.resolve(null)),
    invalidation: { resources: ["schedules"] },
    keepPreviousData: false,
  });
  const [busy, setBusy] = useState<string | null>(null);
  const task = query.data;

  const act = async (action: TaskAction | "delete") => {
    if (!task) return;
    setBusy(action);
    try {
      if (action === "delete") {
        await scheduleApi.remove(task.id);
        message.success("已删除");
        onChanged();
        onClose();
        return;
      }
      const result = await scheduleApi.action(task.id, action);
      if (action === "run") message.success("已开始执行，结果稍后出现在运行记录里");
      else if (result.changed === false) message.info("状态没有变化");
      onChanged();
      query.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const open = task?.status === "active" || task?.status === "paused";
  return (
    <Drawer
      open={taskId !== null}
      onClose={onClose}
      width="min(760px, 100%)"
      title={task ? `#${task.id} ${task.title}` : "定时任务"}
      className="sch-detail"
      extra={task && (
        <Space wrap>
          {open && <Button icon={<EditOutlined />} onClick={() => onEdit(task)}>编辑</Button>}
          <Popconfirm title="现在就执行一次？" description="不影响原来的计划；会真的发到群里。" onConfirm={() => void act("run")} okText="执行" cancelText="算了">
            <Button icon={<CaretRightOutlined />} loading={busy === "run"}>立即执行</Button>
          </Popconfirm>
          {task.status === "active" && <Button icon={<PauseOutlined />} loading={busy === "pause"} onClick={() => void act("pause")}>暂停</Button>}
          {task.status === "paused" && <Button icon={<CaretRightOutlined />} loading={busy === "resume"} onClick={() => void act("resume")}>恢复</Button>}
          {open ? (
            <Popconfirm title="取消这个定时任务？" onConfirm={() => void act("cancel")} okText="取消任务" cancelText="再想想">
              <Button danger icon={<StopOutlined />} loading={busy === "cancel"}>取消任务</Button>
            </Popconfirm>
          ) : (
            <Popconfirm title="永久删除这个任务和它的记录？" onConfirm={() => void act("delete")} okText="删除" cancelText="算了">
              <Button danger icon={<DeleteOutlined />} loading={busy === "delete"}>删除</Button>
            </Popconfirm>
          )}
        </Space>
      )}
    >
      {query.error && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {!task ? <Skeleton active loading={query.loading} /> : (
        <>
          {task.status === "paused" && task.statusReason && <Alert className="sbx-gap" type="warning" showIcon message={`已暂停：${task.statusReason}`} />}
          {task.status === "failed" && <Alert className="sbx-gap" type="error" showIcon message={task.statusReason ?? "任务已失败"} />}
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="状态"><Tag color={STATUS_META[task.status].color}>{STATUS_META[task.status].label}</Tag></Descriptions.Item>
            <Descriptions.Item label="方式">{MODE_META[task.mode].label}</Descriptions.Item>
            <Descriptions.Item label="群">{task.groupName ? `${task.groupName}（${task.groupId}）` : task.groupId}</Descriptions.Item>
            <Descriptions.Item label="创建者">{task.creatorName ?? task.creatorUserId}{task.mentionCreator ? " · 到点 @" : ""}</Descriptions.Item>
            <Descriptions.Item label="规则" span={2}>{task.description}</Descriptions.Item>
            <Descriptions.Item label="下次">{task.nextRunAt ? `${formatLocal(task.nextRunAt)}（${relativeTo(task.nextRunAt, now)}）` : "—"}</Descriptions.Item>
            <Descriptions.Item label="次数">
              已执行 {task.runCount}{task.maxRuns ? ` / ${task.maxRuns}` : ""}
              {task.skipCount > 0 && <span className="sbx-muted"> · 跳过 {task.skipCount}</span>}
              {task.failureCount > 0 && <span className="sch-bad"> · 连续失败 {task.failureCount}</span>}
            </Descriptions.Item>
            <Descriptions.Item label="来源">{sourceLabel(task.sourceKind)}</Descriptions.Item>
            <Descriptions.Item label="创建于">{formatLocal(task.createdAt)}</Descriptions.Item>
            {task.cancelledAt && <Descriptions.Item label="取消于" span={2}>{formatLocal(task.cancelledAt)}{task.cancelledBy ? ` · 由 ${task.cancelledBy}` : " · 管理台"}</Descriptions.Item>}
          </Descriptions>
          <h4 className="sbx-subhead">{task.mode === "agent" ? "到点交给 Agent 的指令" : "提醒内容"}</h4>
          <Paragraph className="sch-instruction" copyable>{task.instruction}</Paragraph>
          {task.lastOutput && (
            <>
              <h4 className="sbx-subhead">上次发到群里的内容</h4>
              <Paragraph className="sch-output" type="secondary">{task.lastOutput}</Paragraph>
            </>
          )}
          {task.upcoming.length > 0 && (
            <>
              <h4 className="sbx-subhead">接下来</h4>
              <div className="sch-upcoming">
                {task.upcoming.map((item) => <Tag key={item}>{formatLocal(item)}</Tag>)}
              </div>
            </>
          )}
          <h4 className="sbx-subhead">运行记录</h4>
          {task.history.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有执行过" /> : (
            <Timeline
              items={task.history.map((entry) => ({
                color: resultColor(entry.result) === "default" ? "gray" : resultColor(entry.result),
                children: (
                  <div className="sch-history-item">
                    <div>
                      <Tag color={resultColor(entry.result)}>{entry.label ?? entry.result}</Tag>
                      <span>{formatLocal(entry.at)}</span>
                      {entry.planned && entry.planned !== entry.at && <span className="sbx-muted">（计划 {formatLocal(entry.planned)}）</span>}
                      {entry.late ? <span className="sbx-muted"> · 迟到 {entry.late} 分钟</span> : null}
                      {entry.ms != null && <span className="sbx-muted"> · {formatDurationMs(entry.ms)}</span>}
                      {entry.trace && <Link to={traceLink(task.groupId, entry.trace)} className="sch-trace-link">执行轨迹</Link>}
                    </div>
                    {entry.preview && <div className="sch-history-preview">{entry.preview}</div>}
                    {entry.error && <div className="sch-bad">{entry.error}</div>}
                  </div>
                ),
              }))}
            />
          )}
        </>
      )}
    </Drawer>
  );
}

export function TasksTab({ status, agentModeAllowed }: { status: ScheduleStatusData; agentModeAllowed: boolean }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const group = searchParams.get("schedules.group") ?? "";
  const statusFilter = searchParams.get("schedules.status") ?? "open";
  const mode = searchParams.get("schedules.mode") ?? "";
  const q = searchParams.get("schedules.q") ?? "";
  const page = Math.max(1, Number(searchParams.get("schedules.page") ?? "1") || 1);
  const openId = Number(searchParams.get("schedules.task") ?? "") || null;
  const [draftQ, setDraftQ] = useState(q);
  const [draftGroup, setDraftGroup] = useState(group);
  const [selected, setSelected] = useState<number[]>([]);
  const [editor, setEditor] = useState<{ open: boolean; task: ScheduleTaskDetail | null }>({ open: false, task: null });

  useEffect(() => setDraftQ(q), [q]);
  useEffect(() => setDraftGroup(group), [group]);
  const patch = (values: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, values), { replace: true });

  const tasksQuery = useApiQuery<ScheduleTasksPage>({
    queryKey: ["schedule-tasks", group, statusFilter, mode, q, page],
    fetcher: (signal) => scheduleApi.tasks({ group, status: statusFilter === "all" ? "" : statusFilter, mode, q, page, pageSize: PAGE_SIZE }, signal),
    invalidation: { resources: ["schedules"] },
  });
  const data = tasksQuery.data;
  useEffect(() => setSelected([]), [group, statusFilter, mode, q, page]);

  const runAction = async (task: ScheduleTask, action: TaskAction) => {
    try {
      const result = await scheduleApi.action(task.id, action);
      if (result.changed === false) message.info("状态没有变化");
      tasksQuery.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    }
  };

  const openEditor = async (task: ScheduleTask | null) => {
    if (!task) {
      setEditor({ open: true, task: null });
      return;
    }
    try {
      setEditor({ open: true, task: await scheduleApi.task(task.id) });
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载任务失败");
    }
  };

  const bulk = (action: BulkAction) => {
    const labels: Record<BulkAction, string> = { pause: "暂停", resume: "恢复", cancel: "取消", delete: "永久删除" };
    modal.confirm({
      title: `${labels[action]}选中的 ${selected.length} 个任务？`,
      okButtonProps: { danger: action === "cancel" || action === "delete" },
      onOk: async () => {
        try {
          const result = await scheduleApi.bulk(selected, action);
          message.success(`已${labels[action]} ${result.changed} 个`);
          setSelected([]);
          tasksQuery.reload();
        } catch (error) {
          message.error(error instanceof Error ? error.message : "操作失败");
        }
      },
    });
  };

  const columns: ColumnsType<ScheduleTask> = [
    {
      title: "任务",
      key: "title",
      render: (_, task) => (
        <div className="sch-task-cell">
          <strong>{task.title}</strong>
          <span className="sbx-muted">{task.description}</span>
        </div>
      ),
    },
    {
      title: "群",
      key: "group",
      width: 160,
      render: (_, task) => (
        <div className="sch-task-cell">
          <span>{task.groupName ?? task.groupId}</span>
          <span className="sbx-muted">{task.creatorName ?? task.creatorUserId}</span>
        </div>
      ),
    },
    {
      title: "方式",
      dataIndex: "mode",
      width: 96,
      render: (value: ScheduleTask["mode"]) => <Tag color={value === "agent" ? "magenta" : "blue"}>{MODE_META[value].label}</Tag>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (value: ScheduleTask["status"], task) => (
        <Tooltip title={task.statusReason ?? undefined}>
          <Tag color={STATUS_META[value].color}>{STATUS_META[value].label}</Tag>
        </Tooltip>
      ),
    },
    { title: "下次执行", key: "next", width: 190, render: (_, task) => <NextRun task={task} now={status.now} /> },
    {
      title: "上次",
      key: "last",
      width: 150,
      responsive: ["lg"],
      render: (_, task) => task.lastRunAt ? (
        <div className="sch-task-cell">
          <Tag color={resultColor(task.lastResult)}>{task.lastResultLabel ?? task.lastResult}</Tag>
          <span className="sbx-muted">{relativeTo(task.lastRunAt, status.now)} · 共 {task.runCount} 次</span>
        </div>
      ) : <span className="sbx-muted">还没执行</span>,
    },
    { title: "", key: "actions", width: 120, render: (_, task) => <RowActions task={task} onAction={(row, action) => void runAction(row, action)} onEdit={(row) => void openEditor(row)} /> },
  ];

  return (
    <div className="sch-tasks">
      <div className="sbx-toolbar">
        <Input.Search
          allowClear
          value={draftQ}
          onChange={(event) => setDraftQ(event.target.value)}
          onSearch={(value) => patch({ "schedules.q": value.trim() || null, "schedules.page": null })}
          placeholder="搜标题或指令"
          prefix={<SearchOutlined />}
          style={{ width: 220 }}
          aria-label="搜索任务"
        />
        <Input
          allowClear
          value={draftGroup}
          onChange={(event) => setDraftGroup(event.target.value)}
          onPressEnter={() => patch({ "schedules.group": draftGroup.trim() || null, "schedules.page": null })}
          onBlur={() => draftGroup.trim() !== group && patch({ "schedules.group": draftGroup.trim() || null, "schedules.page": null })}
          placeholder="群号"
          style={{ width: 130 }}
          aria-label="按群筛选"
        />
        <Select
          value={statusFilter}
          onChange={(value) => patch({ "schedules.status": value === "open" ? null : value || "all", "schedules.page": null })}
          options={STATUS_FILTERS.map((item) => ({ value: item.value || "all", label: item.label }))}
          style={{ width: 170 }}
          aria-label="按状态筛选"
        />
        <Select
          value={mode}
          onChange={(value) => patch({ "schedules.mode": value || null, "schedules.page": null })}
          options={[{ value: "", label: "全部方式" }, { value: "agent", label: MODE_META.agent.label }, { value: "notify", label: MODE_META.notify.label }]}
          style={{ width: 120 }}
          aria-label="按方式筛选"
        />
        <span className="sbx-toolbar-spacer" />
        {selected.length > 0 && (
          <Space.Compact>
            <Button onClick={() => bulk("pause")}>暂停</Button>
            <Button onClick={() => bulk("resume")}>恢复</Button>
            <Button danger onClick={() => bulk("cancel")}>取消</Button>
            <Button danger onClick={() => bulk("delete")}>删除</Button>
          </Space.Compact>
        )}
        <Button type="primary" icon={<PlusOutlined />} onClick={() => void openEditor(null)}>新建任务</Button>
      </div>
      {tasksQuery.error && <QueryErrorAlert error={tasksQuery.error} onRetry={tasksQuery.reload} />}
      <Table
        size="middle"
        rowKey="id"
        loading={tasksQuery.loading}
        columns={columns}
        dataSource={data?.items ?? []}
        rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(Number)) }}
        onRow={(task) => ({ onClick: () => patch({ "schedules.task": String(task.id) }), className: task.failureCount > 0 ? "sbx-row-failed sch-row" : "sch-row" })}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total: data?.total ?? 0,
          showSizeChanger: false,
          hideOnSinglePage: true,
          onChange: (next) => patch({ "schedules.page": next > 1 ? String(next) : null }),
        }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={q || group || mode || statusFilter !== "open" ? "没有符合条件的任务" : "还没有定时任务。群友可以直接对 Bot 说“每天早上 8 点提醒我打卡”，也可以在这里新建。"}
            />
          ),
        }}
        scroll={{ x: 860 }}
      />
      <TaskDetailDrawer
        taskId={openId}
        now={status.now}
        onClose={() => patch({ "schedules.task": null })}
        onEdit={(task) => setEditor({ open: true, task })}
        onChanged={tasksQuery.reload}
      />
      <TaskEditor
        open={editor.open}
        task={editor.task}
        defaultGroupId={group || undefined}
        agentModeAllowed={agentModeAllowed}
        onClose={() => setEditor({ open: false, task: null })}
        onSaved={(task) => {
          setEditor({ open: false, task: null });
          tasksQuery.reload();
          patch({ "schedules.task": String(task.id) });
        }}
      />
    </div>
  );
}
