import { Alert, Button, Empty, Flex, Modal, Space, Table, Tag, Typography } from "antd";
import type { TablePaginationConfig } from "antd/es/table";
import { useCallback, useEffect, useRef, useState } from "react";

const { Title, Text } = Typography;

export function formatTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

export interface EntityChangeScope {
  groupId?: string | null;
}

export interface EntityChangeDetail {
  resource: string;
  scope?: EntityChangeScope | null;
  entityId?: string | null;
}

export interface EntityInvalidation {
  resources: readonly string[];
  scope?: EntityChangeScope;
}

function isEntityInvalidation(
  value: EntityInvalidation | readonly string[],
): value is EntityInvalidation {
  return !Array.isArray(value);
}

function scopeMatches(actual: EntityChangeScope | null | undefined, expected?: EntityChangeScope): boolean {
  if (!expected) return true;
  return Object.entries(expected).every(([key, value]) => {
    if (value == null) return true;
    return actual?.[key as keyof EntityChangeScope] === value;
  });
}

export function useEntityRefresh(
  callback: () => void,
  invalidation: EntityInvalidation | readonly string[] = [],
): void {
  const resources = isEntityInvalidation(invalidation) ? invalidation.resources : invalidation;
  const scope = isEntityInvalidation(invalidation) ? invalidation.scope : undefined;
  const resourcesKey = resources.join("\u001f");
  const scopeKey = JSON.stringify(scope ?? null);
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    if (resources.length === 0) return undefined;
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<EntityChangeDetail>).detail;
      if (
        detail
        && resources.includes(detail.resource)
        && scopeMatches(detail.scope, scope)
      ) callbackRef.current();
    };
    window.addEventListener("yawnbot-entity-changed", listener);
    return () => window.removeEventListener("yawnbot-entity-changed", listener);
  }, [resourcesKey, scopeKey]);
}

export interface ApiQuery<T> {
  data: T | null;
  loading: boolean;
  initialLoading: boolean;
  refreshing: boolean;
  transitioning: boolean;
  stale: boolean;
  error: string;
  updatedAt: number | null;
  reload: () => void;
}

export interface ApiQueryConfig<T> {
  queryKey: readonly unknown[];
  fetcher: (signal: AbortSignal) => Promise<T>;
  invalidation?: EntityInvalidation;
  keepPreviousData?: boolean;
}

interface LegacyApiQueryOptions {
  resources?: readonly string[];
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

// 统一取数 Hook：queryKey 决定数据身份，fetcher 接收 AbortSignal；
// queryKey 变化立即取消旧请求并进入 transitioning，普通 reload/entity.changed 进入 refreshing。
// 保留旧签名仅供尚未迁移的页面过渡，Agent 管理页使用新配置对象。
export function useApiQuery<T>(config: ApiQueryConfig<T>): ApiQuery<T>;
export function useApiQuery<T>(load: () => Promise<T>, options?: LegacyApiQueryOptions): ApiQuery<T>;
export function useApiQuery<T>(
  configOrLoad: ApiQueryConfig<T> | (() => Promise<T>),
  legacyOptions: LegacyApiQueryOptions = {},
): ApiQuery<T> {
  const modern = typeof configOrLoad !== "function";
  const fetcher = modern
    ? configOrLoad.fetcher
    : (_signal: AbortSignal) => configOrLoad();
  const invalidation = modern
    ? configOrLoad.invalidation
    : legacyOptions.resources
      ? { resources: legacyOptions.resources }
      : undefined;
  const keepPreviousData = modern ? configOrLoad.keepPreviousData !== false : true;
  const keyToken: unknown = modern ? JSON.stringify(configOrLoad.queryKey) : configOrLoad;

  const [data, setData] = useState<T | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [transitioning, setTransitioning] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const dataRef = useRef<T | null>(null);
  const fetcherRef = useRef(fetcher);
  const controllerRef = useRef<AbortController | null>(null);
  const generation = useRef(0);
  fetcherRef.current = fetcher;

  const run = useCallback((mode: "initial" | "transition" | "refresh") => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const ticket = ++generation.current;
    const hasData = dataRef.current !== null;

    if (mode === "transition" && hasData) {
      setTransitioning(true);
      setRefreshing(false);
      setStale(true);
      if (!keepPreviousData) {
        dataRef.current = null;
        setData(null);
        setInitialLoading(true);
      }
    } else if (mode === "refresh" && hasData) {
      setRefreshing(true);
      setStale(true);
    } else {
      setInitialLoading(true);
    }

    setError("");
    fetcherRef.current(controller.signal)
      .then((value) => {
        if (ticket !== generation.current || controller.signal.aborted) return;
        dataRef.current = value;
        setData(value);
        setError("");
        setUpdatedAt(Date.now());
        setStale(false);
      })
      .catch((reason: unknown) => {
        if (ticket !== generation.current || controller.signal.aborted || isAbortError(reason)) return;
        setError(reason instanceof Error ? reason.message : "加载失败");
      })
      .finally(() => {
        if (ticket !== generation.current) return;
        setInitialLoading(false);
        setRefreshing(false);
        setTransitioning(false);
      });
  }, [keepPreviousData]);

  const mounted = useRef(false);
  useEffect(() => {
    const mode = mounted.current && dataRef.current !== null ? "transition" : "initial";
    mounted.current = true;
    run(mode);
    return () => controllerRef.current?.abort();
  }, [keyToken, run]);

  const reload = useCallback(() => run(dataRef.current === null ? "initial" : "refresh"), [run]);
  useEntityRefresh(reload, invalidation ?? []);

  return {
    data,
    loading: initialLoading || transitioning,
    initialLoading,
    refreshing,
    transitioning,
    stale,
    error,
    updatedAt,
    reload,
  };
}

export interface VersionedServerData {
  version?: string | null;
}

export interface DraftSafeServerState<T> {
  remoteUpdate: T | null;
  keepDraft: () => void;
  reloadRemote: () => void;
  acceptServerData: (value: T) => void;
}

// 表单 hydration 只允许在首次加载、无草稿的远端更新、或用户明确重新载入时发生。
// dirty=true 时的新服务端版本仅进入 remoteUpdate，不会改表单，也不会清 dirty。
export function useDraftSafeServerData<T extends VersionedServerData>(
  data: T | null,
  dirty: boolean,
  hydrate: (value: T) => void,
): DraftSafeServerState<T> {
  const [remoteUpdate, setRemoteUpdate] = useState<T | null>(null);
  const hasApplied = useRef(false);
  const appliedVersion = useRef<string | null>(null);
  const ignoredVersion = useRef<string | null>(null);
  const dirtyRef = useRef(dirty);
  const hydrateRef = useRef(hydrate);
  dirtyRef.current = dirty;
  hydrateRef.current = hydrate;

  const acceptServerData = useCallback((value: T) => {
    hydrateRef.current(value);
    hasApplied.current = true;
    appliedVersion.current = value.version ?? null;
    ignoredVersion.current = null;
    setRemoteUpdate(null);
  }, []);

  useEffect(() => {
    if (!data) return;
    const version = data.version ?? null;
    if (hasApplied.current && version === appliedVersion.current) return;
    if (dirtyRef.current) {
      if (version !== ignoredVersion.current) setRemoteUpdate(data);
      return;
    }
    acceptServerData(data);
  }, [acceptServerData, data]);

  const keepDraft = useCallback(() => {
    ignoredVersion.current = remoteUpdate?.version ?? null;
    setRemoteUpdate(null);
  }, [remoteUpdate]);

  const reloadRemote = useCallback(() => {
    if (remoteUpdate) acceptServerData(remoteUpdate);
  }, [acceptServerData, remoteUpdate]);

  return { remoteUpdate, keepDraft, reloadRemote, acceptServerData };
}

export function ServerDraftUpdateAlert({
  onKeep,
  onCompare,
  onReload,
}: {
  onKeep: () => void;
  onCompare: () => void;
  onReload: () => void;
}): React.JSX.Element {
  return (
    <Alert
      type="warning"
      showIcon
      className="section-alert"
      message="服务器配置已更新"
      description="检测到当前群的服务端版本发生变化。你的未保存草稿已保留，系统没有自动覆盖表单。"
      action={<Space wrap>
        <Button size="small" onClick={onKeep}>保留我的草稿</Button>
        <Button size="small" onClick={onCompare}>查看差异</Button>
        <Button size="small" danger onClick={onReload}>重新载入</Button>
      </Space>}
    />
  );
}

export function DraftDiffModal({
  open,
  draft,
  server,
  onClose,
}: {
  open: boolean;
  draft: unknown;
  server: unknown;
  onClose: () => void;
}): React.JSX.Element {
  const paneStyle: React.CSSProperties = {
    flex: "1 1 320px",
    minWidth: 0,
    maxHeight: 440,
    overflow: "auto",
    padding: 12,
    borderRadius: 12,
    background: "rgba(255,255,255,.62)",
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  };
  return (
    <Modal open={open} title="草稿与服务器版本差异" width={900} footer={null} onCancel={onClose}>
      <Text type="secondary">左侧是你当前未保存的表单，右侧是服务器最新版本。重新载入只会在你明确点击后发生。</Text>
      <Flex gap={12} wrap style={{ marginTop: 16 }}>
        <div style={paneStyle}><Text strong>我的草稿</Text><pre>{JSON.stringify(draft, null, 2)}</pre></div>
        <div style={paneStyle}><Text strong>服务器版本</Text><pre>{JSON.stringify(server, null, 2)}</pre></div>
      </Flex>
    </Modal>
  );
}

export function QueryErrorAlert({ error, onRetry }: { error: string; onRetry: () => void }): React.JSX.Element {
  return (
    <Alert
      type="error"
      showIcon
      message="加载失败"
      description={error}
      action={<Button size="small" onClick={onRetry}>重试</Button>}
    />
  );
}

export function PageHeader({
  title,
  subtitle,
  extra,
  onRefresh,
  refreshing = false,
  status,
}: {
  title: string;
  subtitle: string;
  extra?: React.ReactNode;
  onRefresh?: () => void;
  refreshing?: boolean;
  status?: React.ReactNode;
}): React.JSX.Element {
  return (
    <Flex justify="space-between" align="center" gap={16} wrap className="page-heading">
      <div>
        <Flex align="center" gap={10} wrap>
          <Title level={2}>{title}</Title>
          {status}
        </Flex>
        <Text type="secondary">{subtitle}</Text>
      </div>
      <Space wrap>
        {onRefresh && <Button onClick={onRefresh} loading={refreshing}>刷新</Button>}
        {extra}
      </Space>
    </Flex>
  );
}

export function AdminEmpty({
  description = "暂无数据",
  action,
}: {
  description?: React.ReactNode;
  action?: React.ReactNode;
}): React.JSX.Element {
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}>{action}</Empty>;
}

export function SaveStatus({ dirty, saving }: { dirty: boolean; saving: boolean }): React.JSX.Element | null {
  if (saving) return <Tag color="processing">保存中</Tag>;
  if (dirty) return <Tag color="gold">有未保存修改</Tag>;
  return null;
}

export function DangerActionButton(props: React.ComponentProps<typeof Button>): React.JSX.Element {
  return <Button {...props} danger className={`danger-action ${props.className ?? ""}`.trim()} />;
}

let unsavedScopeCount = 0;

function publishUnsavedState(): void {
  window.dispatchEvent(new CustomEvent("yawnbot-dirty-state", { detail: { count: unsavedScopeCount } }));
}

export function hasUnsavedChanges(): boolean {
  return unsavedScopeCount > 0;
}

export function confirmDiscardChanges(): boolean {
  return !hasUnsavedChanges() || window.confirm("当前页面有未保存修改，确定要离开吗？");
}

export function useUnsavedChanges(dirty: boolean): void {
  const registered = useRef(false);
  useEffect(() => {
    if (dirty && !registered.current) {
      registered.current = true;
      unsavedScopeCount += 1;
      publishUnsavedState();
    } else if (!dirty && registered.current) {
      registered.current = false;
      unsavedScopeCount = Math.max(unsavedScopeCount - 1, 0);
      publishUnsavedState();
    }
    return () => {
      if (registered.current) {
        registered.current = false;
        unsavedScopeCount = Math.max(unsavedScopeCount - 1, 0);
        publishUnsavedState();
      }
    };
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return undefined;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const click = (event: MouseEvent) => {
      const target = event.target as Element | null;
      const anchor = target?.closest("a[href]") as HTMLAnchorElement | null;
      if (!anchor || anchor.target === "_blank" || event.defaultPrevented) return;
      if (!confirmDiscardChanges()) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", click, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", click, true);
    };
  }, [dirty]);
}

// 少于等于一页时完全隐藏分页；否则只渲染分页条（配合服务端分页表格）。
export function TablePagination({ current, total, onChange }: { current: number; total: number; onChange: (page: number) => void }): React.JSX.Element {
  if (total <= 20) return <></>;
  const pagination: TablePaginationConfig = { current, total, pageSize: 20, showSizeChanger: false, onChange };
  return <Table rowKey="placeholder" columns={[]} dataSource={[]} showHeader={false} pagination={pagination} className="pagination-only" />;
}
