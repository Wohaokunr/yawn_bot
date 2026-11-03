import { MessageOutlined, ReloadOutlined, SwapOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Skeleton, Tag, Typography } from "antd";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { privateApi, type BridgeHandoff, type BridgeRelay } from "./private-api";
import { BRIDGE_REASON_LABELS, bridgeStatusLabel } from "./private-meta";

const { Text, Paragraph } = Typography;

function statusColor(status: string): string {
  if (status === "opened" || status === "done" || status === "sent") return "green";
  if (status === "open" || status === "opening") return "blue";
  if (status === "failed") return "red";
  return "default";
}

function HandoffList({ rows, onPickUser }: { rows: BridgeHandoff[]; onPickUser?: (userId: string) => void }): React.JSX.Element {
  if (rows.length === 0) return <Empty description="还没有从群里转到私聊的事" />;
  return <ul className="pa-user-list pa-bridge-list" aria-label="群里转私聊">
    {rows.map((row) => <li key={`${row.user_id ?? ""}-${row.id}`}>
      <div className="pa-bridge-main">
        <Text strong>{row.topic}</Text>
        <Text type="secondary" className="dbg-small">
          「{row.group_name}」{row.created} · {BRIDGE_REASON_LABELS[row.reason] ?? row.reason}
          {row.asked ? ` · 他在群里说：${row.asked}` : ""}
        </Text>
      </div>
      <Tag color={statusColor(row.status)}>{bridgeStatusLabel(row.status)}</Tag>
      {row.user_id && onPickUser && <Button size="small" type="link" onClick={() => onPickUser(String(row.user_id))}>{row.user_id}</Button>}
    </li>)}
  </ul>;
}

function RelayList({ rows, onPickUser, showReplies }: { rows: BridgeRelay[]; onPickUser?: (userId: string) => void; showReplies?: boolean }): React.JSX.Element {
  if (rows.length === 0) return <Empty description="还没有私聊替人在群里转告或提问" />;
  return <ul className="pa-user-list pa-bridge-list" aria-label="私聊转告到群里">
    {rows.map((row) => <li key={`${row.user_id ?? ""}-${row.id}`}>
      <div className="pa-bridge-main">
        <Text strong>{row.kind === "ask" ? "提问" : "转告"}：{row.text}</Text>
        <Text type="secondary" className="dbg-small">
          「{row.group_name}」{row.created}
          {row.kind === "ask" ? ` · 收到 ${row.reply_count ?? row.replies?.length ?? 0} 条回复${row.until ? `，收到 ${row.until}` : ""}` : ""}
        </Text>
        {showReplies && (row.replies ?? []).length > 0 && <ul className="pa-bridge-replies">
          {(row.replies ?? []).map((reply, index) => <li key={index}><Text>{reply.from_name}：{reply.text}</Text> <Text type="secondary" className="dbg-small">{reply.at}{reply.delivered ? " · 已转给他" : " · 待转"}</Text></li>)}
        </ul>}
      </div>
      <Tag color={statusColor(row.status)}>{bridgeStatusLabel(row.status)}</Tag>
      {row.user_id && onPickUser && <Button size="small" type="link" onClick={() => onPickUser(String(row.user_id))}>{row.user_id}</Button>}
    </li>)}
  </ul>;
}

function UserBridge({ userId }: { userId: string }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["private-bridge-user", userId],
    fetcher: (signal) => privateApi.bridgeUser(userId, signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 4 }} />;
  const data = query.data;
  return <section className="dbg-panel" aria-label={`用户 ${userId} 的协作记录`}>
    <div className="pst-section-head">
      <Text strong>用户 {userId} 的协作记录</Text>
      <span className="dbg-grow" />
      <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
    </div>
    {data.waiting_confirm.length > 0 && <Alert type="info" showIcon message="等他确认的转告" description={data.waiting_confirm.map((row) => `${row.summary}${row.he_replied_since ? "（他已回话）" : ""}`).join("；")} />}
    <Text strong><SwapOutlined /> 群里转私聊</Text>
    <HandoffList rows={data.handoffs} />
    <Text strong><MessageOutlined /> 转告与提问（含群友回复）</Text>
    <RelayList rows={data.relays} showReplies />
  </section>;
}

/** 群聊 ↔ 私聊协作：群里转私聊的事、私聊替人转告/提问的记录与限额。只有话题和群里公开的原文。 */
export function BridgeTab({ userId, onPickUser }: { userId: string; onPickUser: (userId: string | null) => void }): React.JSX.Element {
  const query = useApiQuery({
    queryKey: ["private-bridge"],
    fetcher: (signal) => privateApi.bridge(signal),
  });
  if (query.error && !query.data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!query.data) return <Skeleton active paragraph={{ rows: 6 }} />;
  const data = query.data;
  const limits = data.limits;
  return <div className="pa-bridge">
    {!data.enabled && <Alert type="warning" showIcon message="群聊 ↔ 私聊协作已关闭" description="AGENT_BRIDGE_ENABLED=false：群里不能转私聊，私聊也不能替人在群里转告。" />}
    <section className="dbg-panel" aria-label="协作概况">
      <div className="pst-section-head">
        <Text strong>群聊 ↔ 私聊协作</Text>
        <Tag>今天转私聊 {data.today.handoffs}</Tag>
        <Tag>今天转告/提问 {data.today.relays}</Tag>
        <Tag color={data.today.open_asks ? "blue" : "default"}>收回复中 {data.today.open_asks}</Tag>
        <span className="dbg-grow" />
        {userId && <Button size="small" onClick={() => onPickUser(null)}>看全部</Button>}
        <Button size="small" icon={<ReloadOutlined />} onClick={query.reload} loading={query.refreshing}>刷新</Button>
      </div>
      <Paragraph type="secondary" className="dbg-small">
        群里说“私聊你”的事由私聊这边主动开口接着办；私聊里他让我在群里转告/提问时，{limits.confirm ? "要他确认后" : "直接"}以“〔某某 让我转告〕”发到群里，群友引用回复会攒一会儿转回私聊。
        限额：每人每天转私聊 {limits.handoff_daily || "不限"} 次、转告 {limits.relay_daily || "不限"} 次；每群每小时 {limits.relay_group_hourly || "不限"} 条；提问收回复 {limits.ask_hours} 小时、最多 {limits.reply_limit} 条。
        这里只有话题和群里公开的原文，没有私聊对话内容。
      </Paragraph>
    </section>
    {userId ? <UserBridge userId={userId} /> : <>
      <section className="dbg-panel"><Text strong><SwapOutlined /> 最近从群里转到私聊</Text><HandoffList rows={data.handoffs} onPickUser={onPickUser} /></section>
      <section className="dbg-panel"><Text strong><MessageOutlined /> 最近私聊替人转告/提问</Text><RelayList rows={data.relays} onPickUser={onPickUser} /></section>
    </>}
  </div>;
}
