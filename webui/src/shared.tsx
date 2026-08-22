import { Alert, Button, Flex, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

const { Title, Text } = Typography;

export function formatTime(value?: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

export function useEntityRefresh(callback: () => void): void {
  useEffect(() => {
    window.addEventListener("yawnbot-entity-changed", callback);
    return () => window.removeEventListener("yawnbot-entity-changed", callback);
  }, [callback]);
}

export interface ApiQuery<T> {
  data: T | null;
  loading: boolean;
  error: string;
  reload: () => void;
}

// 统一取数 Hook:接管加载/错误状态,随 entity.changed 自动刷新;
// 用代次号拦截乱序返回,避免快速翻页/搜索时旧响应覆盖新响应。
export function useApiQuery<T>(load: () => Promise<T>): ApiQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const run = useCallback(() => {
    const ticket = ++generation.current;
    setLoading(true);
    load()
      .then((value) => {
        if (ticket === generation.current) {
          setData(value);
          setError("");
        }
      })
      .catch((reason: unknown) => {
        if (ticket === generation.current) {
          setError(reason instanceof Error ? reason.message : "加载失败");
        }
      })
      .finally(() => {
        if (ticket === generation.current) setLoading(false);
      });
  }, [load]);
  useEffect(() => { void run(); }, [run]);
  useEntityRefresh(run);
  return { data, loading, error, reload: run };
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

export function PageHeader({ title, subtitle, extra }: { title: string; subtitle: string; extra?: React.ReactNode }): React.JSX.Element {
  return <Flex justify="space-between" align="center" gap={16} wrap className="page-heading"><div><Title level={2}>{title}</Title><Text type="secondary">{subtitle}</Text></div>{extra}</Flex>;
}
