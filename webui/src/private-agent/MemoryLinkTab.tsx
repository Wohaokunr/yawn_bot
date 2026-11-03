import { DeleteOutlined, HistoryOutlined, LinkOutlined, ReloadOutlined, TeamOutlined, UserOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty, Popconfirm, Skeleton, Space, Tag, Typography } from "antd";
import { useState } from "react";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { privateApi, type MemoryLinkEvent, type MemoryLinkUserDetail } from "./private-api";
import { memoryKeyLabel } from "./private-meta";

const { Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function eventDetail(row: MemoryLinkEvent): string {
  const parts: string[] = [];
  if (row.keys?.length) parts.push(row.keys.map(memoryKeyLabel).join("、"));
  if (row.key) parts.push(memoryKeyLabel(row.key));
  if (row.keyword) parts.push(`“${row.keyword}”`);
  if (row.group) parts.push(`「${row.group}」`);
  if (row.groups?.length) parts.push(row.groups.map((name) => `「${name}」`).join(""));
  if (row.on !== undefined) parts.push(row.on ? "开" : "关");
  if (row.by === "admin") parts.push("管理员操作");
  return parts.join(" · ");
}

export function EventList({ rows, onPickUser }: { rows: MemoryLinkEvent[]; onPickUser?: (userId: string) => void }): React.JSX.Element {
  if (rows.length === 0) return <Empty description="还没有跨群聊/私聊的记忆流动" />;
  return <ul className="pa-user-list pa-bridge-list" aria-label="记忆流动记录">
    {rows.map((row, index) => <li key={`${row.user_id ?? ""}-${row.at}-${index}`}>
      <div className="pa-bridge-main">
        <Text strong>{row.what}</Text>
        <Text type="secondary" className="dbg-small">{row.at}{eventDetail(row) ? ` · ${eventDetail(row)}` : ""}</Text>
      </div>
      {row.user_id && onPickUser && <Button size="small" type="link" onClick={() => onPickUser(String(row.user_id))}>{row.user_id}</Button>}
    </li>)}
  </ul>;
}

function UserMemoryLink({ userId }: { userId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<MemoryLinkUserDetail>({
    queryKey: ["private-memory-link-user", userId],
    fetcher: (signal) => privateApi.memoryLinkUser(userId, signal),
  });
  const [busy, setBusy] = useState(false);
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 5 }} />;
  const data = query.data;
  const act = async (work: () => Promise<unknown>, done: string): Promise<void> => {
    setBusy(true);
    try {
      await work();
      message.success(done);
      query.reload();
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };
  const settings = data.settings;
  return <section className="dbg-panel pa-memlink-user" aria-label={`用户 ${userId} 的记忆打通`}>
    <div className="pst-section-head">
      <Text strong><UserOutlined /> 用户 {userId}</Text>
      <span className="dbg-grow" />
      <Popconfirm title="撤掉他全部“群里也知道”？" description="之后群里的 Bot 不再看到他的私聊事实；只能由他本人在私聊里重新打开。" onConfirm={() => act(() => privateApi.memoryLinkUnshare(userId), "已撤掉共享")} disabled={settings.share_keys.length === 0}>
        <Button size="small" danger disabled={busy || settings.share_keys.length === 0}>撤掉共享</Button>
      </Popconfirm>
      <Popconfirm title="恢复默认设置？" description="不共享、不静音任何群、私聊里可以提群里的事。“忘掉”的记录会保留。" onConfirm={() => act(() => privateApi.memoryLinkReset(userId), "已恢复默认")}>
        <Button size="small" disabled={busy}>恢复默认</Button>
      </Popconfirm>
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    <Space wrap size={[4, 4]}>
      <Tag color={settings.groups_in_private ? "green" : "orange"}>{settings.groups_in_private ? "私聊里会提群里的事" : "私聊里不提群里的事"}</Tag>
      {settings.share_keys.length > 0
        ? <Tag color="blue">群里也知道：{settings.share_keys.map(memoryKeyLabel).join("、")}{settings.share_groups.length ? `（只限 ${settings.share_groups.length} 个群）` : ""}</Tag>
        : <Tag>私聊事实不进群</Tag>}
      {settings.muted_groups.length > 0 && <Tag color="orange">静音 {settings.muted_groups.length} 个群</Tag>}
    </Space>

    <Text strong><TeamOutlined /> 每个群</Text>
    {data.groups.length === 0 ? <Empty description="他不在任何 Bot 所在的群里" /> : <ul className="pa-user-list pa-bridge-list" aria-label="各群的流动情况">
      {data.groups.map((group) => <li key={group.group_id}>
        <div className="pa-bridge-main">
          <Text strong>{group.name}</Text>
          <Text type="secondary" className="dbg-small">
            {group.agent_readable ? (group.in_private ? "群里的记忆会带进私聊" : "他让私聊里别提这个群") : "Agent 没开或他隐私退出，两边都不流动"}
            {group.group_sees_shared.length > 0 ? ` · 群里能看到：${group.group_sees_shared.map((item) => `${item.label}「${item.content}」`).join("；")}` : ""}
          </Text>
        </div>
      </li>)}
    </ul>}

    <Text strong>私聊里记得的</Text>
    {data.private_facts.length === 0 ? <Empty description="没有私聊事实" /> : <ul className="pa-user-list" aria-label="私聊事实">
      {data.private_facts.map((item) => <li key={item.key}><Text>{item.label}：{item.content}</Text> <Text type="secondary" className="dbg-small">{item.source}</Text></li>)}
    </ul>}
    <Text strong>私聊里能用到的群画像</Text>
    {data.group_profile.length === 0 ? <Empty description="没有" /> : <ul className="pa-user-list" aria-label="群画像">
      {data.group_profile.map((item) => <li key={item.key}>
        <Text>{memoryKeyLabel(item.key)}：{item.content}</Text>{" "}
        <Text type="secondary" className="dbg-small">{item.groups.join("、")}{item.self_told ? " · 他亲口说的" : ""}{item.as_of ? ` · 记到 ${item.as_of}` : ""}</Text>
      </li>)}
    </ul>}

    <Text strong>他让 Bot 忘掉的</Text>
    {data.tombstones.length === 0 ? <Empty description="没有" /> : <ul className="pa-user-list pa-bridge-list" aria-label="忘掉的记录">
      {data.tombstones.map((item) => <li key={item.index}>
        <div className="pa-bridge-main">
          <Text>{item.what}{item.keyword ? `：“${item.keyword}”` : ""}</Text>
          <Text type="secondary" className="dbg-small">{item.scope} · 到 {item.until} 前不会被自动整理记回来</Text>
        </div>
        <Popconfirm title="删掉这条“忘掉”记录？" description="之后自动整理可能会重新记起这件事。" onConfirm={() => act(() => privateApi.memoryLinkDropTombstone(userId, item.index), "已删掉")}>
          <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="删掉这条记录" disabled={busy} />
        </Popconfirm>
      </li>)}
    </ul>}

    <Text strong><HistoryOutlined /> 流动记录</Text>
    <EventList rows={data.ledger} />
  </section>;
}

/** 群聊记忆 ↔ 私聊记忆打通：谁开了“群里也知道”、静音了哪些群、让 Bot 忘掉过什么，以及流动记录。 */
export function MemoryLinkTab({ userId, onPickUser }: { userId: string; onPickUser: (userId: string | null) => void }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["private-memory-link"],
    fetcher: (signal) => privateApi.memoryLink(signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const data = query.data;
  return <div className="pa-bridge">
    {!data.enabled && <Alert type="warning" showIcon message="记忆打通已关闭" description="MEMORY_LINK_ENABLED=false：不共享、不屏蔽、不记“忘掉”，退回旧行为。" />}
    {data.enabled && !data.share_enabled && <Alert type="info" showIcon message="“群里也知道”暂停中" description="MEMORY_LINK_SHARE=false：用户的共享设置保留，但群里的 Bot 暂时看不到任何私聊事实。" />}
    <section className="dbg-panel" aria-label="记忆打通概况">
      <div className="pst-section-head">
        <Text strong><LinkOutlined /> 群聊记忆 ↔ 私聊记忆</Text>
        <Tag color={data.totals.sharing ? "blue" : "default"}>开了共享 {data.totals.sharing} 人</Tag>
        <Tag color={data.totals.muting ? "orange" : "default"}>静音了群 {data.totals.muting} 人</Tag>
        <Tag>让 Bot 忘掉过 {data.totals.forgetting} 人</Tag>
        <span className="dbg-grow" />
        {userId && <Button size="small" onClick={() => onPickUser(null)}>看全部</Button>}
        <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
      </div>
      <Paragraph type="secondary" className="dbg-small">
        群里的记忆只从他本人活跃、开了 Agent、没隐私退出的群带进私聊，他可以让私聊里别提某个群；私聊里聊的内容永远不进群，
        只有他明确同意的类别（称呼、爱好之类）群里的 Bot 才看得到{data.group_fact_limit ? `，每人最多 ${data.group_fact_limit} 条` : ""}。
        他让 Bot 忘掉的事，{data.tombstone_days} 天内两边的自动整理都不会再记回来{data.group_self_facts ? "；群里亲口让 Bot 记住的关于自己的事，私聊里也记着" : ""}。
        管理员在这里只能收紧（撤掉共享、恢复默认），不能替他打开共享。
      </Paragraph>
    </section>
    {userId ? <UserMemoryLink userId={userId} /> : <>
      <section className="dbg-panel">
        <Text strong><UserOutlined /> 改过设置的用户</Text>
        {data.users.length === 0 ? <Empty description="还没有人改过记忆打通设置" /> : <ul className="pa-user-list pa-bridge-list" aria-label="改过设置的用户">
          {data.users.map((row) => <li key={row.user_id}>
            <div className="pa-bridge-main">
              <Button size="small" type="link" onClick={() => onPickUser(row.user_id)}>{row.user_id}</Button>
              <Text type="secondary" className="dbg-small">
                {row.shares.length ? `群里也知道：${row.shares.map(memoryKeyLabel).join("、")}` : "不共享"}
                {row.muted_groups ? ` · 静音 ${row.muted_groups} 个群` : ""}
                {row.groups_in_private ? "" : " · 私聊里不提群"}
                {row.tombstones ? ` · 忘掉 ${row.tombstones} 条` : ""}
              </Text>
            </div>
          </li>)}
        </ul>}
      </section>
      <section className="dbg-panel"><Text strong><HistoryOutlined /> 最近的流动记录</Text><EventList rows={data.events} onPickUser={onPickUser} /></section>
    </>}
  </div>;
}
