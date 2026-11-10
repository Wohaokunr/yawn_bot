import {
  CalendarOutlined,
  CheckSquareOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
  PushpinFilled,
  PushpinOutlined,
  SearchOutlined,
  SmileOutlined,
  DownloadOutlined,
  FireOutlined,
  SwapOutlined,
  UnorderedListOutlined,
  UpOutlined,
} from "@ant-design/icons";
import { App as AntApp, AutoComplete, Button, Checkbox, Empty, Input, Modal, Popconfirm, Progress, Segmented, Select, Skeleton, Switch, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type CountdownList, type MoodList, type NoteList, spaceApi, type TodoItem, type TodoList } from "./me-space-api";
import { countdownLabel, dueLabel, filterNotes, filterTodos, MOOD_OPTIONS, moodByDay, moodEmoji, PRIORITY_META, type TodoDraftEdit, todoChanges, type TodoFilter, trendLabel } from "./me-space-meta";
import { announceMeChanged, Columns, copyText, downloadText, Heatmap, HeatLegend, Highlight, ME_CHANGED_EVENT } from "./hub-ui";
import { hubApi, type TodoBatchAction } from "./me-hub-api";
import { addDays, completionPercent, groupTodos, heatColumns, moodByWeekday, moodDistribution, moodStreak, type NoteSort, notesToMarkdown, parseQuickTodo, sortNotes, sortTodos, type TodoSort, todayIso, todosToMarkdown, WEEKDAY_LABELS, weekdayOf } from "./me-hub-meta";
import { MeSection, StatTile, UrlTabs, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text, Paragraph } = Typography;
const TABS = ["todos", "notes", "countdowns", "moods"] as const;

// ── 待办 ───────────────────────────────────────────────────────────────

const PRIORITY_OPTIONS = (["high", "normal", "low"] as const).map((value) => ({ value, label: PRIORITY_META[value].label }));

/** 一条待办的编辑区：草稿只在点「保存」时一次性提交，日期没填完整不让存。 */
function TodoEditor({ item, busy, onSave, onDelete, onClose }: {
  item: TodoItem;
  busy: string | null;
  onSave: (changes: NonNullable<ReturnType<typeof todoChanges>>) => Promise<void>;
  onDelete: () => Promise<void>;
  onClose: () => void;
}): React.JSX.Element {
  const [draft, setDraft] = useState<TodoDraftEdit>({ content: item.content, priority: item.priority, due: item.due ?? "" });
  const [dateIncomplete, setDateIncomplete] = useState(false);
  const changes = dateIncomplete ? null : todoChanges(item, draft);
  const dirty = changes !== null && Object.keys(changes).length > 0;
  const save = (): void => { if (changes && dirty) void onSave(changes); };

  return <div className="me-todo-editor" role="group" aria-label={`编辑：${item.content}`}>
    <Input
      autoFocus
      maxLength={120}
      value={draft.content}
      onChange={(event) => setDraft({ ...draft, content: event.target.value })}
      onPressEnter={save}
      onKeyDown={(event) => { if (event.key === "Escape") onClose(); }}
      aria-label="待办内容"
      status={draft.content.trim() ? undefined : "error"}
    />
    <div className="me-todo-editor-row">
      <Segmented<TodoItem["priority"]> value={draft.priority} onChange={(priority) => setDraft({ ...draft, priority })} options={PRIORITY_OPTIONS} aria-label="优先级" />
      <span className="me-todo-editor-date">
        <Input
          type="date"
          value={draft.due}
          onChange={(event) => {
            setDateIncomplete(event.target.validity.badInput);
            setDraft({ ...draft, due: event.target.value });
          }}
          aria-label="截止日期"
          status={dateIncomplete ? "error" : undefined}
        />
        {draft.due && <Button type="link" size="small" onClick={() => { setDateIncomplete(false); setDraft({ ...draft, due: "" }); }}>去掉截止</Button>}
      </span>
    </div>
    {dateIncomplete && <Text type="danger" className="me-hint">日期还没填完整</Text>}
    <div className="me-todo-editor-actions">
      <Popconfirm title="删掉这条待办？" okText="删掉" okButtonProps={{ danger: true }} onConfirm={onDelete}>
        <Button danger type="text" icon={<DeleteOutlined />} loading={busy === `del-${item.id}`}>删除</Button>
      </Popconfirm>
      <span className="me-toolbar-spacer" />
      <Button onClick={onClose}>取消</Button>
      <Button type="primary" loading={busy === `todo-${item.id}`} disabled={!dirty} onClick={save}>保存</Button>
    </div>
  </div>;
}

const VIEW_KEY = "yawnbot.me.todos.view";
type TodoView = "grouped" | "flat";

function readView(): { view: TodoView; sort: TodoSort } {
  try {
    const raw = JSON.parse(window.localStorage.getItem(VIEW_KEY) ?? "{}") as { view?: string; sort?: string };
    return {
      view: raw.view === "flat" ? "flat" : "grouped",
      sort: (["smart", "due", "priority", "newest"] as const).find((value) => value === raw.sort) ?? "smart",
    };
  } catch {
    return { view: "grouped", sort: "smart" };
  }
}

const SORT_OPTIONS: Array<{ value: TodoSort; label: string }> = [
  { value: "smart", label: "默认顺序" },
  { value: "due", label: "按截止日期" },
  { value: "priority", label: "按优先级" },
  { value: "newest", label: "新加的在前" },
];

/** 一次贴很多行：每行一条，行内同样认“明天 … !重要”。 */
function BulkModal({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: (next: TodoList) => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const lines = text.split("\n").map((line) => line.replace(/^\s*(?:[-*•]|\d+[.、)])\s*(?:\[[ xX]\]\s*)?/, "").trim()).filter(Boolean);
  const submit = async (): Promise<void> => {
    if (lines.length === 0 || saving) return;
    setSaving(true);
    try {
      const result = await hubApi.bulkTodos(lines.slice(0, 30));
      message.success(`加了 ${result.added} 条${result.skipped.length ? `，${result.skipped.length} 条没加上（${result.skipped[0].reason}）` : ""}`);
      announceMeChanged();
      onAdded(result);
      setText("");
      onClose();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  return <Modal open={open} title="一次加很多条" okText={`加 ${Math.min(lines.length, 30)} 条`} okButtonProps={{ disabled: lines.length === 0, loading: saving }} onOk={() => void submit()} onCancel={onClose} destroyOnHidden>
    <div className="me-stack">
      <Text type="secondary" className="me-hint">每行一条，最多 30 条。从别处复制来的清单可以直接贴，行首的“-”“1.”“[ ]”会自动去掉；行里写“明天”“周五”“!重要”一样认。</Text>
      <Input.TextArea autoFocus autoSize={{ minRows: 6, maxRows: 14 }} value={text} onChange={(event) => setText(event.target.value)} placeholder={"买牛奶\n周五 交报告 !重要\n给妈妈打电话"} aria-label="多条待办" />
      {lines.length > 30 && <Text type="warning" className="me-hint">超过 30 行了，只会加前 30 条。</Text>}
    </div>
  </Modal>;
}

function TodosTab(): React.JSX.Element {
  const { query, data, setData } = useResource<TodoList>(["me", "todos"], () => spaceApi.todos());
  const { busy, run } = useAction();
  const { message } = AntApp.useApp();
  const [params, setParams] = useSearchParams();
  const [content, setContent] = useState("");
  const [priority, setPriority] = useState<TodoItem["priority"]>("normal");
  const [due, setDue] = useState("");
  const [filter, setFilter] = useState<TodoFilter>("open");
  const [keyword, setKeyword] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [{ view, sort }, setViewState] = useState(readView);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);
  const [bulk, setBulk] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);
  const today = todayIso();
  const parsed = useMemo(() => parseQuickTodo(content, today), [content, today]);
  const rows = useMemo(() => sortTodos(filterTodos(data?.items ?? [], filter, keyword), sort), [data, filter, keyword, sort]);
  const groups = useMemo(() => (view === "grouped" && sort === "smart" ? groupTodos(rows) : [{ bucket: "all", label: "", items: rows }]), [rows, view, sort]);

  const setView = (next: Partial<{ view: TodoView; sort: TodoSort }>): void => {
    const merged = { view, sort, ...next };
    setViewState(merged);
    try {
      window.localStorage.setItem(VIEW_KEY, JSON.stringify(merged));
    } catch {
      // 存不了就只在这次生效
    }
  };

  // 随手记、日历里加了待办，这里跟着刷新
  const reload = query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reload);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reload);
  }, [reload]);

  // 从搜一搜、提醒点过来：?find=<id> 定位到那一条
  const find = Number(params.get("find"));
  const loaded = Boolean(data);
  useEffect(() => {
    if (!loaded || !find) return;
    setFilter("all");
    setFlash(find);
    const next = new URLSearchParams(params);
    next.delete("find");
    setParams(next, { replace: true });
    window.setTimeout(() => document.getElementById(`me-todo-${find}`)?.scrollIntoView?.({ block: "center" }), 60);
    // 只在数据到了以后定位一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, find]);

  const add = async (): Promise<void> => {
    if (!content.trim()) return;
    const draft = { content: parsed.content, priority: priority !== "normal" ? priority : parsed.priority, due: due || parsed.due };
    const next = await run("add", () => spaceApi.addTodo(draft), `记下了${draft.due ? `，${draft.due === today ? "今天" : draft.due.slice(5)} 到期` : ""}`);
    if (next) {
      setData(next);
      setContent("");
      setDue("");
      setPriority("normal");
    }
  };
  const patch = async (item: TodoItem, change: Parameters<typeof spaceApi.editTodo>[1], success?: string): Promise<boolean> => {
    const next = await run(`todo-${item.id}`, () => spaceApi.editTodo(item.id, change), success);
    if (next) setData(next);
    return Boolean(next);
  };
  const batch = async (action: TodoBatchAction, extra: Parameters<typeof hubApi.batchTodos>[2] = {}, label = "改好了"): Promise<void> => {
    if (picked.length === 0) return;
    const next = await run(`batch-${action}`, () => hubApi.batchTodos(picked, action, extra), (r) => `${label} ${r.affected} 条`);
    if (next) {
      setData(next);
      setPicked([]);
      if (action === "delete") setSelecting(false);
    }
  };
  const exportMarkdown = async (): Promise<void> => {
    const text = todosToMarkdown(rows);
    if (await copyText(text)) message.success(`复制了 ${rows.length} 条（Markdown 清单）`);
    else {
      downloadText(`待办-${today}.md`, text);
      message.info("浏览器不让复制，已改为下载");
    }
  };

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 4 }} />;
  const overdue = data.items.filter((item) => item.overdue).length;
  const filters: { value: TodoFilter; label: string }[] = [
    { value: "open", label: `未完成 ${data.pending}` },
    { value: "today", label: "今天" },
    ...(overdue > 0 || filter === "overdue" ? [{ value: "overdue" as const, label: `逾期 ${overdue}` }] : []),
    { value: "done", label: `已完成 ${data.done}` },
    { value: "all", label: "全部" },
  ];
  const visibleIds = rows.map((item) => item.id);
  const allPicked = visibleIds.length > 0 && visibleIds.every((id) => picked.includes(id));

  const renderRow = (item: TodoItem): React.JSX.Element => {
    const label = dueLabel(item.dueInDays);
    const open = editing === item.id;
    return <li key={item.id} id={`me-todo-${item.id}`} className={`me-todo${item.done ? " is-done" : ""}${item.overdue ? " is-overdue" : ""}${open ? " is-editing" : ""}${flash === item.id ? " me-flash" : ""}`}>
      <div className="me-todo-row">
        {selecting && <Checkbox checked={picked.includes(item.id)} onChange={(event) => setPicked(event.target.checked ? [...picked, item.id] : picked.filter((id) => id !== item.id))} aria-label={`选择：${item.content}`} />}
        <label className="me-todo-check">
          <Checkbox checked={item.done} disabled={busy === `todo-${item.id}`} onChange={(event) => void patch(item, { done: event.target.checked }, event.target.checked ? "完成一件！" : undefined)} aria-label={`完成：${item.content}`} />
        </label>
        <div className="me-todo-body">
          <span className="me-todo-text"><Highlight text={item.content} needle={keyword} /></span>
          {(item.priority !== "normal" || label || (item.done && item.doneAt)) && (
            <span className="me-todo-meta">
              {item.priority !== "normal" && <Tag color={PRIORITY_META[item.priority].color}>{PRIORITY_META[item.priority].label}</Tag>}
              {label && !item.done && <Tag color={item.overdue ? "error" : item.dueInDays === 0 ? "warning" : "default"}>{label}</Tag>}
              {item.done && item.doneAt && <Text type="secondary" className="me-hint">完成于 {item.doneAt.slice(5)}</Text>}
            </span>
          )}
        </div>
        {item.overdue && !item.done && !selecting && (
          <Tooltip title="改到明天">
            <Button type="text" size="small" loading={busy === `todo-${item.id}`} onClick={() => void patch(item, { due: addDays(today, 1) }, "挪到明天了")} aria-label={`挪到明天：${item.content}`}>明天做</Button>
          </Tooltip>
        )}
        <Button
          type="text"
          className="me-todo-toggle"
          icon={open ? <UpOutlined /> : <EditOutlined />}
          aria-label={open ? `收起：${item.content}` : `编辑：${item.content}`}
          aria-expanded={open}
          onClick={() => setEditing(open ? null : item.id)}
        />
      </div>
      {open && (
        <TodoEditor
          item={item}
          busy={busy}
          onClose={() => setEditing(null)}
          onSave={async (changes) => { if (await patch(item, changes, "改好了")) setEditing(null); }}
          onDelete={async () => { const next = await run(`del-${item.id}`, () => spaceApi.deleteTodo(item.id), "删掉了"); if (next) { setData(next); setEditing(null); } }}
        />
      )}
    </li>;
  };

  return <div className="me-stack">
    <div className="me-composer-block" role="group" aria-label="新待办">
      <div className="me-composer">
        <Input
          className="me-composer-main"
          placeholder="要做什么？回车记下。可以写：明天 交报告 !重要"
          maxLength={120}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          onPressEnter={() => void add()}
          aria-label="新待办内容"
        />
        <Button type="primary" icon={<PlusOutlined />} loading={busy === "add"} disabled={!content.trim()} onClick={() => void add()}>添加</Button>
        <Tooltip title="一次加很多条">
          <Button icon={<UnorderedListOutlined />} onClick={() => setBulk(true)} aria-label="一次加很多条" />
        </Tooltip>
      </div>
      {content.trim() && (
        <div className="me-composer-extra">
          <Segmented<TodoItem["priority"]> size="small" value={priority !== "normal" ? priority : parsed.priority} onChange={setPriority} options={PRIORITY_OPTIONS} aria-label="优先级" />
          <Input type="date" size="small" value={due || parsed.due || ""} min={today} onChange={(event) => setDue(event.target.validity.badInput ? "" : event.target.value)} aria-label="截止日期（可选）" className="me-composer-date" />
          <span className="me-parse-hint">
            {!due && parsed.due && <Tag color="blue">识别到日期：{parsed.due === today ? "今天" : parsed.due}</Tag>}
            {parsed.content !== content.trim() && <Text type="secondary" className="me-hint">会记成：{parsed.content}</Text>}
          </span>
        </div>
      )}
    </div>

    {data.pending + data.done > 0 && (
      <Progress percent={completionPercent(data.done, data.pending)} size="small" strokeColor="var(--success)" format={(value) => `完成 ${value}%`} aria-label="待办完成进度" />
    )}

    <div className="me-toolbar">
      <Segmented<TodoFilter> value={filter} onChange={setFilter} options={filters} aria-label="筛选待办" />
      <Input allowClear prefix={<SearchOutlined aria-hidden="true" />} placeholder="搜索待办" value={keyword} onChange={(event) => setKeyword(event.target.value)} className="me-toolbar-search" aria-label="搜索待办" />
      <span className="me-toolbar-spacer" />
      <Text type="secondary" className="me-hint">{data.items.length}/{data.limit} 条</Text>
    </div>
    <div className="me-toolbar">
      <Segmented<TodoView> size="small" value={view} onChange={(next) => setView({ view: next })} options={[{ value: "grouped", label: "按日期分组" }, { value: "flat", label: "不分组" }]} aria-label="排列方式" />
      <Select<TodoSort> size="small" value={sort} onChange={(next) => setView({ sort: next })} options={SORT_OPTIONS} aria-label="排序" popupMatchSelectWidth={false} />
      <span className="me-toolbar-spacer" />
      <Button size="small" type={selecting ? "primary" : "default"} onClick={() => { setSelecting(!selecting); setPicked([]); }}>{selecting ? "退出批量" : "批量"}</Button>
      <Button size="small" icon={<CopyOutlined />} disabled={rows.length === 0} onClick={() => void exportMarkdown()}>复制清单</Button>
      <Button size="small" icon={<DownloadOutlined />} disabled={rows.length === 0} onClick={() => downloadText(`待办-${today}.md`, todosToMarkdown(rows))} aria-label="下载清单" />
      {data.done > 0 && (
        <Popconfirm title={`清掉 ${data.done} 条已完成的待办？`} okText="清掉" onConfirm={async () => { const next = await run("clear", () => spaceApi.clearDoneTodos(), (r) => `清掉了 ${r.cleared ?? 0} 条`); if (next) setData(next); }}>
          <Button size="small" loading={busy === "clear"}>清掉已完成</Button>
        </Popconfirm>
      )}
    </div>

    {rows.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={filter === "open" && !keyword ? "没有要做的事，轻松一下～" : "没有符合条件的待办"} />
    ) : groups.map((group) => (
      <div key={group.bucket}>
        {group.label && <div className={`me-bucket-head is-${group.bucket}`}>{group.label} · {group.items.length}</div>}
        <ul className="me-todo-list">{group.items.map(renderRow)}</ul>
      </div>
    ))}

    {selecting && (
      <div className="me-batch-bar" role="toolbar" aria-label="批量操作">
        <Checkbox checked={allPicked} indeterminate={picked.length > 0 && !allPicked} onChange={(event) => setPicked(event.target.checked ? visibleIds : [])}>全选</Checkbox>
        <Text type="secondary" className="me-hint">选了 {picked.length} 条</Text>
        <span className="me-toolbar-spacer" />
        <Button size="small" disabled={!picked.length} loading={busy === "batch-done"} onClick={() => void batch("done", {}, "完成了")}>标为完成</Button>
        <Button size="small" disabled={!picked.length} loading={busy === "batch-postpone"} onClick={() => void batch("postpone", { days: 1 }, "顺延了")}>顺延一天</Button>
        <Button size="small" disabled={!picked.length} onClick={() => void batch("postpone", { days: 7 }, "顺延了")}>顺延一周</Button>
        <Button size="small" disabled={!picked.length} loading={busy === "batch-priority"} onClick={() => void batch("priority", { priority: "high" }, "标成重要")}>标为重要</Button>
        <Popconfirm title={`删掉选中的 ${picked.length} 条待办？`} okText="删掉" okButtonProps={{ danger: true }} disabled={!picked.length} onConfirm={() => batch("delete", {}, "删掉了")}>
          <Button size="small" danger disabled={!picked.length} loading={busy === "batch-delete"}>删除</Button>
        </Popconfirm>
      </div>
    )}
    <BulkModal open={bulk} onClose={() => setBulk(false)} onAdded={setData} />
  </div>;
}

// ── 笔记 ───────────────────────────────────────────────────────────────

function NotesTab(): React.JSX.Element {
  const { query, data, setData } = useResource<NoteList>(["me", "notes"], () => spaceApi.notes());
  const { busy, run } = useAction();
  const [content, setContent] = useState("");
  const [tag, setTag] = useState("");
  const [keyword, setKeyword] = useState("");
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [sort, setSort] = useState<NoteSort>("pinned");
  const { message } = AntApp.useApp();
  const rows = useMemo(() => sortNotes(filterNotes(data?.items ?? [], keyword, activeTag), sort), [data, keyword, activeTag, sort]);
  const reload = query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reload);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reload);
  }, [reload]);

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 4 }} />;

  const toTodo = async (text: string): Promise<void> => {
    await run("to-todo", () => spaceApi.addTodo({ content: text.replace(/\s+/g, " ").slice(0, 120), priority: "normal" }), "加进待办了");
  };
  const add = async (): Promise<void> => {
    if (!content.trim()) return;
    const next = await run("add", () => spaceApi.addNote(content.trim(), tag.trim()), "记下了");
    if (next) {
      setData(next);
      setContent("");
    }
  };
  const save = async (id: number): Promise<void> => {
    const next = await run(`note-${id}`, () => spaceApi.editNote(id, { content: draft.trim() }), "改好了");
    if (next) {
      setData(next);
      setEditing(null);
    }
  };

  return <div className="me-stack">
    <div className="me-note-composer">
      <Input.TextArea
        placeholder="随手记点什么（Bot 在私聊里也能翻到）。Ctrl+Enter 保存"
        autoSize={{ minRows: 2, maxRows: 6 }}
        maxLength={300}
        showCount
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) void add(); }}
      />
      <div className="me-composer">
        <AutoComplete
          className="me-composer-main"
          placeholder="标签（可选，比如：家、学习）"
          value={tag}
          onChange={setTag}
          options={data.tags.map((value) => ({ value }))}
          maxLength={12}
          aria-label="标签"
        />
        <Button type="primary" icon={<PlusOutlined />} loading={busy === "add"} disabled={!content.trim()} onClick={() => void add()}>保存笔记</Button>
      </div>
    </div>

    <div className="me-toolbar">
      <Input.Search allowClear placeholder="搜索笔记或标签" value={keyword} onChange={(event) => setKeyword(event.target.value)} className="me-toolbar-search" />
      <span className="me-chip-row">
        <Tag.CheckableTag checked={activeTag === null} onChange={() => setActiveTag(null)}>全部</Tag.CheckableTag>
        {data.tags.map((value) => <Tag.CheckableTag key={value} checked={activeTag === value} onChange={(on) => setActiveTag(on ? value : null)}>#{value}</Tag.CheckableTag>)}
      </span>
      <span className="me-toolbar-spacer" />
      <Select<NoteSort> size="small" value={sort} onChange={setSort} aria-label="笔记排序" popupMatchSelectWidth={false} options={[{ value: "pinned", label: "置顶在前" }, { value: "newest", label: "新的在前" }, { value: "oldest", label: "旧的在前" }, { value: "edited", label: "最近改过" }]} />
      <Button size="small" icon={<DownloadOutlined />} disabled={rows.length === 0} onClick={() => downloadText(`笔记-${todayIso()}.md`, notesToMarkdown(rows))}>导出</Button>
      <Text type="secondary" className="me-hint">{rows.length === data.items.length ? "" : `${rows.length} / `}{data.items.length}/{data.limit} 条</Text>
    </div>

    {rows.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data.items.length ? "没有匹配的笔记" : "还没有笔记。也可以在私聊里说“帮我记一下……”"} />
    ) : (
      <div className="me-note-grid">
        {rows.map((item) => (
          <article key={item.id} className={`me-note${item.pinned ? " is-pinned" : ""}`}>
            {editing === item.id ? (
              <>
                <Input.TextArea autoFocus autoSize={{ minRows: 2, maxRows: 8 }} maxLength={300} showCount value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") setEditing(null); if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && draft.trim()) void save(item.id); }} aria-label="笔记内容" />
                <div className="me-note-actions">
                  <Button size="small" onClick={() => setEditing(null)}>取消</Button>
                  <Button size="small" type="primary" loading={busy === `note-${item.id}`} disabled={!draft.trim()} onClick={() => void save(item.id)}>保存</Button>
                </div>
              </>
            ) : (
              <>
                <Paragraph className="me-note-text" ellipsis={{ rows: 6, expandable: true, symbol: "展开" }}>{keyword ? <Highlight text={item.content} needle={keyword} /> : item.content}</Paragraph>
                <div className="me-note-foot">
                  {item.tag && <Tag className="me-note-tag" onClick={() => setActiveTag(item.tag)}>#{item.tag}</Tag>}
                  <Text type="secondary" className="me-hint">{item.at}{item.editedAt ? " · 改过" : ""}</Text>
                  <span className="me-toolbar-spacer" />
                  <Tooltip title={item.pinned ? "取消置顶" : "置顶"}>
                    <Button type="text" size="small" aria-label={item.pinned ? "取消置顶" : "置顶"} icon={item.pinned ? <PushpinFilled /> : <PushpinOutlined />} onClick={async () => { const next = await run(`pin-${item.id}`, () => spaceApi.editNote(item.id, { pinned: !item.pinned })); if (next) setData(next); }} />
                  </Tooltip>
                  <Tooltip title="复制">
                    <Button type="text" size="small" aria-label="复制" icon={<CopyOutlined />} onClick={async () => { if (await copyText(item.content)) message.success("复制了"); else message.warning("浏览器不让复制"); }} />
                  </Tooltip>
                  <Tooltip title="转成待办">
                    <Button type="text" size="small" aria-label="转成待办" icon={<SwapOutlined />} loading={busy === "to-todo"} onClick={() => void toTodo(item.content)} />
                  </Tooltip>
                  <Tooltip title="编辑">
                    <Button type="text" size="small" aria-label="编辑" icon={<EditOutlined />} onClick={() => { setEditing(item.id); setDraft(item.content); }} />
                  </Tooltip>
                  <Popconfirm title="删掉这条笔记？" okText="删掉" okButtonProps={{ danger: true }} onConfirm={async () => { const next = await run(`del-${item.id}`, () => spaceApi.deleteNote(item.id)); if (next) setData(next); }}>
                    <Button type="text" size="small" aria-label="删除" icon={<DeleteOutlined />} />
                  </Popconfirm>
                </div>
              </>
            )}
          </article>
        ))}
      </div>
    )}
  </div>;
}

// ── 倒数日 ─────────────────────────────────────────────────────────────

function CountdownsTab(): React.JSX.Element {
  const { query, data, setData } = useResource<CountdownList>(["me", "countdowns"], () => spaceApi.countdowns());
  const { busy, run } = useAction();
  const [title, setTitle] = useState("");
  const [date, setDate] = useState("");
  const [notify, setNotify] = useState(true);
  const [showPast, setShowPast] = useState(false);
  const reload = query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reload);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reload);
  }, [reload]);

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 3 }} />;
  const past = data.items.filter((item) => item.status === "past");
  const shown = [...data.items.filter((item) => item.status !== "past").sort((a, b) => a.daysLeft - b.daysLeft), ...(showPast ? past : [])];
  const nearest = shown.find((item) => item.status !== "past");

  const add = async (): Promise<void> => {
    const next = await run("add", () => spaceApi.addCountdown(title.trim(), date, notify), "加好了");
    if (next) {
      setData(next);
      setTitle("");
      setDate("");
    }
  };

  return <div className="me-stack">
    <div className="me-composer" role="group" aria-label="新倒数日">
      <Input className="me-composer-main" placeholder="什么日子？比如：期末考试、纪念日" maxLength={40} value={title} onChange={(event) => setTitle(event.target.value)} />
      <Input type="date" value={date} onChange={(event) => setDate(event.target.value)} aria-label="日期" className="me-composer-date" />
      <span className="me-inline-switch"><Switch size="small" checked={notify} onChange={setNotify} aria-label="到日子提醒我" /> 到日子提醒我</span>
      <Button type="primary" icon={<PlusOutlined />} loading={busy === "add"} disabled={!title.trim() || !date} onClick={() => void add()}>添加</Button>
    </div>
    <div className="me-toolbar">
      <Text type="secondary" className="me-hint">{data.items.length}/{data.limit} 个。开着“提醒”的，Bot 会在临近和当天私聊提醒你。{nearest ? `最近的是「${nearest.title}」，${countdownLabel(nearest.daysLeft)}。` : ""}</Text>
      <span className="me-toolbar-spacer" />
      {past.length > 0 && <span className="me-inline-switch"><Switch size="small" checked={showPast} onChange={setShowPast} aria-label="显示已经过去的" /> 已过去的 {past.length} 个</span>}
    </div>
    {date && <Text type="secondary" className="me-hint">{date} 是周{WEEKDAY_LABELS[weekdayOf(date)]}</Text>}
    {shown.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data.items.length ? "剩下的都过去了，打开右上角的开关可以看" : "还没有倒数日"} />
    ) : (
      <div className="me-countdown-grid">
        {shown.map((item) => (
          <article key={item.id} className={`me-countdown is-${item.status}`}>
            <Paragraph className="me-countdown-title" editable={{ tooltip: "改名字", maxLength: 40, onChange: async (value) => { if (value.trim() && value !== item.title) { const next = await run(`cd-${item.id}`, () => spaceApi.editCountdown(item.id, { title: value.trim() })); if (next) setData(next); } } }}>{item.title}</Paragraph>
            <strong className="me-countdown-days">{item.daysLeft === 0 ? "今天" : Math.abs(item.daysLeft)}<small>{item.daysLeft === 0 ? "" : " 天"}</small></strong>
            <Text type="secondary" className="me-hint">{countdownLabel(item.daysLeft)} · {item.date} 周{WEEKDAY_LABELS[weekdayOf(item.date)]}</Text>
            <div className="me-note-foot">
              <span className="me-inline-switch"><Switch size="small" checked={item.notify} onChange={async (on) => { const next = await run(`cd-${item.id}`, () => spaceApi.editCountdown(item.id, { notify: on })); if (next) setData(next); }} aria-label="提醒" /> 提醒</span>
              <span className="me-toolbar-spacer" />
              <Input type="date" size="small" className="me-todo-date" value={item.date} onChange={async (event) => { if (!event.target.value) return; const next = await run(`cd-${item.id}`, () => spaceApi.editCountdown(item.id, { date: event.target.value })); if (next) setData(next); }} aria-label="改日期" />
              <Popconfirm title="删掉这个倒数日？" okText="删掉" okButtonProps={{ danger: true }} onConfirm={async () => { const next = await run(`del-${item.id}`, () => spaceApi.deleteCountdown(item.id)); if (next) setData(next); }}>
                <Button type="text" size="small" aria-label="删除" icon={<DeleteOutlined />} />
              </Popconfirm>
            </div>
          </article>
        ))}
      </div>
    )}
  </div>;
}

// ── 心情 ───────────────────────────────────────────────────────────────

function MoodsTab(): React.JSX.Element {
  const { query, data, setData } = useResource<MoodList>(["me", "moods"], () => spaceApi.moods());
  const { busy, run } = useAction();
  const [score, setScore] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const days = useMemo(() => moodByDay(data?.items ?? [], todayIso(), 30), [data]);
  const [onlyNotes, setOnlyNotes] = useState(false);
  const heat = useMemo(() => {
    const byDay: Record<string, number> = {};
    for (const item of data?.items ?? []) byDay[item.at.slice(0, 10)] = Math.max(byDay[item.at.slice(0, 10)] ?? 0, item.score);
    return heatColumns(todayIso(), 84, byDay);
  }, [data]);
  const reload = query.reload;
  useEffect(() => {
    window.addEventListener(ME_CHANGED_EVENT, reload);
    return () => window.removeEventListener(ME_CHANGED_EVENT, reload);
  }, [reload]);

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 3 }} />;

  const log = async (): Promise<void> => {
    if (score === null) return;
    const next = await run("log", () => spaceApi.logMood(score, note.trim()), "记下今天的心情了");
    if (next) {
      setData(next);
      setScore(null);
      setNote("");
    }
  };

  return <div className="me-stack">
    <MeSection icon={<SmileOutlined />} title="现在心情怎么样？" hint="只有你和 Bot 看得到；连着几天不好，Bot 会更体贴一点。">
      <div className="me-mood-picker" role="radiogroup" aria-label="心情">
        {MOOD_OPTIONS.map((option) => (
          <button
            key={option.score}
            type="button"
            role="radio"
            aria-checked={score === option.score}
            className={`me-mood-option${score === option.score ? " is-active" : ""}`}
            onClick={() => setScore(option.score)}
          >
            <span className="me-mood-emoji" aria-hidden="true">{option.emoji}</span>
            <span>{option.label}</span>
          </button>
        ))}
      </div>
      <div className="me-composer">
        <Input className="me-composer-main" placeholder="想说一句吗？（可选）" maxLength={60} value={note} onChange={(event) => setNote(event.target.value)} onPressEnter={() => void log()} />
        <Button type="primary" loading={busy === "log"} disabled={score === null} onClick={() => void log()}>记下</Button>
      </div>
    </MeSection>

    <div className="me-stat-row">
      <StatTile label="最近 7 天" value={data.week.average === null ? "—" : `${moodEmoji(data.week.average)} ${data.week.averageLabel}`} sub={`${data.week.count} 次 · ${trendLabel(data.week.trend)}`} tone="brand" />
      <StatTile label="最近 30 天" value={data.month.average === null ? "—" : `${moodEmoji(data.month.average)} ${data.month.averageLabel}`} sub={`${data.month.count} 次 · ${trendLabel(data.month.trend)}`} />
      <StatTile label="一共记了" value={data.items.length} sub={`最多保留 ${data.limit} 条`} />
      <StatTile label="连续记了" value={`${moodStreak(data.items, todayIso())} 天`} sub={<span><FireOutlined /> 今天{data.items.some((item) => item.at.startsWith(todayIso())) ? "记过了" : "还没记"}</span>} tone="warning" />
    </div>

    <MeSection icon={<CalendarOutlined />} title="最近 30 天" hint="柱子越高心情越好；空着的是那天没记。">
      <div className="me-mood-bars" role="img" aria-label="最近 30 天心情">
        {days.map((day) => (
          <Tooltip key={day.day} title={`${day.day.slice(5)} ${day.score === null ? "没记" : `${moodEmoji(day.score)} ${day.score}`}`}>
            <span className={`me-mood-bar${day.score === null ? " is-empty" : ""}`} style={{ height: day.score === null ? "6%" : `${day.score * 20}%` }} />
          </Tooltip>
        ))}
      </div>
    </MeSection>

    {data.items.length > 0 && <>
      <div className="me-two-col">
        <MeSection title="最近 12 周" hint="颜色越深那天心情越好" extra={<HeatLegend less="低" more="高" />}>
          <Heatmap columns={heat} max={5} label="最近 12 周的心情" tip={(cell) => `${cell.day}：${cell.value ? `${moodEmoji(cell.value)} ${cell.value}` : "没记"}`} />
        </MeSection>
        <MeSection title="各种心情占多少">
          <div className="me-mood-dist">
            {moodDistribution(data.items).map((row) => (
              <div key={row.score} className="me-mood-dist-row">
                <span>{moodEmoji(row.score)} {MOOD_OPTIONS.find((option) => option.score === row.score)?.label}</span>
                <span className="me-mood-dist-track"><i style={{ width: `${row.percent}%` }} /></span>
                <span>{row.count} 次</span>
              </div>
            ))}
          </div>
        </MeSection>
      </div>
      <MeSection title="星期几心情最好" hint="按你记过的所有记录平均">
        <Columns values={moodByWeekday(data.items)} max={5} labels={WEEKDAY_LABELS.map((label) => `周${label}`)} label="按星期的平均心情" tip={(value, index) => `周${WEEKDAY_LABELS[index]}：${value === null ? "没记过" : value}`} tone={(value) => (value !== null && value < 2.5 ? "is-low" : "")} />
      </MeSection>
      <div className="me-toolbar">
        <strong>记录</strong>
        <span className="me-toolbar-spacer" />
        <span className="me-inline-switch"><Switch size="small" checked={onlyNotes} onChange={setOnlyNotes} aria-label="只看写了话的" /> 只看写了话的</span>
      </div>
      <ul className="me-plain-list">
        {data.items.filter((item) => !onlyNotes || item.note).slice(0, 30).map((item) => (
          <li key={`${item.index}-${item.at}`}>
            <span className="me-mood-emoji" aria-hidden="true">{moodEmoji(item.score)}</span>
            <span className="me-plain-main"><strong>{item.label}</strong>{item.note && <Text type="secondary"> · {item.note}</Text>}</span>
            <Text type="secondary" className="me-hint">{item.at}</Text>
            <Popconfirm title="删掉这条心情记录？" okText="删掉" onConfirm={async () => { const next = await run(`del-${item.index}`, () => spaceApi.deleteMood(item)); if (next) setData(next); }}>
              <Button type="text" size="small" aria-label="删除" icon={<DeleteOutlined />} />
            </Popconfirm>
          </li>
        ))}
      </ul>
    </>}
  </div>;
}

export function MyAssistantPage(): React.JSX.Element {
  return <div className="me-page">
    <PageHeader title="小本本" subtitle="待办、笔记、倒数日、心情。和私聊里让 Bot 记的是同一份，两边改了立刻互通。" />
    <UrlTabs
      keys={TABS}
      fallback="todos"
      items={[
        { key: "todos", label: <span><CheckSquareOutlined /> 待办</span>, children: <TodosTab /> },
        { key: "notes", label: <span><FileTextOutlined /> 笔记</span>, children: <NotesTab /> },
        { key: "countdowns", label: <span><CalendarOutlined /> 倒数日</span>, children: <CountdownsTab /> },
        { key: "moods", label: <span><SmileOutlined /> 心情</span>, children: <MoodsTab /> },
      ]}
    />
  </div>;
}
