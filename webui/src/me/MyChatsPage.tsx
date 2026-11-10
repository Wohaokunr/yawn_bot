import { ArrowLeftOutlined, CopyOutlined, DeleteOutlined, DownloadOutlined, DownOutlined, EditOutlined, MessageOutlined, PushpinFilled, PushpinOutlined, SearchOutlined, TeamOutlined, UpOutlined, UserOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Dropdown, Empty, Input, Modal, Popconfirm, Segmented, Skeleton, Tag, Tooltip, Typography } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type ChatHit, type ChatMessageRow, type ChatRow, chatExportUrl, spaceApi } from "./me-space-api";
import { formatQqTime } from "./me-meta";
import { groupByDay } from "./me-space-meta";
import { copyText, type FontScale, Highlight, useLocalPrefs, useUiPrefs } from "./hub-ui";
import { useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text, Paragraph } = Typography;
type Where = "all" | "private" | "group";

function chatTitle(row: ChatRow): string {
  if (row.title) return row.title;
  return row.where === "private" ? "私聊" : `群聊 · ${row.groupName || row.groupId}`;
}

function Viewer({ chat, target, initialFind, pinned, onPin, onRenamed, onDeleted, onBack }: {
  chat: ChatRow;
  target: string | null;
  initialFind: string;
  pinned: boolean;
  onPin: () => void;
  onRenamed: (title: string) => void;
  onDeleted: () => void;
  onBack: () => void;
}): React.JSX.Element {
  const { busy, run } = useAction();
  const { message } = AntApp.useApp();
  const { local, setLocal } = useLocalPrefs();
  const [rows, setRows] = useState<ChatMessageRow[] | null>(null);
  const [error, setError] = useState("");
  const [older, setOlder] = useState<{ cursor: string | null; more: boolean }>({ cursor: null, more: false });
  const [newer, setNewer] = useState<{ cursor: string | null; more: boolean }>({ cursor: null, more: false });
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const [find, setFind] = useState(initialFind);
  const [finding, setFinding] = useState(Boolean(initialFind));
  const [hitIndex, setHitIndex] = useState(0);
  const scroller = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async (mode: "first" | "older" | "newer", cursor?: string) => {
    try {
      const page = await spaceApi.chatMessages(chat.id, mode === "older" ? { before: cursor } : mode === "newer" ? { after: cursor } : target ? { around: target } : undefined);
      setRows((current) => (mode === "older" ? [...page.rows, ...(current ?? [])] : mode === "newer" ? [...(current ?? []), ...page.rows] : page.rows));
      if (mode !== "newer") setOlder({ cursor: page.before, more: page.hasMore });
      if (mode !== "older") setNewer({ cursor: page.after, more: page.hasNewer });
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [chat.id, target]);
  useEffect(() => {
    setRows(null);
    void load("first");
  }, [load]);

  // 从搜索结果跳过来：滚到那条消息；否则停在最新一条
  const ready = rows !== null;
  useEffect(() => {
    if (!ready) return;
    const box = scroller.current;
    if (!box) return;
    if (target) document.getElementById(`me-msg-${target}`)?.scrollIntoView?.({ block: "center" });
    else box.scrollTop = box.scrollHeight;
  }, [ready, target]);

  const days = useMemo(() => groupByDay(rows ?? []), [rows]);
  const needle = finding ? find.trim() : "";
  const hits = useMemo(() => (needle ? (rows ?? []).filter((row) => row.content.toLowerCase().includes(needle.toLowerCase())).map((row) => row.id) : []), [rows, needle]);
  const current = hits.length ? hits[Math.min(hitIndex, hits.length - 1)] : null;
  const jump = (delta: number): void => {
    if (!hits.length) return;
    const next = (hitIndex + delta + hits.length) % hits.length;
    setHitIndex(next);
    document.getElementById(`me-msg-${hits[next]}`)?.scrollIntoView?.({ block: "center" });
  };
  const copy = async (text: string): Promise<void> => {
    if (await copyText(text)) message.success("复制了");
    else message.warning("浏览器不让复制");
  };
  const copyAll = (): Promise<void> => copy((rows ?? []).map((row) => `${row.role === "user" ? "我" : "Bot"}：${row.content}`).join("\n"));

  return <section className="me-chat-viewer" aria-label="对话内容">
    <header className="me-chat-viewer-head">
      <Button type="text" className="me-chat-back" icon={<ArrowLeftOutlined />} onClick={onBack} aria-label="返回列表" />
      <span className="me-plain-main"><strong>{pinned && <PushpinFilled className="me-pin-mark" aria-label="已置顶" />}{chatTitle(chat)}</strong><Text type="secondary" className="me-hint">{chat.messages} 条（我说了 {chat.mine} 条）· 开始于 {formatQqTime(chat.firstAt ?? chat.createdAt)}</Text></span>
      <span className="me-chat-tools">
        <Tooltip title="在这段对话里找"><Button size="small" type={finding ? "primary" : "default"} icon={<SearchOutlined />} onClick={() => setFinding(!finding)} aria-label="在这段对话里找" aria-pressed={finding} /></Tooltip>
        <Tooltip title={pinned ? "取消置顶" : "置顶这段对话"}><Button size="small" icon={pinned ? <PushpinFilled /> : <PushpinOutlined />} onClick={onPin} aria-label={pinned ? "取消置顶" : "置顶这段对话"} aria-pressed={pinned} /></Tooltip>
        <Segmented<FontScale> size="small" value={local.chatFont} onChange={(chatFont) => setLocal({ chatFont })} options={[{ value: "s", label: "小" }, { value: "m", label: "中" }, { value: "l", label: "大" }]} aria-label="聊天字号" />
        <Button size="small" icon={<EditOutlined />} onClick={() => { setTitle(chat.title ?? ""); setRenaming(true); }}>改名</Button>
        <Dropdown menu={{ items: [
          { key: "txt", label: <a href={chatExportUrl(chat.id)}>纯文本 .txt</a> },
          { key: "md", label: <a href={chatExportUrl(chat.id, "md")}>Markdown .md</a> },
          { key: "json", label: <a href={chatExportUrl(chat.id, "json")}>JSON（给程序读）</a> },
          { type: "divider" },
          { key: "copy", label: "复制已加载的消息", onClick: () => void copyAll() },
        ] }}>
          <Button size="small" icon={<DownloadOutlined />}>导出</Button>
        </Dropdown>
        <Popconfirm title="删除这段对话？" description="删除后 Bot 也不再参考这段对话；下一条私聊会开新对话。" okText="删除" okButtonProps={{ danger: true }} onConfirm={async () => { const ok = await run("del", () => spaceApi.deleteChat(chat.id), "删除了"); if (ok) onDeleted(); }}>
          <Button size="small" danger icon={<DeleteOutlined />} loading={busy === "del"} aria-label="删除这段对话" />
        </Popconfirm>
      </span>
    </header>
    {finding && (
      <div className="me-chat-find" role="search">
        <Input
          autoFocus
          allowClear
          size="small"
          prefix={<SearchOutlined aria-hidden="true" />}
          placeholder="在这段对话里找"
          value={find}
          onChange={(event) => { setFind(event.target.value); setHitIndex(0); }}
          onPressEnter={(event) => jump(event.shiftKey ? -1 : 1)}
          onKeyDown={(event) => { if (event.key === "Escape") setFinding(false); }}
          aria-label="在这段对话里找"
        />
        <Text type="secondary" className="me-hint" aria-live="polite">{needle ? (hits.length ? `${Math.min(hitIndex, hits.length - 1) + 1}/${hits.length}` : "没找到") : ""}</Text>
        <Button size="small" type="text" icon={<UpOutlined />} disabled={!hits.length} onClick={() => jump(-1)} aria-label="上一处" />
        <Button size="small" type="text" icon={<DownOutlined />} disabled={!hits.length} onClick={() => jump(1)} aria-label="下一处" />
      </div>
    )}
    <div className="me-chat-scroll me-chat-messages" data-font={local.chatFont} ref={scroller}>
      {error && <QueryErrorAlert error={error} onRetry={() => void load("first")} />}
      {rows === null && !error && <Skeleton active paragraph={{ rows: 6 }} />}
      {older.more && <div className="me-center"><Button size="small" onClick={() => older.cursor && void load("older", older.cursor)}>加载更早的消息</Button></div>}
      {rows && rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这段对话还没有消息" />}
      {needle && older.more && <div className="me-center"><Text type="secondary" className="me-hint">只在已加载的消息里找；更早的要先加载，或用左边的搜索。</Text></div>}
      {days.map((day) => (
        <div key={day.day}>
          <div className="me-chat-day"><span>{day.day}</span></div>
          {day.rows.map((row) => (
            <div key={row.id} id={`me-msg-${row.id}`} className={`me-bubble-row is-${row.role === "user" ? "me" : "bot"}${row.id === target || row.id === current ? " is-target" : ""}`}>
              <div className="me-bubble">
                <Paragraph className="me-bubble-text" ellipsis={needle ? false : { rows: 12, expandable: true, symbol: "展开" }}>{needle ? <Highlight text={row.content} needle={needle} /> : row.content}</Paragraph>
                <span className="me-bubble-time">{(row.at ?? "").slice(11, 16)}</span>
              </div>
              <button type="button" className="me-bubble-copy" onClick={() => void copy(row.content)} aria-label="复制这条消息"><CopyOutlined /></button>
            </div>
          ))}
        </div>
      ))}
      {newer.more && <div className="me-center"><Button size="small" onClick={() => newer.cursor && void load("newer", newer.cursor)}>加载更新的消息</Button></div>}
    </div>
    <Modal open={renaming} title="给这段对话起个名字" okText="保存" okButtonProps={{ disabled: !title.trim(), loading: busy === "rename" }} onCancel={() => setRenaming(false)} onOk={async () => { const next = await run("rename", () => spaceApi.renameChat(chat.id, title.trim()), "改好了"); if (next) { onRenamed(next.title); setRenaming(false); } }}>
      <Input autoFocus maxLength={40} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="比如：周末出游计划" />
    </Modal>
  </section>;
}

export function MyChatsPage(): React.JSX.Element {
  const { query, data, setData } = useResource<ChatRow[]>(["me", "chats"], () => spaceApi.chats());
  const { prefs, save } = useUiPrefs();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("chat");
  const target = params.get("at");
  const [keyword, setKeyword] = useState(params.get("q") ?? "");
  const [draft, setDraft] = useState(params.get("q") ?? "");
  const [hits, setHits] = useState<ChatHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [where, setWhere] = useState<Where>("all");
  const selected = data?.find((row) => row.id === selectedId) ?? null;
  const pinned = useMemo(() => prefs?.pinnedChats ?? [], [prefs]);

  const select = (id: string | null, at?: string): void => {
    const next = new URLSearchParams(params);
    if (id) next.set("chat", id);
    else next.delete("chat");
    if (id && at) next.set("at", at);
    else next.delete("at");
    if (!id || !at) next.delete("q");
    else if (keyword) next.set("q", keyword);
    setParams(next, { replace: !id });
  };
  const search = async (text: string): Promise<void> => {
    setKeyword(text.trim());
    if (!text.trim()) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      setHits(await spaceApi.searchChats(text.trim()));
    } catch {
      setHits([]);
    } finally {
      setSearching(false);
    }
  };
  // 停手 400ms 自动搜，不用按回车
  useEffect(() => {
    if (draft.trim() === keyword) return undefined;
    const timer = window.setTimeout(() => void search(draft), 400);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  const togglePin = (id: string): void => save({ pinnedChats: pinned.includes(id) ? pinned.filter((item) => item !== id) : [id, ...pinned].slice(0, 20) });
  const rows = useMemo(() => {
    const list = (data ?? []).filter((row) => where === "all" || row.where === where);
    return [...list.filter((row) => pinned.includes(row.id)), ...list.filter((row) => !pinned.includes(row.id))];
  }, [data, where, pinned]);
  const totals = useMemo(() => ({ chats: data?.length ?? 0, messages: (data ?? []).reduce((sum, row) => sum + row.messages, 0), groups: (data ?? []).filter((row) => row.where === "group").length }), [data]);

  return <div className="me-page">
    <PageHeader title="聊天记录" subtitle={data ? `和 Bot 的 ${totals.chats} 段对话，共 ${totals.messages} 条消息。只有你自己看得到。` : "和 Bot 的对话"} onRefresh={query.reload} refreshing={query.refreshing} />
    {query.error && !data && <QueryErrorAlert error={query.error} onRetry={query.reload} />}
    {!data ? <Skeleton active paragraph={{ rows: 6 }} /> : (
      <div className={`me-chat-layout${selected ? " has-selection" : ""}`}>
        <aside className="me-chat-list" aria-label="对话列表">
          <Input.Search allowClear placeholder="搜索聊天内容" prefix={<SearchOutlined />} loading={searching} value={draft} onSearch={(text) => void search(text)} onChange={(event) => setDraft(event.target.value)} aria-label="搜索聊天内容" />
          {hits === null && totals.groups > 0 && totals.groups < totals.chats && (
            <Segmented<Where> size="small" block value={where} onChange={setWhere} options={[{ value: "all", label: `全部 ${totals.chats}` }, { value: "private", label: "私聊" }, { value: "group", label: "群聊" }]} aria-label="筛选对话" />
          )}
          {hits !== null ? (
            <>
              <Text type="secondary" className="me-hint" aria-live="polite">“{keyword}” 找到 {hits.length} 条{hits.length >= 50 ? "（只显示最近 50 条）" : ""}，点一条会跳到那句话</Text>
              {hits.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没找到，换个词试试" />}
              <ul className="me-chat-items">
                {hits.map((hit) => (
                  <li key={hit.messageId}>
                    <button type="button" className={`me-chat-item${hit.chatId === selectedId && hit.messageId === target ? " is-active" : ""}`} onClick={() => select(hit.chatId, hit.messageId)}>
                      <span className="me-chat-item-title">{hit.role === "user" ? "我" : "Bot"}<Text type="secondary" className="me-hint"> · {hit.chatTitle || (hit.where === "private" ? "私聊" : "群聊")} · {formatQqTime(hit.at)}</Text></span>
                      <span className="me-chat-item-preview"><Highlight text={hit.snippet} needle={keyword} /></span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : data.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有和 Bot 聊过。去 QQ 私聊它说句话，记录就会出现在这里。" />
          ) : (
            <ul className="me-chat-items">
              {rows.map((row) => (
                <li key={row.id}>
                  <button type="button" className={`me-chat-item${row.id === selectedId ? " is-active" : ""}`} onClick={() => select(row.id)} aria-current={row.id === selectedId ? "true" : undefined}>
                    <span className="me-chat-item-title">
                      {pinned.includes(row.id) && <PushpinFilled className="me-pin-mark" aria-label="已置顶" />}
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
            key={`${selected.id}:${target ?? ""}`}
            chat={selected}
            target={target}
            initialFind={target ? params.get("q") ?? "" : ""}
            pinned={pinned.includes(selected.id)}
            onPin={() => togglePin(selected.id)}
            onBack={() => select(null)}
            onRenamed={(title) => setData(data.map((row) => (row.id === selected.id ? { ...row, title } : row)))}
            onDeleted={() => { setData(data.filter((row) => row.id !== selected.id)); if (pinned.includes(selected.id)) togglePin(selected.id); select(null); }}
          />
        ) : (
          <div className="me-chat-placeholder"><Empty image={<MessageOutlined className="me-placeholder-icon" />} description={selectedId ? "这段对话找不到了，可能已经删除" : "选一段对话看看"} /></div>
        )}
      </div>
    )}
  </div>;
}
