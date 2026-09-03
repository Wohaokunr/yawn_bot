import { Alert, Button, Empty, Flex, Space, Table, Tag, Typography } from "antd";
import type { TablePaginationConfig } from "antd/es/table";
import { useCallback, useEffect, useRef, useState } from "react";

const { Title, Text } = Typography;

export function formatTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

export interface EntityChangeDetail {
  resource: string;
  resourceId?: string | null;
}

export function useEntityRefresh(callback: () => void, resources: readonly string[] = []): void {
  useEffect(() => {
    if (resources.length === 0) return undefined;
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<EntityChangeDetail>).detail;
      if (detail && resources.includes(detail.resource)) callback();
    };
    window.addEventListener("yawnbot-entity-changed", listener);
    return () => window.removeEventListener("yawnbot-entity-changed", listener);
  }, [callback, resources]);
}

export interface ApiQuery<T> {
  data: T | null;
  loading: boolean;
  refreshing: boolean;
  error: string;
  updatedAt: number | null;
  reload: () => void;
}

export interface ApiQueryOptions {
  resources?: readonly string[];
}

// 统一取数 Hook:接管加载/错误状态,随 entity.changed 自动刷新;
// 用代次号拦截乱序返回,避免快速翻页/搜索时旧响应覆盖新响应。
export function useApiQuery<T>(load: () => Promise<T>, options: ApiQueryOptions = {}): ApiQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const generation = useRef(0);
  const hasData = useRef(false);
  const inFlight = useRef(false);
  const rerunRequested = useRef(false);
  const runRef = useRef<() => void>(() => undefined);
  const run = useCallback(() => {
    if (inFlight.current) {
      // 极慢网络/轮询重叠时只保留一个“再跑一次”请求；同时让当前旧请求失效，
      // 避免分页/搜索条件已经变化后旧响应覆盖新状态。
      rerunRequested.current = true;
      generation.current += 1;
      return;
    }
    inFlight.current = true;
    const ticket = ++generation.current;
    if (hasData.current) setRefreshing(true);
    else setLoading(true);
    load()
      .then((value) => {
        if (ticket === generation.current) {
          setData(value);
          hasData.current = true;
          setError("");
          setUpdatedAt(Date.now());
        }
      })
      .catch((reason: unknown) => {
        if (ticket === generation.current) {
          setError(reason instanceof Error ? reason.message : "加载失败");
        }
      })
      .finally(() => {
        inFlight.current = false;
        if (ticket === generation.current) {
          setLoading(false);
          setRefreshing(false);
        }
        if (rerunRequested.current) {
          rerunRequested.current = false;
          queueMicrotask(() => runRef.current());
        }
      });
  }, [load]);
  runRef.current = run;
  useEffect(() => { void run(); }, [run]);
  useEntityRefresh(run, options.resources ?? []);
  return { data, loading, refreshing, error, updatedAt, reload: run };
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
