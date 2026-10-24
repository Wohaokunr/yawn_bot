import {
  AppstoreOutlined,
  BarsOutlined,
  CodeOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FolderAddOutlined,
  FolderOutlined,
  InboxOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  App as AntApp,
  Breadcrumb,
  Button,
  Checkbox,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Radio,
  Segmented,
  Select,
  Table,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { DangerActionButton, QueryErrorAlert, useApiQuery } from "../shared";
import { FilePreviewDrawer } from "./FilePreview";
import { sandboxApi, sandboxFileUrl, UploadConflictError, uploadSandboxFile } from "./sandbox-api";
import {
  baseName,
  breadcrumbs,
  childDirs,
  dirName,
  fileKindMeta,
  filesInDir,
  formatBytes,
  formatEpoch,
  inlineMediaKind,
  INBOX_DIR,
  isInboxPath,
  joinPath,
  kindCounts,
  nextFreeName,
  readSnippet,
  relativeEpoch,
  supportsFeature,
  updateSearchParams,
  validateRelativePath,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxFileEntry, SandboxFileListing, SandboxStatus, SandboxWorkspace } from "./types";

// 本机便利：记住列表/网格视图和工作区排序（存储不可用时静默降级）。
const VIEW_KEY = "yawnbot.sandbox.files.view";
const SORT_KEY = "yawnbot.sandbox.files.wsSort";

function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value as T) ? (value as T) : fallback;
  } catch {
    return fallback;
  }
}

function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 隐私模式等场景下忽略。
  }
}

type WorkspaceSort = "recent" | "size" | "name";
type ConflictChoice = "overwrite" | "keep" | "skip";

function copyText(text: string, done: () => void): void {
  void navigator.clipboard?.writeText(text).then(done);
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
  const [sort, setSort] = useState<WorkspaceSort>(() => readPref(SORT_KEY, ["recent", "size", "name"] as const, "recent"));
  const rows = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const matched = workspaces.filter((item) => !needle || item.id.includes(needle) || (item.groupName ?? "").toLowerCase().includes(needle));
    const sorters: Record<WorkspaceSort, (a: SandboxWorkspace, b: SandboxWorkspace) => number> = {
      recent: (a, b) => (b.lastModified ?? 0) - (a.lastModified ?? 0),
      size: (a, b) => b.bytes - a.bytes,
      name: (a, b) => workspaceLabel(a.id, a.groupName).localeCompare(workspaceLabel(b.id, b.groupName), "zh-CN"),
    };
    return [...matched].sort(sorters[sort]);
  }, [workspaces, filter, sort]);
  return (
    <aside className="sbx-ws-list" aria-label="工作区">
      <div className="sbx-ws-tools">
        <Input.Search allowClear size="small" placeholder="找群名或群号" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <Select
          size="small"
          value={sort}
          onChange={(value: WorkspaceSort) => {
            setSort(value);
            writePref(SORT_KEY, value);
          }}
          options={[{ value: "recent", label: "最近修改" }, { value: "size", label: "占用最多" }, { value: "name", label: "按名称" }]}
          aria-label="工作区排序"
          style={{ width: 104 }}
        />
      </div>
      {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={filter ? "没有匹配的工作区" : "还没有工作区"} />}
      {rows.map((item) => {
        const percent = Math.min(100, Math.round((item.bytes / (quotaMb * 1024 * 1024)) * 100));
        return (
          <button type="button" key={item.id} className={`sbx-ws-item ${item.id === selected ? "is-active" : ""}`} onClick={() => onSelect(item.id)} aria-current={item.id === selected}>
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

function RenameModal({
  file,
  taken,
  onCancel,
  onSubmit,
}: {
  file: SandboxFileEntry | null;
  taken: Set<string>;
  onCancel: () => void;
  onSubmit: (target: string, overwrite: boolean) => Promise<void>;
}): React.JSX.Element {
  const [value, setValue] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const target = value.trim().replace(/\\/g, "/").replace(/^\.\//, "");
  const error = file ? validateRelativePath(target) ?? (target === file.path ? "新路径和原来一样" : null) : null;
  const exists = !!target && target !== file?.path && taken.has(target);
  return (
    <Modal
      open={!!file}
      title="重命名 / 移动"
      okText="确定"
      onCancel={onCancel}
      afterOpenChange={(open) => {
        if (open && file) {
          setValue(file.path);
          setOverwrite(false);
        }
      }}
      okButtonProps={{ disabled: !!error || (exists && !overwrite), loading: busy }}
      onOk={async () => {
        setBusy(true);
        try {
          await onSubmit(target, overwrite);
        } finally {
          setBusy(false);
        }
      }}
      destroyOnHidden
    >
      <p className="sbx-muted">写上新的相对路径，带目录就是移动，例如 data/{file ? baseName(file.path) : "文件名"}。</p>
      <Input value={value} onChange={(event) => setValue(event.target.value)} status={error ? "error" : undefined} aria-label="新路径" autoFocus />
      {error && <span className="sbx-field-error">{error}</span>}
      {exists && <Checkbox className="sbx-gap" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)}>已有同名文件，覆盖它</Checkbox>}
    </Modal>
  );
}

function ConflictModal({
  names,
  onResolve,
}: {
  names: string[];
  onResolve: (choice: ConflictChoice | null) => void;
}): React.JSX.Element {
  const [choice, setChoice] = useState<ConflictChoice>("keep");
  return (
    <Modal
      open={names.length > 0}
      title={`${names.length} 个文件已经存在`}
      okText="继续上传"
      onOk={() => onResolve(choice)}
      onCancel={() => onResolve(null)}
      destroyOnHidden
    >
      <ul className="sbx-conflict-list">
        {names.slice(0, 8).map((name) => <li key={name}>{name}</li>)}
        {names.length > 8 && <li>……还有 {names.length - 8} 个</li>}
      </ul>
      <Radio.Group
        value={choice}
        onChange={(event) => setChoice(event.target.value as ConflictChoice)}
        options={[
          { value: "keep", label: "保留两个（新文件自动改名）" },
          { value: "overwrite", label: "覆盖旧文件" },
          { value: "skip", label: "跳过这些文件" },
        ]}
      />
    </Modal>
  );
}

function FileGrid({
  workspace,
  files,
  selectedKeys,
  onToggle,
  onOpen,
}: {
  workspace: string;
  files: SandboxFileEntry[];
  selectedKeys: string[];
  onToggle: (path: string, checked: boolean) => void;
  onOpen: (file: SandboxFileEntry) => void;
}): React.JSX.Element {
  if (!files.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这里没有文件" />;
  return (
    <ul className="sbx-file-grid">
      {files.map((file) => {
        const thumb = inlineMediaKind(file.path, file.kind) === "image";
        const checked = selectedKeys.includes(file.path);
        return (
          <li key={file.path} className={checked ? "is-selected" : ""}>
            <Checkbox className="sbx-grid-check" checked={checked} onChange={(event) => onToggle(file.path, event.target.checked)} aria-label={`选择 ${file.path}`} />
            <button type="button" onClick={() => onOpen(file)} title={file.path}>
              <span className="sbx-grid-thumb">
                {thumb ? <img src={sandboxFileUrl(workspace, file.path)} alt="" loading="lazy" /> : <span aria-hidden="true">{fileKindMeta(file.kind).emoji}</span>}
              </span>
              <span className="sbx-grid-name">{baseName(file.path)}</span>
              <small>{formatBytes(file.size)} · {relativeEpoch(file.mtime)}</small>
            </button>
            {isInboxPath(file.path) && <Tag className="sbx-grid-tag" color="magenta">群聊</Tag>}
          </li>
        );
      })}
    </ul>
  );
}

export function FilesTab({ status }: { status: SandboxStatus }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const [previewing, setPreviewing] = useState<SandboxFileEntry | null>(null);
  const [renaming, setRenaming] = useState<SandboxFileEntry | null>(null);
  const [filter, setFilter] = useState("");
  const [kindFilter, setKindFilter] = useState<string>("");
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [forgetHistory, setForgetHistory] = useState(false);
  const [upload, setUpload] = useState<{ name: string; index: number; total: number; percent: number } | null>(null);
  const [conflicts, setConflicts] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const [view, setView] = useState<"list" | "grid">(() => readPref(VIEW_KEY, ["list", "grid"] as const, "list"));
  const conflictResolver = useRef<((choice: ConflictChoice | null) => void) | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const online = status.online;
  const features = status.health?.features;
  const canRename = supportsFeature(features, "rename");
  const canMkdir = supportsFeature(features, "mkdir");
  const guardedUpload = supportsFeature(features, "upload_guard");

  const workspacesQuery = useApiQuery<SandboxWorkspace[]>({
    queryKey: ["sandbox-workspaces", online],
    fetcher: (signal) => (online ? sandboxApi.workspaces(signal) : Promise.resolve([])),
    invalidation: { resources: ["sandbox"] },
  });
  const workspaces = workspacesQuery.data ?? [];
  const selected = searchParams.get("sandbox.ws") ?? workspaces[0]?.id ?? "admin";
  const dir = searchParams.get("sandbox.dir") ?? "";
  const filesQuery = useApiQuery<SandboxFileListing | null>({
    queryKey: ["sandbox-files", selected, online],
    fetcher: (signal) => (online ? sandboxApi.files(selected, "", signal) : Promise.resolve(null)),
    invalidation: { resources: ["sandbox"] },
  });
  const allFiles = useMemo(() => filesQuery.data?.items ?? [], [filesQuery.data]);
  const taken = useMemo(() => new Set(allFiles.map((item) => item.path)), [allFiles]);
  const searching = filter.trim().length > 0;
  const scope = useMemo(() => (searching ? allFiles : filesInDir(allFiles, dir)), [allFiles, dir, searching]);
  const files = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return scope
      .filter((item) => !needle || item.path.toLowerCase().includes(needle))
      .filter((item) => !kindFilter || item.kind === kindFilter)
      .sort((a, b) => b.mtime - a.mtime);
  }, [scope, filter, kindFilter]);
  const folders = useMemo(() => (searching ? [] : childDirs(dir, filesQuery.data?.dirs, allFiles)), [dir, searching, filesQuery.data, allFiles]);
  const kinds = useMemo(() => kindCounts(scope), [scope]);
  const inboxCount = useMemo(() => allFiles.filter((item) => isInboxPath(item.path)).length, [allFiles]);

  // 从收件箱页点“位置”跳过来时直接打开那个文件的预览。
  const wanted = searchParams.get("sandbox.file");
  useEffect(() => {
    if (!wanted || !filesQuery.data) return;
    const found = allFiles.find((item) => item.path === wanted);
    if (found) setPreviewing(found);
    else message.info("这个文件已经不在工作区里了");
    setSearchParams(updateSearchParams(searchParams, { "sandbox.file": null }), { replace: true });
  }, [wanted, filesQuery.data]);

  const navigate = (patch: Record<string, string | null>) => {
    setSelectedKeys([]);
    setSearchParams(updateSearchParams(searchParams, patch), { replace: true });
  };
  const selectWorkspace = (id: string) => navigate({ "sandbox.ws": id, "sandbox.dir": null });
  const openDir = (path: string) => {
    setKindFilter("");
    navigate({ "sandbox.dir": path || null });
  };
  const refresh = () => {
    filesQuery.reload();
    workspacesQuery.reload();
  };
  const openInConsole = useCallback((file: SandboxFileEntry) => {
    setSearchParams(
      updateSearchParams(searchParams, { "sandbox.tab": "console", "sandbox.consoleWs": selected, "sandbox.open": file.path, "sandbox.openKind": file.kind }),
      { replace: true },
    );
  }, [searchParams, selected, setSearchParams]);

  const askConflict = (names: string[]) => new Promise<ConflictChoice | null>((resolve) => {
    conflictResolver.current = resolve;
    setConflicts(names);
  });

  const targetDir = dir || "uploads";
  const uploadFiles = async (list: File[]) => {
    if (!list.length) return;
    const planned = list.map((file) => ({ file, path: joinPath(targetDir, file.name) }));
    const clashing = planned.filter((item) => taken.has(item.path)).map((item) => item.path);
    let choice: ConflictChoice | null = "overwrite";
    if (clashing.length) {
      choice = await askConflict(clashing);
      if (!choice) return;
    }
    const used = new Set(taken);
    let done = 0;
    let skipped = 0;
    try {
      for (const [index, item] of planned.entries()) {
        let path = item.path;
        let overwrite = !guardedUpload;
        if (used.has(path)) {
          if (choice === "skip") {
            skipped += 1;
            continue;
          }
          if (choice === "keep") path = nextFreeName(path, used);
          else overwrite = true;
        }
        used.add(path);
        setUpload({ name: item.file.name, index: index + 1, total: planned.length, percent: 0 });
        try {
          await uploadSandboxFile(selected, path, item.file, {
            overwrite,
            onProgress: (percent) => setUpload((current) => (current ? { ...current, percent } : current)),
          });
          done += 1;
        } catch (error) {
          if (error instanceof UploadConflictError) skipped += 1;
          else throw error;
        }
      }
      if (done) message.success(`已上传 ${done} 个文件到 ${targetDir}/${skipped ? `，跳过 ${skipped} 个` : ""}`);
      else if (skipped) message.info(`跳过了 ${skipped} 个同名文件`);
      refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
      refresh();
    } finally {
      setUpload(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const removeMany = async (paths: string[]) => {
    try {
      const result = paths.length === 1
        ? { ...(await sandboxApi.deleteFile(selected, paths[0])), failed: [] as Array<{ path: string; error: string }> }
        : await sandboxApi.deleteMany(selected, paths);
      if (result.failed.length) message.warning(`删除了 ${result.removed} 项，${result.failed.length} 项失败：${result.failed[0].error}`);
      else message.success(result.removed ? `已删除 ${result.removed} 项` : "文件已经不存在");
      setSelectedKeys([]);
      refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const createFolder = () => {
    let name = "";
    modal.confirm({
      title: `在 ${dir || "工作区根目录"} 下新建文件夹`,
      icon: <FolderAddOutlined />,
      content: <Input placeholder="文件夹名" autoFocus onChange={(event) => { name = event.target.value; }} aria-label="文件夹名" />,
      okText: "新建",
      onOk: async () => {
        const path = joinPath(dir, name.trim());
        const problem = validateRelativePath(name.trim());
        if (problem || !name.trim()) {
          message.error(problem ?? "文件夹名不能为空");
          throw new Error("invalid");
        }
        await sandboxApi.mkdir(selected, path);
        message.success(`已新建 ${path}/`);
        refresh();
        openDir(path);
      },
    });
  };

  const rowMenu = (row: SandboxFileEntry) => ({
    items: [
      { key: "copy-path", icon: <CopyOutlined />, label: "复制路径" },
      { key: "copy-code", icon: <CodeOutlined />, label: "复制读取代码" },
      { key: "console", icon: <CodeOutlined />, label: "在控制台打开" },
      ...(canRename ? [{ key: "rename", icon: <EditOutlined />, label: "重命名 / 移动" }] : []),
      ...(searching && dirName(row.path) !== dir ? [{ key: "reveal", icon: <FolderOutlined />, label: "打开所在文件夹" }] : []),
    ],
    onClick: ({ key }: { key: string }) => {
      if (key === "copy-path") copyText(row.path, () => message.success("已复制路径"));
      if (key === "copy-code") copyText(readSnippet(row.path, row.kind), () => message.success("已复制 Python 读取代码"));
      if (key === "console") openInConsole(row);
      if (key === "rename") setRenaming(row);
      if (key === "reveal") {
        setFilter("");
        openDir(dirName(row.path));
      }
    },
  });

  const columns: ColumnsType<SandboxFileEntry> = [
    {
      title: "文件",
      dataIndex: "path",
      ellipsis: true,
      render: (value: string, row) => (
        <span className="sbx-file-cell">
          <button type="button" className="sbx-file-name" onClick={() => setPreviewing(row)} title={value}>
            <span aria-hidden="true">{fileKindMeta(row.kind).emoji}</span> {searching ? value : baseName(value)}
          </button>
          {isInboxPath(value) && <Tag color="magenta">来自群聊</Tag>}
        </span>
      ),
    },
    { title: "类型", dataIndex: "kind", width: 90, responsive: ["md"], render: (value: string) => <Tag>{fileKindMeta(value).label}</Tag> },
    { title: "大小", dataIndex: "size", width: 100, sorter: (a, b) => a.size - b.size, render: (value: number) => formatBytes(value) },
    {
      title: "修改时间",
      dataIndex: "mtime",
      width: 120,
      responsive: ["sm"],
      sorter: (a, b) => a.mtime - b.mtime,
      render: (value: number) => <Tooltip title={formatEpoch(value)}>{relativeEpoch(value)}</Tooltip>,
    },
    {
      title: "",
      width: 150,
      render: (_, row) => (
        <span className="sbx-row-actions">
          <Tooltip title="预览"><Button size="small" type="text" icon={<EyeOutlined />} onClick={() => setPreviewing(row)} aria-label="预览" /></Tooltip>
          <Tooltip title="下载"><Button size="small" type="text" icon={<DownloadOutlined />} href={sandboxFileUrl(selected, row.path, true)} aria-label="下载" /></Tooltip>
          <Dropdown menu={rowMenu(row)} trigger={["click"]}>
            <Button size="small" type="text" aria-label="更多操作">⋯</Button>
          </Dropdown>
          <Popconfirm title={`删除 ${row.path}？`} onConfirm={() => void removeMany([row.path])}>
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
  const usagePercent = usage ? Math.min(100, Math.round((usage.bytes / (quotaMb * 1024 * 1024)) * 100)) : 0;
  const crumbs = breadcrumbs(dir);
  return (
    <div className="sbx-files">
      {workspacesQuery.error && <QueryErrorAlert error={workspacesQuery.error} onRetry={workspacesQuery.reload} />}
      <div className="sbx-files-layout">
        <WorkspaceList workspaces={workspaces} selected={selected} quotaMb={quotaMb} onSelect={selectWorkspace} />
        <section
          className={`sbx-files-main ${dragging ? "is-dragging" : ""}`}
          onDragOver={(event) => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            void uploadFiles(Array.from(event.dataTransfer.files));
          }}
        >
          <div className="sbx-toolbar">
            <strong className="sbx-files-title">{workspaceLabel(selected, workspaces.find((item) => item.id === selected)?.groupName)}</strong>
            {usage && (
              <Tooltip title={`${formatBytes(usage.bytes)} / ${quotaMb}MB · ${usage.files} 个文件`}>
                <span className="sbx-quota">
                  <Progress percent={usagePercent} size="small" showInfo={false} status={usagePercent > 90 ? "exception" : "normal"} />
                  <small>{formatBytes(usage.bytes)} / {quotaMb}MB</small>
                </span>
              </Tooltip>
            )}
            <span className="sbx-toolbar-spacer" />
            <Input.Search allowClear size="small" placeholder="搜索整个工作区" value={filter} onChange={(event) => setFilter(event.target.value)} style={{ width: 150 }} aria-label="搜索文件" />
            <Segmented
              size="small"
              value={view}
              onChange={(value) => {
                setView(value as "list" | "grid");
                writePref(VIEW_KEY, String(value));
              }}
              options={[{ value: "list", icon: <BarsOutlined />, title: "列表" }, { value: "grid", icon: <AppstoreOutlined />, title: "网格" }]}
              aria-label="视图"
            />
            <input ref={fileInput} type="file" multiple hidden onChange={(event) => void uploadFiles(Array.from(event.target.files ?? []))} />
            <Tooltip title={`上传到 ${targetDir}/，也可以直接把文件拖进来`}>
              <Button size="small" icon={<UploadOutlined />} loading={!!upload} onClick={() => fileInput.current?.click()}>上传</Button>
            </Tooltip>
            {canMkdir && <Button size="small" icon={<FolderAddOutlined />} onClick={createFolder}>新建文件夹</Button>}
            <Popconfirm
              title="清空这个工作区？"
              description={(
                <div className="sbx-reset-confirm">
                  <p>删除全部文件（包括群聊放进来的附件）并终止正在执行的代码，不可撤销。</p>
                  <Checkbox checked={forgetHistory} onChange={(event) => setForgetHistory(event.target.checked)}>同时删除它的运行历史</Checkbox>
                </div>
              )}
              okButtonProps={{ danger: true }}
              onConfirm={async () => {
                try {
                  const result = await sandboxApi.resetWorkspace(selected, forgetHistory);
                  message.success(`已清空 ${result.removed} 项${result.historyRemoved ? `，删除 ${result.historyRemoved} 条历史` : ""}`);
                  navigate({ "sandbox.dir": null });
                  refresh();
                } catch (error) {
                  message.error(error instanceof Error ? error.message : "清空失败");
                }
              }}
            >
              <DangerActionButton size="small">清空工作区</DangerActionButton>
            </Popconfirm>
          </div>

          <div className="sbx-files-nav">
            <Breadcrumb
              items={crumbs.map((crumb, index) => ({
                key: crumb.path || "root",
                title: index === crumbs.length - 1 && !searching
                  ? <span aria-current="page">{crumb.label}</span>
                  : <button type="button" className="sbx-link" onClick={() => { setFilter(""); openDir(crumb.path); }}>{crumb.label}</button>,
              }))}
            />
            {searching && <span className="sbx-muted">在整个工作区里搜索「{filter.trim()}」</span>}
            <span className="sbx-toolbar-spacer" />
            {inboxCount > 0 && dir !== INBOX_DIR && (
              <Button size="small" type="dashed" icon={<InboxOutlined />} onClick={() => { setFilter(""); openDir(INBOX_DIR); }}>
                群聊附件 {inboxCount}
              </Button>
            )}
          </div>

          {upload && (
            <div className="sbx-upload-progress" role="status">
              <span>正在上传 {upload.name}（{upload.index}/{upload.total}）</span>
              <Progress percent={upload.percent} size="small" />
            </div>
          )}
          {dragging && <div className="sbx-drop-hint" aria-hidden="true">松手上传到 {targetDir}/</div>}

          {folders.length > 0 && (
            <ul className="sbx-folders" aria-label="子文件夹">
              {folders.map((folder) => (
                <li key={folder.path}>
                  <button type="button" onClick={() => openDir(folder.path)}>
                    {folder.path === INBOX_DIR ? <InboxOutlined /> : <FolderOutlined />}
                    <span>{baseName(folder.path)}</span>
                    <small>{folder.files} 个 · {formatBytes(folder.bytes)}</small>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {kinds.length > 1 && (
            <div className="sbx-kind-chips" role="group" aria-label="按类型筛选">
              <Tag.CheckableTag checked={!kindFilter} onChange={() => setKindFilter("")}>全部 {scope.length}</Tag.CheckableTag>
              {kinds.map((item) => (
                <Tag.CheckableTag key={item.kind} checked={kindFilter === item.kind} onChange={(checked) => setKindFilter(checked ? item.kind : "")}>
                  {fileKindMeta(item.kind).emoji} {fileKindMeta(item.kind).label} {item.count}
                </Tag.CheckableTag>
              ))}
            </div>
          )}

          {selectedKeys.length > 0 && (
            <div className="sbx-bulk-bar" role="region" aria-label="批量操作">
              <strong>已选 {selectedKeys.length} 项</strong>
              <Button size="small" onClick={() => copyText(selectedKeys.join("\n"), () => message.success("已复制路径"))} icon={<CopyOutlined />}>复制路径</Button>
              <Popconfirm title={`删除选中的 ${selectedKeys.length} 个文件？`} okButtonProps={{ danger: true }} onConfirm={() => void removeMany(selectedKeys)}>
                <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
              </Popconfirm>
              <Button size="small" type="link" onClick={() => setSelectedKeys([])}>取消选择</Button>
            </div>
          )}

          {filesQuery.error && <QueryErrorAlert error={filesQuery.error} onRetry={filesQuery.reload} />}
          {view === "grid" ? (
            <FileGrid
              workspace={selected}
              files={files}
              selectedKeys={selectedKeys}
              onToggle={(path, checked) => setSelectedKeys(checked ? [...selectedKeys, path] : selectedKeys.filter((key) => key !== path))}
              onOpen={setPreviewing}
            />
          ) : (
            <Table
              size="small"
              rowKey="path"
              loading={filesQuery.loading}
              columns={columns}
              dataSource={files}
              rowSelection={{ selectedRowKeys: selectedKeys, onChange: (keys) => setSelectedKeys(keys.map(String)) }}
              scroll={{ x: 520 }}
              pagination={{ pageSize: 30, showSizeChanger: false, hideOnSinglePage: true }}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={searching || kindFilter ? "没有符合条件的文件" : dir === INBOX_DIR ? "群聊里还没有附件放进来" : allFiles.length ? "这个文件夹里没有文件" : "工作区是空的，把文件拖进来试试"}
                  />
                ),
              }}
            />
          )}
          {filesQuery.data && filesQuery.data.total > filesQuery.data.items.length && (
            <p className="sbx-muted">只列出了前 {filesQuery.data.items.length} 个文件（共 {filesQuery.data.total} 个）。</p>
          )}
        </section>
      </div>
      <FilePreviewDrawer
        workspace={selected}
        file={previewing}
        siblings={files}
        onNavigate={setPreviewing}
        onClose={() => setPreviewing(null)}
        onOpenInConsole={openInConsole}
      />
      <RenameModal
        file={renaming}
        taken={taken}
        onCancel={() => setRenaming(null)}
        onSubmit={async (target, overwrite) => {
          if (!renaming) return;
          try {
            await sandboxApi.rename(selected, renaming.path, target, overwrite);
            message.success(`已移动到 ${target}`);
            setRenaming(null);
            refresh();
          } catch (error) {
            message.error(error instanceof Error ? error.message : "重命名失败");
          }
        }}
      />
      <ConflictModal
        names={conflicts}
        onResolve={(choice) => {
          setConflicts([]);
          conflictResolver.current?.(choice);
          conflictResolver.current = null;
        }}
      />
    </div>
  );
}
