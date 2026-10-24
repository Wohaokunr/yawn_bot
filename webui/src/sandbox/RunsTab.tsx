import { CopyOutlined, DownloadOutlined, SearchOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Descriptions, Drawer, Empty, Input, Select, Skeleton, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { ActiveRunsTable } from "./OverviewTab";
import { sandboxApi, sandboxFileUrl } from "./sandbox-api";
import {
  fileKindMeta,
  formatBytes,
  formatDuration,
  formatEpoch,
  formatLimit,
  inlineMediaKind,
  relativeEpoch,
  RUN_STATUS_OPTIONS,
  runSourceLabel,
  runStatusMeta,
  updateSearchParams,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxRunDetail, SandboxRunFile, SandboxRunSummary, SandboxRunsPage, SandboxWorkspace } from "./types";

const { Paragraph } = Typography;

export function OutputBlock({ label, text, tone }: { label: string; text: string; tone?: "error" }): React.JSX.Element | null {
  const { message } = AntApp.useApp();
  if (!text) return null;
  return (
    <div className={`sbx-output ${tone === "error" ? "is-error" : ""}`}>
      <div className="sbx-output-head">
        <span>{label}</span>
        <Button
          size="small"
          type="text"
          icon={<CopyOutlined />}
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(() => message.success("已复制"), () => message.error("复制失败"));
          }}
        >复制</Button>
      </div>
      <pre>{text}</pre>
    </div>
  );
}

export function RunFiles({ workspace, files }: { workspace: string; files: SandboxRunFile[] }): React.JSX.Element | null {
  if (!files.length) return null;
  return (
    <div className="sbx-run-files">
      {files.map((file) => {
        const media = inlineMediaKind(file.path, file.kind);
        const kind = fileKindMeta(file.kind);
        return (
          <figure key={file.path} className="sbx-run-file">
            {media === "image" && <img src={sandboxFileUrl(workspace, file.path)} alt={file.path} loading="lazy" />}
            {media === "audio" && <audio controls preload="none" src={sandboxFileUrl(workspace, file.path)} />}
            {media === "video" && <video controls preload="none" src={sandboxFileUrl(workspace, file.path)} />}
            <figcaption>
              <span>{kind.emoji} {file.path}</span>
              <small>{formatBytes(file.size)}{file.created === false ? " · 修改" : ""}</small>
              <a href={sandboxFileUrl(workspace, file.path, true)} aria-label={`下载 ${file.path}`}><DownloadOutlined /></a>
            </figcaption>
          </figure>
        );
      })}
    </div>
  );
}

function RunDetailDrawer({ runId, onClose }: { runId: string | null; onClose: () => void }): React.JSX.Element {
  const query = useApiQuery<SandboxRunDetail | null>({
    queryKey: ["sandbox-run", runId],
    fetcher: (signal) => (runId ? sandboxApi.run(runId, signal) : Promise.resolve(null)),
    keepPreviousData: false,
  });
  const detail = query.data;
  const meta = detail ? runStatusMeta(detail.status) : null;
  return (
    <Drawer open={!!runId} onClose={onClose} width="min(760px, 100%)" title={detail ? `执行 ${detail.runId}` : "执行详情"} className="sbx-run-drawer">
      {query.error && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
      {!detail ? <Skeleton active loading={query.loading} /> : (
        <>
          <Descriptions size="small" column={2} bordered>
            <Descriptions.Item label="状态"><Tag color={meta?.color}>{meta?.label}</Tag>{detail.exitCode != null && <span className="sbx-muted">退出码 {detail.exitCode}</span>}{detail.signal && <span className="sbx-muted"> {detail.signal}</span>}</Descriptions.Item>
            <Descriptions.Item label="工作区">{workspaceLabel(detail.workspace, detail.groupName)}</Descriptions.Item>
            <Descriptions.Item label="开始">{formatEpoch(detail.startedAt)}</Descriptions.Item>
            <Descriptions.Item label="来源">{runSourceLabel(detail.meta.source)}{detail.meta.userId ? ` · 成员 ${detail.meta.userId}` : ""}</Descriptions.Item>
            <Descriptions.Item label="耗时">{formatDuration(detail.durationMs)}{detail.queueMs > 50 ? `（排队 ${formatDuration(detail.queueMs)}）` : ""}</Descriptions.Item>
            <Descriptions.Item label="CPU / 内存峰值">{formatDuration(detail.cpuMs)} / {formatBytes(detail.maxRssKb * 1024)}</Descriptions.Item>
            <Descriptions.Item label="限额" span={2}>
              {Object.entries(detail.limits).map(([key, value]) => <Tag key={key}>{formatLimit(key, value)}</Tag>)}
            </Descriptions.Item>
            {detail.meta.purpose && <Descriptions.Item label="用途" span={2}>{detail.meta.purpose}</Descriptions.Item>}
          </Descriptions>
          {detail.message && <Alert className="sbx-gap" type={detail.status === "ok" ? "info" : "warning"} showIcon message={detail.message} />}
          <OutputBlock label={`代码（${detail.codeLines} 行${detail.codeTruncated ? "，已截断" : ""}）`} text={detail.code} />
          <OutputBlock label="标准输出" text={detail.stdout} />
          <OutputBlock label="错误输出" text={detail.stderr} tone="error" />
          {detail.files.length > 0 && (
            <>
              <h4 className="sbx-subhead">产物</h4>
              <Paragraph type="secondary" className="sbx-muted">文件可能已被后续执行修改或删除。</Paragraph>
              <RunFiles workspace={detail.workspace} files={detail.files} />
            </>
          )}
        </>
      )}
    </Drawer>
  );
}

export function RunsTab({ online }: { online: boolean }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const workspace = searchParams.get("sandbox.ws") ?? "";
  const statusFilter = searchParams.get("sandbox.status") ?? "";
  const q = searchParams.get("sandbox.q") ?? "";
  const openRun = searchParams.get("sandbox.run");
  const [draftQ, setDraftQ] = useState(q);
  const [pages, setPages] = useState<SandboxRunSummary[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => setDraftQ(q), [q]);
  const patch = (values: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, values), { replace: true });

  const runsQuery = useApiQuery<SandboxRunsPage | null>({
    queryKey: ["sandbox-runs", workspace, statusFilter, q, online],
    fetcher: (signal) => (online ? sandboxApi.runs({ workspace, status: statusFilter, q, limit: 50 }, signal) : Promise.resolve(null)),
    invalidation: { resources: ["sandbox"] },
  });
  const workspacesQuery = useApiQuery<SandboxWorkspace[]>({
    queryKey: ["sandbox-workspaces", online],
    fetcher: (signal) => (online ? sandboxApi.workspaces(signal) : Promise.resolve([])),
  });

  // 新的第一页到来时丢掉“加载更多”累积的旧页。
  useEffect(() => {
    setPages([]);
    setCursor(runsQuery.data?.nextBefore ?? null);
  }, [runsQuery.data]);

  const rows = useMemo(() => [...(runsQuery.data?.items ?? []), ...pages], [runsQuery.data, pages]);

  const loadMore = async () => {
    if (cursor == null) return;
    setLoadingMore(true);
    try {
      const next = await sandboxApi.runs({ workspace, status: statusFilter, q, before: cursor, limit: 50 });
      setPages((current) => [...current, ...next.items]);
      setCursor(next.nextBefore);
    } finally {
      setLoadingMore(false);
    }
  };

  const columns: ColumnsType<SandboxRunSummary> = [
    {
      title: "时间",
      dataIndex: "startedAt",
      width: 130,
      render: (value: number) => <Tooltip title={formatEpoch(value)}>{relativeEpoch(value)}</Tooltip>,
    },
    { title: "工作区", dataIndex: "workspace", width: 180, ellipsis: true, render: (value: string, row) => workspaceLabel(value, row.groupName) },
    {
      title: "代码",
      dataIndex: "preview",
      ellipsis: true,
      render: (value: string, row) => (
        <div className="sbx-run-preview">
          <code>{value || "（空）"}</code>
          <small>{runSourceLabel(row.meta.source)}{row.meta.purpose ? ` · ${row.meta.purpose}` : ""} · {row.codeLines} 行</small>
        </div>
      ),
    },
    {
      title: "结果",
      dataIndex: "status",
      width: 110,
      render: (value: string, row) => {
        const meta = runStatusMeta(value);
        return <Tooltip title={row.message || undefined}><Tag color={meta.color}>{meta.label}</Tag></Tooltip>;
      },
    },
    { title: "耗时", dataIndex: "durationMs", width: 90, render: (value: number) => formatDuration(value) },
    { title: "内存", dataIndex: "maxRssKb", width: 90, render: (value: number) => formatBytes(value * 1024) },
    { title: "产物", dataIndex: "fileCount", width: 70, render: (value: number) => (value ? <Tag>{value}</Tag> : <span className="sbx-muted">—</span>) },
  ];

  if (!online) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="沙箱离线，运行历史保存在沙箱容器里，恢复连接后可查看" />;
  }

  return (
    <div className="sbx-runs">
      <div className="sbx-toolbar">
        <Select
          allowClear
          showSearch
          placeholder="全部工作区"
          value={workspace || undefined}
          onChange={(value?: string) => patch({ "sandbox.ws": value ?? null })}
          style={{ minWidth: 200 }}
          optionFilterProp="label"
          options={(workspacesQuery.data ?? []).map((item) => ({ value: item.id, label: workspaceLabel(item.id, item.groupName) }))}
        />
        <Select
          allowClear
          placeholder="全部结果"
          value={statusFilter || undefined}
          onChange={(value?: string) => patch({ "sandbox.status": value ?? null })}
          style={{ minWidth: 130 }}
          options={RUN_STATUS_OPTIONS}
        />
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜代码、输出、用途或 ID"
          value={draftQ}
          onChange={(event) => {
            setDraftQ(event.target.value);
            if (!event.target.value) patch({ "sandbox.q": null });
          }}
          onPressEnter={() => patch({ "sandbox.q": draftQ.trim() || null })}
          style={{ maxWidth: 320 }}
        />
        <Button onClick={runsQuery.reload} loading={runsQuery.refreshing}>刷新</Button>
      </div>
      {runsQuery.error && <QueryErrorAlert error={runsQuery.error} onRetry={runsQuery.reload} />}
      {(runsQuery.data?.active.length ?? 0) > 0 && (
        <div className="sbx-active-strip">
          <strong>正在执行 {runsQuery.data?.active.length} 个</strong>
          <ActiveRunsTable runs={runsQuery.data?.active ?? []} onCancelled={runsQuery.reload} />
        </div>
      )}
      <Table
        size="small"
        rowKey="runId"
        loading={runsQuery.loading}
        columns={columns}
        dataSource={rows}
        pagination={false}
        rowClassName={(row) => (row.status === "ok" ? "" : "sbx-row-failed")}
        onRow={(row) => ({ onClick: () => patch({ "sandbox.run": row.runId }), style: { cursor: "pointer" } })}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={q || statusFilter || workspace ? "没有符合条件的执行" : "还没有人运行过代码"} /> }}
      />
      {cursor != null && (
        <div className="sbx-load-more">
          <Button onClick={() => void loadMore()} loading={loadingMore}>加载更早的记录</Button>
        </div>
      )}
      <RunDetailDrawer runId={openRun} onClose={() => patch({ "sandbox.run": null })} />
    </div>
  );
}
