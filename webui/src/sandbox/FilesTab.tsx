import { DeleteOutlined, DownloadOutlined, EyeOutlined, UploadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Checkbox, Drawer, Empty, Input, Popconfirm, Progress, Skeleton, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { DangerActionButton, QueryErrorAlert, useApiQuery } from "../shared";
import { sandboxApi, sandboxFileUrl, uploadSandboxFile } from "./sandbox-api";
import {
  canPreviewText,
  fileKindMeta,
  formatBytes,
  inlineMediaKind,
  relativeEpoch,
  formatEpoch,
  updateSearchParams,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxFileEntry, SandboxFileListing, SandboxStatus, SandboxWorkspace } from "./types";

function FilePreviewDrawer({
  workspace,
  file,
  onClose,
}: {
  workspace: string;
  file: SandboxFileEntry | null;
  onClose: () => void;
}): React.JSX.Element {
  const textual = file ? canPreviewText(file.path, file.kind) : false;
  const media = file ? inlineMediaKind(file.path, file.kind) : null;
  const query = useApiQuery<{ text: string; truncated: boolean } | null>({
    queryKey: ["sandbox-preview", workspace, file?.path, textual],
    fetcher: (signal) => (file && textual ? sandboxApi.preview(workspace, file.path, signal) : Promise.resolve(null)),
    keepPreviousData: false,
  });
  return (
    <Drawer
      open={!!file}
      onClose={onClose}
      width="min(820px, 100%)"
      title={file ? `${fileKindMeta(file.kind).emoji} ${file.path}` : "预览"}
      extra={file && <Button icon={<DownloadOutlined />} href={sandboxFileUrl(workspace, file.path, true)}>下载</Button>}
    >
      {file && (
        <div className="sbx-preview">
          <p className="sbx-muted">{formatBytes(file.size)} · {file.mime} · 修改于 {formatEpoch(file.mtime)}</p>
          {media === "image" && <img src={sandboxFileUrl(workspace, file.path)} alt={file.path} />}
          {media === "audio" && <audio controls src={sandboxFileUrl(workspace, file.path)} />}
          {media === "video" && <video controls src={sandboxFileUrl(workspace, file.path)} />}
          {textual && (query.error
            ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
            : query.data
              ? <>
                <pre className="sbx-preview-text">{query.data.text}</pre>
                {query.data.truncated && <p className="sbx-muted">只显示前 200KB，完整内容请下载。</p>}
              </>
              : <Skeleton active />)}
          {!media && !textual && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这种文件不能在页面里预览，请下载查看" />}
        </div>
      )}
    </Drawer>
  );
}

function WorkspaceList({
  workspaces,
  selected,
  quotaMb,
  onSelect,
}: {
  workspaces: SandboxWorkspace[];
  selected: string;
  quotaMb: number;
  onSelect: (id: string) => void;
}): React.JSX.Element {
  const [filter, setFilter] = useState("");
  const rows = workspaces.filter((item) => {
    const needle = filter.trim().toLowerCase();
    return !needle || item.id.includes(needle) || (item.groupName ?? "").toLowerCase().includes(needle);
  });
  return (
    <aside className="sbx-ws-list" aria-label="工作区">
      <Input.Search allowClear size="small" placeholder="找群名或群号" value={filter} onChange={(event) => setFilter(event.target.value)} />
      {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有工作区" />}
      {rows.map((item) => {
        const percent = Math.min(100, Math.round((item.bytes / (quotaMb * 1024 * 1024)) * 100));
        return (
          <button type="button" key={item.id} className={`sbx-ws-item ${item.id === selected ? "is-active" : ""}`} onClick={() => onSelect(item.id)}>
            <span className="sbx-ws-name">
              {workspaceLabel(item.id, item.groupName)}
              {item.running && <Tag color="processing">执行中</Tag>}
            </span>
            <Progress percent={percent} size="small" showInfo={false} status={percent > 90 ? "exception" : "normal"} />
            <small>{formatBytes(item.bytes)} · {item.files} 个文件 · {relativeEpoch(item.lastModified)}</small>
          </button>
        );
      })}
    </aside>
  );
}

export function FilesTab({ status }: { status: SandboxStatus }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const [previewing, setPreviewing] = useState<SandboxFileEntry | null>(null);
  const [filter, setFilter] = useState("");
  const [forgetHistory, setForgetHistory] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const online = status.online;

  const workspacesQuery = useApiQuery<SandboxWorkspace[]>({
    queryKey: ["sandbox-workspaces", online],
    fetcher: (signal) => (online ? sandboxApi.workspaces(signal) : Promise.resolve([])),
    invalidation: { resources: ["sandbox"] },
  });
  const workspaces = workspacesQuery.data ?? [];
  const selected = searchParams.get("sandbox.ws") ?? workspaces[0]?.id ?? "admin";
  const filesQuery = useApiQuery<SandboxFileListing | null>({
    queryKey: ["sandbox-files", selected, online],
    fetcher: (signal) => (online ? sandboxApi.files(selected, "", signal) : Promise.resolve(null)),
    invalidation: { resources: ["sandbox"] },
  });
  const files = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (filesQuery.data?.items ?? []).filter((item) => !needle || item.path.toLowerCase().includes(needle));
  }, [filesQuery.data, filter]);

  const selectWorkspace = (id: string) => setSearchParams(updateSearchParams(searchParams, { "sandbox.ws": id }), { replace: true });
  const refresh = () => {
    filesQuery.reload();
    workspacesQuery.reload();
  };

  const upload = async (list: FileList | null) => {
    if (!list?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(list)) {
        await uploadSandboxFile(selected, `uploads/${file.name}`, file);
      }
      message.success(`已上传到 uploads/，代码里用 open('uploads/文件名') 读取`);
      refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const remove = async (path: string) => {
    try {
      const result = await sandboxApi.deleteFile(selected, path);
      message.success(result.removed ? "已删除" : "文件已经不存在");
      refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const columns: ColumnsType<SandboxFileEntry> = [
    {
      title: "文件",
      dataIndex: "path",
      ellipsis: true,
      render: (value: string, row) => (
        <button type="button" className="sbx-file-name" onClick={() => setPreviewing(row)}>
          <span aria-hidden="true">{fileKindMeta(row.kind).emoji}</span> {value}
        </button>
      ),
    },
    { title: "类型", dataIndex: "kind", width: 90, render: (value: string) => <Tag>{fileKindMeta(value).label}</Tag> },
    { title: "大小", dataIndex: "size", width: 100, sorter: (a, b) => a.size - b.size, render: (value: number) => formatBytes(value) },
    {
      title: "修改时间",
      dataIndex: "mtime",
      width: 130,
      defaultSortOrder: "descend",
      sorter: (a, b) => a.mtime - b.mtime,
      render: (value: number) => <Tooltip title={formatEpoch(value)}>{relativeEpoch(value)}</Tooltip>,
    },
    {
      title: "",
      width: 120,
      render: (_, row) => (
        <span className="sbx-row-actions">
          <Tooltip title="预览"><Button size="small" type="text" icon={<EyeOutlined />} onClick={() => setPreviewing(row)} /></Tooltip>
          <Tooltip title="下载"><Button size="small" type="text" icon={<DownloadOutlined />} href={sandboxFileUrl(selected, row.path, true)} /></Tooltip>
          <Popconfirm title={`删除 ${row.path}？`} onConfirm={() => void remove(row.path)}>
            <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="删除" />
          </Popconfirm>
        </span>
      ),
    },
  ];

  if (!online) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="沙箱离线，工作区文件暂时看不到" />;
  }

  const usage = filesQuery.data?.usage;
  const quotaMb = status.settings.diskQuotaMb;
  return (
    <div className="sbx-files">
      {workspacesQuery.error && <QueryErrorAlert error={workspacesQuery.error} onRetry={workspacesQuery.reload} />}
      <div className="sbx-files-layout">
        <WorkspaceList workspaces={workspaces} selected={selected} quotaMb={quotaMb} onSelect={selectWorkspace} />
        <section className="sbx-files-main">
          <div className="sbx-toolbar">
            <strong className="sbx-files-title">{workspaceLabel(selected, workspaces.find((item) => item.id === selected)?.groupName)}</strong>
            <span className="sbx-muted">{usage ? `${formatBytes(usage.bytes)} / ${quotaMb}MB · ${usage.files} 个文件` : ""}</span>
            <span className="sbx-toolbar-spacer" />
            <Input.Search allowClear size="small" placeholder="筛选文件名" value={filter} onChange={(event) => setFilter(event.target.value)} style={{ width: 180 }} />
            <input ref={fileInput} type="file" multiple hidden onChange={(event) => void upload(event.target.files)} />
            <Button size="small" icon={<UploadOutlined />} loading={uploading} onClick={() => fileInput.current?.click()}>上传数据</Button>
            <Popconfirm
              title="清空这个工作区？"
              description={(
                <div className="sbx-reset-confirm">
                  <p>删除全部文件并终止正在执行的代码，不可撤销。</p>
                  <Checkbox checked={forgetHistory} onChange={(event) => setForgetHistory(event.target.checked)}>同时删除它的运行历史</Checkbox>
                </div>
              )}
              okButtonProps={{ danger: true }}
              onConfirm={async () => {
                try {
                  const result = await sandboxApi.resetWorkspace(selected, forgetHistory);
                  message.success(`已清空 ${result.removed} 项${result.historyRemoved ? `，删除 ${result.historyRemoved} 条历史` : ""}`);
                  refresh();
                } catch (error) {
                  message.error(error instanceof Error ? error.message : "清空失败");
                }
              }}
            >
              <DangerActionButton size="small">清空工作区</DangerActionButton>
            </Popconfirm>
          </div>
          {filesQuery.error && <QueryErrorAlert error={filesQuery.error} onRetry={filesQuery.reload} />}
          <Table
            size="small"
            rowKey="path"
            loading={filesQuery.loading}
            columns={columns}
            dataSource={files}
            pagination={{ pageSize: 30, showSizeChanger: false, hideOnSinglePage: true }}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="工作区是空的" /> }}
          />
          {filesQuery.data && filesQuery.data.total > filesQuery.data.items.length && (
            <p className="sbx-muted">只列出了前 {filesQuery.data.items.length} 个文件（共 {filesQuery.data.total} 个）。</p>
          )}
        </section>
      </div>
      <FilePreviewDrawer workspace={selected} file={previewing} onClose={() => setPreviewing(null)} />
    </div>
  );
}
