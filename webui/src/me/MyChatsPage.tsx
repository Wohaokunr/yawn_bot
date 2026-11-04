import { ArrowLeftOutlined, DeleteOutlined, DownloadOutlined, EditOutlined, MessageOutlined, SearchOutlined, TeamOutlined, UserOutlined } from "@ant-design/icons";
import { Button, Empty, Input, Modal, Popconfirm, Skeleton, Tag, Typography } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type ChatHit, type ChatMessageRow, type ChatRow, chatExportUrl, spaceApi } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { groupByDay } from "./me-space-meta";
import { useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text, Paragraph } = Typography;

function chatTitle(row: ChatRow): string {
  if (row.title) return row.title;
  return row.where === "private" ? "私聊" : `群聊 · ${row.groupName || row.groupId}`;
}

function Viewer({ chat, onRenamed, onDeleted, onBack }: { chat: ChatRow; onRenamed: (title: string) => void; onDeleted: () => void; onBack: () => void }): React.JSX.Element {
  const { busy, run } = useAction();
  const [rows, setRows] = useState<ChatMessageRow[] | null>(null);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");

  const load = useCallback(async (before?: string) => {
    try {
      const page = await spaceApi.chatMessages(chat.id, before);
      setRows((current) => (before ? [...page.rows, ...(current ?? [])] : page.rows));
      setCursor(page.before);
      setHasMore(page.hasMore);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [chat.id]);

  useEffect(() => {
    setRows(null);
    void load();
  }, [load]);

  const days = useMemo(() => groupByDay(rows ?? []), [rows]);

  return <section className="me-chat-viewer" aria-label="对话内容">
    <header className="me-chat-viewer-head">
      <Button type="text" className="me-chat-back" icon={<ArrowLeftOutlined />} onClick={onBack} aria-label="返回列表" />
      <span className="me-plain-main"><strong>{chatTitle(chat)}</strong><Text type="secondary" className="me-hint">{chat.messages} 条 · 开始于 {formatQqTime(chat.firstAt ?? chat.createdAt)}</Text></span>
      <Button size="small" icon={<EditOutlined />} onClick={() => { setTitle(chat.title ?? ""); setRenaming(true); }}>改名</Button>
      <Button size="small" icon={<DownloadOutlined />} href={chatExportUrl(chat.id)}>导出</Button>
      <Popconfirm title="删除这段对话？" description="删除后 Bot 也不再参考这段对话；下一条私聊会开新对话。" okText="删除" okButtonProps={{ danger: true }} onConfirm={async () => { const ok = await run("del", () => spaceApi.deleteChat(chat.id), "删除了"); if (ok) onDeleted(); }}>
        <Button size="small" danger icon={<DeleteOutlined />} loading={busy === "del"}>删除</Button>
      </Popconfirm>
    </header>
    <div className="me-chat-scroll">
      {error && <QueryErrorAlert error={error} onRetry={() => void load()} />}
      {rows === null && !error && <Skeleton active paragraph={{ rows: 6 }} />}
      {hasMore && <div className="me-center"><Button size="small" onClick={() => cursor && void load(cursor)}>加载更早的消息</Button></div>}
      {rows && rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段对话还没有消息" />}
      {days.map((day) => (
        <div key={day.day}>
          <div className="me-chat-day"><span>{day.day}</span></div>
          {day.rows.map((row) => (
            <div key={row.id} className={`me-bubble-row is-${row.role === "user" ? "me" : "bot"}`}>
              <div className="me-bubble">
                <Paragraph className="me-bubble-text" ellipsis={{ rows: 12, expandable: true, symbol: "展开" }}>{row.content}</Paragraph>
                <span className="me-bubble-time">{(row.at ?? "").slice(11, 16)}</span>
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
    <Modal open={renaming} title="给这段对话起个名字" okText="保存" okButtonProps={{ disabled: !title.trim(), loading: busy === "rename" }} onCancel={() => setRenaming(false)} onOk={async () => { const next = await run("rename", () => spaceApi.renameChat(chat.id, title.trim()), "改好了"); if (next) { onRenamed(next.title); setRenaming(false); } }}>
      <Input autoFocus maxLength={40} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="比如：周末出游计划" />
    </Modal>
  </section>;
}

export function MyChatsPage(): React.JSX.Element {
  const { query, data, setData } = useResource<ChatRow[]>(["me", "chats"], () => spaceApi.chats());
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("chat");
  const [keyword, setKeyword] = useState("");
  const [hits, setHits] = useState<ChatHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const selected = data?.find((row) => row.id === selectedId) ?? null;

  const select = (id: string | null): void => {
    const next = new URLSearchParams(params);
    if (id) next.set("chat", id);
    else next.delete("chat");
    setParams(next, { replace: !id });
  };
  const search = async (text: string): Promise<void> => {
    setKeyword(text);
    if (!text.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      setHits(await spaceApi.searchChats(text.trim()));
    } finally {
      setSearching(false);
    }
  };
  const totals = useMemo(() => ({ chats: data?.length ?? 0, messages: (data ?? []).reduce((sum, row) => sum + row.messages, 0) }), [data]);

  return <div className="me-page">
    <PageHeader title="聊天记录" subtitle={data ? `和 Bot 的 ${totals.chats} 段对话，共 ${totals.messages} 条消息。只有你自己看得到。` : "和 Bot 的对话"} onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : (
      <div className={`me-chat-layout${selected ? " has-selection" : ""}`}>
        <aside className="me-chat-list" aria-label="对话列表">
          <Input.Search allowClear placeholder="搜索聊天内容" prefix={<SearchOutlined />} loading={searching} onSearch={(text) => void search(text)} onChange={(event) => { if (!event.target.value) void search(""); }} />
          {hits !== null ? (
            <>
              <Text type="secondary" className="me-hint">“{keyword}” 找到 {hits.length} 条{hits.length >= 50 ? "（只显示最近 50 条）" : ""}</Text>
              <ul className="me-chat-items">
                {hits.map((hit) => (
                  <li key={hit.messageId}>
                    <button type="button" className={`me-chat-item${hit.chatId === selectedId ? " is-active" : ""}`} onClick={() => select(hit.chatId)}>
                      <span className="me-chat-item-title">{hit.role === "user" ? "我" : "Bot"}<Text type="secondary" className="me-hint"> · {formatQqTime(hit.at)}</Text></span>
                      <span className="me-chat-item-preview">{hit.snippet}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : data.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有和 Bot 聊过" />
          ) : (
            <ul className="me-chat-items">
              {data.map((row) => (
                <li key={row.id}>
                  <button type="button" className={`me-chat-item${row.id === selectedId ? " is-active" : ""}`} onClick={() => select(row.id)}>
                    <span className="me-chat-item-title">
                      {row.where === "private" ? <UserOutlined /> : <TeamOutlined />} {chatTitle(row)}
                      <Tag className="me-inline-tag">{row.messages}</Tag>
                    </span>
                    <span className="me-chat-item-preview">{row.preview || "（空）"}</span>
                    <Text type="secondary" className="me-hint">{formatQqTime(row.lastAt ?? row.updatedAt)}</Text>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>
        {selected ? (
          <Viewer
            key={selected.id}
            chat={selected}
            onBack={() => select(null)}
            onRenamed={(title) => setData(data.map((row) => (row.id === selected.id ? { ...row, title } : row)))}
            onDeleted={() => { setData(data.filter((row) => row.id !== selected.id)); select(null); }}
          />
        ) : (
          <div className="me-chat-placeholder"><Empty image={<MessageOutlined className="me-placeholder-icon" />} description="选一段对话看看" /></div>
        )}
      </div>
    )}
  </div>;
}
