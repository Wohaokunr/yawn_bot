import { CloudServerOutlined, ProjectOutlined, RobotOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, Button, Collapse, Empty, Popconfirm, Progress, Segmented, Skeleton, Tag, Timeline, Typography } from "antd";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type HelperState, type HelperTask, spaceApi } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { formatBytes, OPEN_TASK_STATES, TASK_STATE_COLOR } from "./me-space-meta";
import { MeSection, StatTile, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text, Paragraph } = Typography;

function TaskBody({ task }: { task: HelperTask }): React.JSX.Element {
  const last = task.progress.at(-1);
  return <div className="me-stack">
    {task.goal && <Paragraph><Text type="secondary">要做的事：</Text>{task.goal}</Paragraph>}
    {task.question && task.state === "blocked" && <Alert type="warning" showIcon message="它在等你回答" description={<>{task.question}<br /><Text type="secondary">直接在私聊里回复 Bot 就行。</Text></>} />}
    {last?.step && last.total ? <Progress percent={Math.round((last.step / last.total) * 100)} size="small" format={() => `${last.step}/${last.total}`} /> : null}
    {task.progress.length > 0 && (
      <Timeline
        className="me-timeline"
        items={task.progress.map((row, index) => ({ key: index, children: <span><Text type="secondary" className="me-hint me-mono">{(row.at ?? "").slice(11, 16)}</Text> {row.note}</span> }))}
      />
    )}
    {task.summary && <Paragraph className="me-recap"><Text strong>结果：</Text>{task.summary}</Paragraph>}
    {task.error && <Alert type="error" showIcon message="没做成" description={task.error} />}
    {task.files.length > 0 && <div className="me-chip-row">{task.files.map((file) => <Tag key={file}>📎 {file}</Tag>)}</div>}
    <Text type="secondary" className="me-hint">建于 {formatQqTime(task.createdAt)}{task.startedAt ? ` · 开始 ${formatQqTime(task.startedAt)}` : ""}{task.finishedAt ? ` · 结束 ${formatQqTime(task.finishedAt)}` : ""}{task.toolsets.length ? ` · 用到：${task.toolsets.join("、")}` : ""}</Text>
  </div>;
}

export function MyTasksPage(): React.JSX.Element {
  const { query, data, setData } = useResource<HelperState>(["me", "tasks"], () => spaceApi.tasks());
  const { busy, run } = useAction();
  const [filter, setFilter] = useState<"open" | "all">("all");
  const tasks = useMemo(() => (data?.tasks ?? []).filter((task) => filter === "all" || OPEN_TASK_STATES.has(task.state)), [data, filter]);
  const open = (data?.tasks ?? []).filter((task) => OPEN_TASK_STATES.has(task.state)).length;

  return <div className="me-page">
    <PageHeader title="后台小助手" subtitle="在私聊里让 Bot “去查一下/整理一下/写个东西”时，它会派小助手在后台做，做完私聊告诉你。" onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : <>
      {!data.tasksEnabled && <Alert type="info" showIcon message="管理员没有开启后台小助手" />}
      <div className="me-stat-row">
        <StatTile label="进行中" value={open} tone={open ? "info" : undefined} sub={`同时最多 ${data.taskLimits.perUser} 个`} />
        <StatTile label="今天派过" value={`${data.tasksToday}/${data.taskLimits.daily}`} />
        <StatTile label="单个任务最长" value={`${Math.round(data.taskLimits.timeoutSeconds / 60)} 分钟`} />
        {data.computer && <StatTile label="我的虚拟电脑" value={`${data.computer.runsToday}/${data.computer.dailyLimit}`} sub={`今天运行次数 · 收件箱 ${formatBytes(data.computer.inboxBytes)}`} />}
      </div>

      <MeSection icon={<RobotOutlined />} title="任务" extra={<Segmented size="small" value={filter} onChange={setFilter} options={[{ value: "all", label: `全部 ${data.tasks.length}` }, { value: "open", label: `进行中 ${open}` }]} />}>
        {tasks.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={filter === "open" ? "没有进行中的任务" : "还没有派过任务。可以在私聊里说“帮我在后台查一下……”"} /> : (
          <Collapse
            className="me-task-collapse"
            items={tasks.map((task) => ({
              key: task.id,
              label: <span className="me-task-head"><Tag color={TASK_STATE_COLOR[task.state] ?? "default"}>{task.status}</Tag><strong>{task.title || task.goal || task.id}</strong><Text type="secondary" className="me-hint me-mono">{task.id}</Text></span>,
              extra: OPEN_TASK_STATES.has(task.state) ? (
                <span onClick={(event) => event.stopPropagation()} role="presentation">
                  <Popconfirm title="取消这个任务？" okText="取消任务" okButtonProps={{ danger: true }} onConfirm={async () => { const next = await run(task.id, () => spaceApi.cancelTask(task.id), "已取消"); if (next) setData(next); }}>
                    <Button size="small" danger icon={<StopOutlined />} loading={busy === task.id}>取消</Button>
                  </Popconfirm>
                </span>
              ) : null,
              children: <TaskBody task={task} />,
            }))}
          />
        )}
      </MeSection>

      {data.projects.length > 0 && (
        <MeSection icon={<ProjectOutlined />} title="多人项目" hint="一件大事拆给几个小助手分工做。">
          <ul className="me-plain-list">
            {data.projects.map((project) => (
              <li key={project.id}><strong>{project.title || project.id}</strong><span className="me-plain-main"><Text type="secondary">{project.members.length} 个小助手</Text></span><Tag>{project.status ?? "—"}</Tag></li>
            ))}
          </ul>
        </MeSection>
      )}
      {data.computer && (
        <Alert type="info" showIcon icon={<CloudServerOutlined />} message={`虚拟电脑 ${data.computer.workspace}`} description="私聊里发给 Bot 的文件会放进这台电脑的收件箱，小助手可以用它跑代码、处理文件。" />
      )}
    </>}
  </div>;
}
