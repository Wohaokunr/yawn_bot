import { Flex, Typography } from "antd";
import { useEffect } from "react";

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

export function PageHeader({ title, subtitle, extra }: { title: string; subtitle: string; extra?: React.ReactNode }): React.JSX.Element {
  return <Flex justify="space-between" align="center" gap={16} wrap className="page-heading"><div><Title level={2}>{title}</Title><Text type="secondary">{subtitle}</Text></div>{extra}</Flex>;
}
