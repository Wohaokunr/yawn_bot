// 「我的空间」各页共用的小组件：区块、数字卡、URL 标签、可就地更新的数据、带提示的操作。
// 颜色只用主题变量（见 me.css / me-space.css），深色模式自动跟随。
import { App as AntApp, Tabs, Typography } from "antd";
import type { TabsProps } from "antd";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { type ApiQuery, useApiQuery } from "../shared";
import { announceMeChanged } from "./hub-ui";
import { pickTab } from "./me-space-meta";

const { Text } = Typography;

export const STALE_AFTER_MS = 60_000;

/** 取数 + 本地可覆盖：写操作返回新数据后直接 setData，不必整页重新拉。 */
export function useResource<T>(queryKey: readonly unknown[], fetcher: () => Promise<T>): { query: ApiQuery<T>; data: T | null; setData: (next: T) => void } {
  const query = useApiQuery({ queryKey, fetcher: () => fetcher() });
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    if (query.data !== null) setData(query.data);
  }, [query.data]);
  // 标签页放到后台超过一分钟，切回来时自动刷新一次（私聊里刚让 Bot 记的东西马上能看到）
  const reload = query.reload;
  useEffect(() => {
    let hiddenAt = 0;
    const onVisible = (): void => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt > STALE_AFTER_MS) reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [reload]);
  return { query, data, setData };
}

/** 跑一个写操作：成功提示、失败把后端原因弹出来；busy 用 key 区分同页多个按钮。 */
export function useAction(): { busy: string | null; run: <R>(key: string, task: () => Promise<R>, success?: string | ((result: R) => string | null)) => Promise<R | null> } {
  const { message } = AntApp.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const run = useCallback(async <R,>(key: string, task: () => Promise<R>, success?: string | ((result: R) => string | null)): Promise<R | null> => {
    setBusy(key);
    try {
      const result = await task();
      const text = typeof success === "function" ? success(result) : success;
      if (text) message.success(text);
      // 提醒中心和侧栏角标跟着刷新
      announceMeChanged();
      return result;
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
      return null;
    } finally {
      setBusy(null);
    }
  }, [message]);
  return { busy, run };
}

export function MeSection({
  icon,
  title,
  hint,
  extra,
  children,
  className,
}: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  hint?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}): React.JSX.Element {
  return <section className={`me-section${className ? ` ${className}` : ""}`}>
    <div className="me-section-head">
      <h3 className="me-section-title">{icon}<strong>{title}</strong></h3>
      {extra && <span className="me-section-extra">{extra}</span>}
      {hint && <Text type="secondary" className="me-hint me-section-hint">{hint}</Text>}
    </div>
    {children}
  </section>;
}

export function StatTile({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "brand" | "success" | "warning" | "danger" | "info" }): React.JSX.Element {
  return <div className={`me-stat${tone ? ` is-${tone}` : ""}`}>
    <span className="me-stat-label">{label}</span>
    <strong className="me-stat-value">{value}</strong>
    {sub && <span className="me-stat-sub">{sub}</span>}
  </div>;
}

/** 标签状态放在 URL 的 tab 参数里（刷新、分享链接都能回到同一页） */
export function UrlTabs<T extends string>({ items, keys, fallback, param = "tab" }: { items: NonNullable<TabsProps["items"]>; keys: readonly T[]; fallback: T; param?: string }): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const active = pickTab(params.get(param), keys, fallback);
  return <Tabs
    className="me-tabs"
    activeKey={active}
    items={items}
    onChange={(key) => {
      const next = new URLSearchParams(params);
      if (key === fallback) next.delete(param);
      else next.set(param, key);
      setParams(next, { replace: true });
    }}
  />;
}
