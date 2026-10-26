import { HistoryOutlined, ReloadOutlined, SafetyCertificateOutlined, UnorderedListOutlined } from "@ant-design/icons";
import { Alert, Button, Skeleton, Tabs, Tag, Tooltip } from "antd";
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { scheduleApi } from "./schedule-api";
import { formatLocal, parseScheduleTab, relativeTo, resultTone } from "./schedule-meta";
import type { ScheduleTab } from "./schedule-meta";
import { PolicyTab } from "./PolicyTab";
import { RunsTab } from "./RunsTab";
import { TasksTab } from "./TasksTab";
import type { ScheduleSettings, ScheduleStatusData } from "./types";

const STATUS_POLL_MS = 30_000;

export function ScheduleStatusBar({ status, refreshing, onRefresh }: { status: ScheduleStatusData; refreshing: boolean; onRefresh: () => void }): React.JSX.Element {
  const tone = !status.ready ? "error" : status.enabled ? "ok" : "warn";
  const next = status.upcoming[0];
  const failedToday = Object.entries(status.today.results).filter(([key]) => resultTone(key) === "error").reduce((sum, [, value]) => sum + value, 0);
  const label = !status.ready ? "数据表未迁移" : status.enabled ? "运行中" : "已关闭";
  const detail = !status.ready
    ? "执行 nb orm upgrade 后刷新"
    : next
      ? `下一个：${formatLocal(next.nextRunAt)}（${relativeTo(next.nextRunAt, status.now)}）· ${next.groupName ?? next.groupId} ·「${next.title}」`
      : "当前没有待执行的任务";
  return (
    <section className={`sbx-status is-${tone}`} aria-label="定时任务状态">
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>定时任务 · {label}</strong>
          <span title={detail}>{detail}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        <Tag color={status.counts.active ? "green" : undefined}>运行中 {status.counts.active ?? 0}</Tag>
        <Tag color={status.counts.paused ? "gold" : undefined}>已暂停 {status.counts.paused ?? 0}</Tag>
        <Tooltip title="有进行中任务的群">
          <Tag>{status.groups} 个群</Tag>
        </Tooltip>
        <Tooltip title={`北京时间 ${status.today.day ?? "今天"} 起计；跳过/推迟不计入`}>
          <Tag color={status.today.runs ? "processing" : undefined}>今日执行 {status.today.runs}</Tag>
        </Tooltip>
        {failedToday > 0 && <Tag color="red">今日失败 {failedToday}</Tag>}
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={onRefresh}>刷新</Button>
      </div>
    </section>
  );
}

export function SchedulesPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseScheduleTab(searchParams.get("schedules.tab"));
  const statusQuery = useApiQuery<ScheduleStatusData>({
    queryKey: ["schedule-status"],
    fetcher: (signal) => scheduleApi.status(signal),
    invalidation: { resources: ["schedules"] },
  });
  // 策略决定新建表单能不能选“唤醒 Agent”；页面级取一次，保存策略后经 hub 刷新。
  const settingsQuery = useApiQuery<{ settings: ScheduleSettings; defaults: ScheduleSettings }>({
    queryKey: ["schedule-settings"],
    fetcher: (signal) => scheduleApi.settings(signal),
    invalidation: { resources: ["schedules"] },
  });
  const { reload } = statusQuery;

  // “下一个”是相对时间，轻量轮询让它别过期；后台标签页不刷新。
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const setTab = (next: ScheduleTab, extra: Record<string, string | null> = {}) => {
    setSearchParams(updateSearchParams(searchParams, { "schedules.tab": next === "tasks" ? null : next, ...extra }), { replace: true });
  };

  const status = statusQuery.data;
  if (!status) {
    return (
      <div className="sbx-page">
        <h2 className="sbx-title">定时任务</h2>
        {statusQuery.error ? <QueryErrorAlert error={statusQuery.error} onRetry={reload} /> : <Skeleton active />}
      </div>
    );
  }

  return (
    <div className="sbx-page">
      <div className="sbx-heading">
        <h2 className="sbx-title">定时任务</h2>
        <p>群友在聊天里让 Agent 设的提醒和定时工作：到点直接提醒，或唤醒 Agent 去查资料、跑代码再回复</p>
      </div>
      <ScheduleStatusBar status={status} refreshing={statusQuery.refreshing} onRefresh={reload} />
      {!status.ready && (
        <Alert
          className="section-alert"
          type="warning"
          showIcon
          message="定时任务的数据表还没有创建"
          description="在 Bot 目录执行 nb orm upgrade 迁移数据库后刷新本页；在那之前 Agent 的定时任务工具会提示“暂不可用”。"
        />
      )}
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as ScheduleTab)}
        destroyOnHidden
        items={[
          {
            key: "tasks",
            label: <span><UnorderedListOutlined /> 任务</span>,
            children: <TasksTab status={status} agentModeAllowed={settingsQuery.data?.settings.allowAgentMode ?? true} />,
          },
          {
            key: "runs",
            label: <span><HistoryOutlined /> 运行记录</span>,
            children: <RunsTab onOpenTask={(taskId) => setTab("tasks", { "schedules.task": String(taskId) })} />,
          },
          { key: "policy", label: <span><SafetyCertificateOutlined /> 策略与限额</span>, children: <PolicyTab /> },
        ]}
      />
    </div>
  );
}
