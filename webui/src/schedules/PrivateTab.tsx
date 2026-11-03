// 私聊提醒：每个人在私聊里（或群里说“到点私聊我”、或网页上）设的提醒。
// 内容是本人设的提醒原话，只给管理员排查用；管理员能暂停/恢复/删除（例如设错了、刷屏）。
import { CaretRightOutlined, DeleteOutlined, PauseOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Descriptions, Drawer, Empty, Input, Popconfirm, Select, Skeleton, Table, Tag, Timeline, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { scheduleApi } from "./schedule-api";
import type { PrivateAction } from "./schedule-api";
import { formatLocal, MODE_META, privateSourceMeta, privateStatusMeta, relativeTo, resultColor } from "./schedule-meta";
import type { PrivateReminder, PrivateReminderDetail, PrivateRemindersPage } from "./types";

const STATUS_OPTIONS = [
  { value: "open", label: "进行中（含暂停）" },
  { value: "active", label: "等着到点" },
  { value: "paused", label: "已暂停" },
  { value: "all", label: "全部（含已完成）" },
];
const SOURCE_OPTIONS = [
  { value: "", label: "全部来源" },
  { value: "private", label: "私聊里设的" },
  { value: "group", label: "群里设的" },
  { value: "webui", label: "网页上设的" },
];
const MODE_OPTIONS = [
  { value: "", label: "全部方式" },
  { value: "notify", label: "到点提醒" },
  { value: "agent", label: "到点替他去办" },
];

export function privateModeLabel(mode: string): string {
  return mode === "agent" ? "到点替他去办" : "到点提醒";
}

function PrivateActions({ item, onAction }: { item: PrivateReminder; onAction: (item: PrivateReminder, action: PrivateAction) => void }): React.JSX.Element {
  return (
    <span className="sbx-row-actions" onClick={(event) => event.stopPropagation()}>
      {item.status !== "paused" && item.status !== "completed" && (
        <Tooltip title="暂停">
          <Button size="small" type="text" icon={<PauseOutlined />} aria-label={`暂停私聊提醒 ${item.key}`} onClick={() => onAction(item, "pause")} />
        </Tooltip>
      )}
      {item.status === "paused" && (
        <Tooltip title="恢复">
          <Button size="small" type="text" icon={<CaretRightOutlined />} aria-label={`恢复私聊提醒 ${item.key}`} onClick={() => onAction(item, "resume")} />
        </Tooltip>
      )}
      <Popconfirm title="删除这条私聊提醒？" description="删除后不会再提醒，本人在私聊里也看不到了。" onConfirm={() => onAction(item, "cancel")} okText="删除" cancelText="再想想">
        <Tooltip title="删除">
          <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label={`删除私聊提醒 ${item.key}`} />
        </Tooltip>
      </Popconfirm>
    </span>
  );
}

function PrivateDetailBody({ userId, itemId, now }: { userId: string; itemId: number; now: string }): React.JSX.Element {
  const detail = useApiQuery<PrivateReminderDetail>({
    queryKey: ["schedule-private-detail", userId, itemId],
    fetcher: (signal) => scheduleApi.privateDetail(userId, itemId, signal),
    invalidation: { resources: ["schedules"] },
  });
  const item = detail.data;
  if (!item) return detail.error ? <QueryErrorAlert error={detail.error} onRetry={detail.reload} /> : <Skeleton active />;
  return (
    <>
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="编号">{item.ref} · QQ {item.userId}</Descriptions.Item>
        <Descriptions.Item label="内容">{item.title ? <><strong>{item.title}</strong>：</> : null}{item.content}</Descriptions.Item>
        <Descriptions.Item label="规则">{item.description}</Descriptions.Item>
        <Descriptions.Item label="方式">{privateModeLabel(item.mode)}</Descriptions.Item>
        <Descriptions.Item label="状态"><Tag color={privateStatusMeta(item.status).color}>{privateStatusMeta(item.status).label}</Tag></Descriptions.Item>
        <Descriptions.Item label="下次">{item.nextRunAt ? `${formatLocal(item.nextRunAt)}（${relativeTo(item.nextRunAt, now)}）` : "—"}</Descriptions.Item>
        {item.retryAt && <Descriptions.Item label="重试">{formatLocal(item.retryAt)}</Descriptions.Item>}
        <Descriptions.Item label="已提醒">{item.runCount} 次{item.maxRuns ? ` / 共 ${item.maxRuns} 次` : ""}</Descriptions.Item>
        <Descriptions.Item label="来源">
          <Tag color={privateSourceMeta(item.sourceKind).color}>{item.sourceLabel}</Tag>
          {item.originGroupId && <span className="sbx-muted">群 {item.originGroupId}</span>}
        </Descriptions.Item>
        <Descriptions.Item label="创建">{formatLocal(item.createdAt)}</Descriptions.Item>
      </Descriptions>
      <h4 className="sch-drawer-subtitle">最近几次</h4>
      {item.history.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没到过点" />
      ) : (
        <Timeline
          items={[...item.history].reverse().map((row) => ({
            color: resultColor(row.result) === "red" ? "red" : "green",
            children: (
              <div className="sch-task-cell">
                <span>{formatLocal(row.at)} <Tag color={resultColor(row.result)}>{row.label ?? row.result}</Tag></span>
                {row.late ? <span className="sbx-muted">迟到 {row.late} 分钟</span> : null}
              </div>
            ),
          }))}
        />
      )}
    </>
  );
}

function PrivateDetailDrawer({ target, now, onClose }: { target: { userId: string; itemId: number } | null; now: string; onClose: () => void }): React.JSX.Element {
  return (
    <Drawer open={!!target} onClose={onClose} title="私聊提醒" width={520} destroyOnHidden>
      {target && <PrivateDetailBody userId={target.userId} itemId={target.itemId} now={now} />}
    </Drawer>
  );
}

export function PrivateTab({ now }: { now: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const user = searchParams.get("schedules.pUser") ?? "";
  const statusFilter = searchParams.get("schedules.pStatus") ?? "open";
  const source = searchParams.get("schedules.pSource") ?? "";
  const mode = searchParams.get("schedules.pMode") ?? "";
  const q = searchParams.get("schedules.pq") ?? "";
  const openKey = searchParams.get("schedules.private") ?? "";
  const [draftUser, setDraftUser] = useState(user);
  const [draftQ, setDraftQ] = useState(q);
  useEffect(() => setDraftUser(user), [user]);
  useEffect(() => setDraftQ(q), [q]);
  const patch = (values: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, values), { replace: true });

  const list = useApiQuery<PrivateRemindersPage>({
    queryKey: ["schedule-private", user, statusFilter, source, mode, q],
    fetcher: (signal) => scheduleApi.privateList({ user, status: statusFilter, source, mode, q, limit: 300 }, signal),
    invalidation: { resources: ["schedules"] },
  });

  const act = async (item: PrivateReminder, action: PrivateAction) => {
    try {
      await scheduleApi.privateAction(item.userId, item.itemId, action);
      message.success(action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : "已删除");
      list.reload();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    }
  };

  const [openUser, openItem] = openKey.split(":");
  const target = openUser && openItem ? { userId: openUser, itemId: Number(openItem) } : null;

  const columns: ColumnsType<PrivateReminder> = [
    {
      title: "提醒",
      key: "content",
      render: (_, row) => (
        <div className="sch-task-cell">
          <span>{row.title ? <strong>{row.title}：</strong> : null}{row.content}</span>
          <span className="sbx-muted">QQ {row.userId} · {row.ref} · {row.description}</span>
        </div>
      ),
    },
    {
      title: "方式",
      dataIndex: "mode",
      width: 120,
      render: (value: string) => <Tooltip title={value === "agent" ? MODE_META.agent.hint.replace("Agent", "私聊的 Bot") : "到点把提醒原文私聊发给他"}><Tag color={value === "agent" ? "purple" : undefined}>{privateModeLabel(value)}</Tag></Tooltip>,
    },
    {
      title: "来源",
      key: "source",
      width: 140,
      render: (_, row) => (
        <div className="sch-task-cell">
          <Tag color={privateSourceMeta(row.sourceKind).color}>{privateSourceMeta(row.sourceKind).label}</Tag>
          {row.originGroupId && <span className="sbx-muted">群 {row.originGroupId}</span>}
        </div>
      ),
    },
    {
      title: "下次",
      key: "next",
      width: 160,
      render: (_, row) => row.nextRunAt
        ? <span className="sch-next"><span>{formatLocal(row.nextRunAt)}</span><small>{relativeTo(row.nextRunAt, now)}</small></span>
        : <Tag color={privateStatusMeta(row.status).color}>{privateStatusMeta(row.status).label}</Tag>,
    },
    {
      title: "已提醒",
      key: "runs",
      width: 110,
      render: (_, row) => (
        <div className="sch-task-cell">
          <span>{row.runCount}{row.maxRuns ? ` / ${row.maxRuns}` : ""} 次</span>
          {row.lastResult && <Tag color={resultColor(row.lastResult)}>{row.lastResultLabel ?? row.lastResult}</Tag>}
        </div>
      ),
    },
    { title: "", key: "actions", width: 100, render: (_, row) => <PrivateActions item={row} onAction={(item, action) => void act(item, action)} /> },
  ];

  const counts = list.data?.counts;
  return (
    <div>
      {counts && (
        <div className="sbx-status-chips sch-private-chips">
          <Tag color={counts.active ? "green" : undefined}>等着到点 {counts.active}</Tag>
          <Tag color={counts.paused ? "gold" : undefined}>已暂停 {counts.paused}</Tag>
          <Tag color={counts.agent ? "purple" : undefined}>替他去办 {counts.agent}</Tag>
          <Tag>群里设的 {counts.fromGroups}</Tag>
          <Tag>{counts.users} 人</Tag>
        </div>
      )}
      <div className="sbx-toolbar">
        <Input
          allowClear
          value={draftUser}
          onChange={(event) => setDraftUser(event.target.value)}
          onPressEnter={() => patch({ "schedules.pUser": draftUser.trim() || null })}
          onBlur={() => draftUser.trim() !== user && patch({ "schedules.pUser": draftUser.trim() || null })}
          placeholder="QQ 号"
          style={{ width: 130 }}
          aria-label="按 QQ 筛选私聊提醒"
        />
        <Select value={statusFilter} onChange={(value) => patch({ "schedules.pStatus": value === "open" ? null : value })} options={STATUS_OPTIONS} style={{ width: 160 }} aria-label="按状态筛选私聊提醒" />
        <Select value={source} onChange={(value) => patch({ "schedules.pSource": value || null })} options={SOURCE_OPTIONS} style={{ width: 130 }} aria-label="按来源筛选私聊提醒" />
        <Select value={mode} onChange={(value) => patch({ "schedules.pMode": value || null })} options={MODE_OPTIONS} style={{ width: 140 }} aria-label="按方式筛选私聊提醒" />
        <Input.Search
          allowClear
          value={draftQ}
          onChange={(event) => setDraftQ(event.target.value)}
          onSearch={(value) => patch({ "schedules.pq": value.trim() || null })}
          placeholder="搜内容"
          style={{ width: 180 }}
          aria-label="搜索私聊提醒"
        />
      </div>
      {list.error && <QueryErrorAlert error={list.error} onRetry={list.reload} />}
      <Table
        size="small"
        rowKey="key"
        loading={list.loading}
        columns={columns}
        dataSource={list.data?.items ?? []}
        onRow={(row) => ({ onClick: () => patch({ "schedules.private": row.key }), className: "sch-row-clickable" })}
        pagination={{ pageSize: 30, hideOnSinglePage: true, showSizeChanger: false }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有人设私聊提醒" /> }}
        scroll={{ x: 860 }}
      />
      <PrivateDetailDrawer target={target} now={now} onClose={() => patch({ "schedules.private": null })} />
    </div>
  );
}
