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
  SmileOutlined,
} from "@ant-design/icons";
import { AutoComplete, Button, Checkbox, Empty, Input, Popconfirm, Segmented, Select, Skeleton, Switch, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import { PageHeader, QueryErrorAlert } from "../shared";
import { type CountdownList, type MoodList, type NoteList, spaceApi, type TodoItem, type TodoList } from "./me-space-api";
import { countdownLabel, dueLabel, filterNotes, filterTodos, MOOD_OPTIONS, moodByDay, moodEmoji, PRIORITY_META, type TodoFilter, trendLabel } from "./me-space-meta";
import { MeSection, StatTile, UrlTabs, useAction, useResource } from "./space-ui";
import "./me.css";
import "./me-space.css";

const { Text, Paragraph } = Typography;
const TABS = ["todos", "notes", "countdowns", "moods"] as const;

const todayIso = (): string => {
  const now = new Date(Date.now() + 8 * 3_600_000);
  return now.toISOString().slice(0, 10);
};

// ── 待办 ───────────────────────────────────────────────────────────────

function TodosTab(): React.JSX.Element {
  const { query, data, setData } = useResource<TodoList>(["me", "todos"], () => spaceApi.todos());
  const { busy, run } = useAction();
  const [content, setContent] = useState("");
  const [priority, setPriority] = useState<TodoItem["priority"]>("normal");
  const [due, setDue] = useState("");
  const [filter, setFilter] = useState<TodoFilter>("open");
  const [keyword, setKeyword] = useState("");
  const rows = useMemo(() => filterTodos(data?.items ?? [], filter, keyword), [data, filter, keyword]);

  const add = async (): Promise<void> => {
    if (!content.trim()) return;
    const next = await run("add", () => spaceApi.addTodo({ content: content.trim(), priority, due: due || null }), "记下了");
    if (next) {
      setData(next);
      setContent("");
      setDue("");
      setPriority("normal");
    }
  };
  const patch = async (item: TodoItem, change: Parameters<typeof spaceApi.editTodo>[1], success?: string): Promise<void> => {
    const next = await run(`todo-${item.id}`, () => spaceApi.editTodo(item.id, change), success);
    if (next) setData(next);
  };

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 4 }} />;
  const overdue = data.items.filter((item) => item.overdue).length;

  return <div className="me-stack">
    <div className="me-composer" role="group" aria-label="新待办">
      <Input
        className="me-composer-main"
        placeholder="要做什么？回车记下"
        maxLength={120}
        value={content}
        onChange={(event) => setContent(event.target.value)}
        onPressEnter={() => void add()}
      />
      <Select<TodoItem["priority"]> value={priority} onChange={setPriority} options={(["high", "normal", "low"] as const).map((value) => ({ value, label: PRIORITY_META[value].label }))} aria-label="优先级" className="me-composer-select" />
      <Input type="date" value={due} min={todayIso()} onChange={(event) => setDue(event.target.value)} aria-label="截止日期" className="me-composer-date" />
      <Button type="primary" icon={<PlusOutlined />} loading={busy === "add"} disabled={!content.trim()} onClick={() => void add()}>添加</Button>
    </div>

    <div className="me-toolbar">
      <Segmented<TodoFilter>
        value={filter}
        onChange={setFilter}
        options={[
          { value: "open", label: `未完成 ${data.pending}` },
          { value: "today", label: "今天要做" },
          { value: "overdue", label: `逾期 ${overdue}` },
          { value: "done", label: `已完成 ${data.done}` },
          { value: "all", label: "全部" },
        ]}
      />
      <Input.Search allowClear placeholder="搜索待办" value={keyword} onChange={(event) => setKeyword(event.target.value)} className="me-toolbar-search" />
      <span className="me-toolbar-spacer" />
      <Text type="secondary" className="me-hint">{data.items.length}/{data.limit} 条</Text>
      {data.done > 0 && (
        <Popconfirm title={`清掉 ${data.done} 条已完成的待办？`} okText="清掉" onConfirm={async () => { const next = await run("clear", () => spaceApi.clearDoneTodos(), (r) => `清掉了 ${r.cleared ?? 0} 条`); if (next) setData(next); }}>
          <Button size="small" loading={busy === "clear"}>清掉已完成</Button>
        </Popconfirm>
      )}
    </div>

    {rows.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={filter === "open" && !keyword ? "没有要做的事，轻松一下～" : "没有符合条件的待办"} />
    ) : (
      <ul className="me-todo-list">
        {rows.map((item) => {
          const label = dueLabel(item.dueInDays);
          return <li key={item.id} className={`me-todo${item.done ? " is-done" : ""}${item.overdue ? " is-overdue" : ""}`}>
            <Checkbox checked={item.done} disabled={busy === `todo-${item.id}`} onChange={(event) => void patch(item, { done: event.target.checked }, event.target.checked ? "完成一件！" : undefined)} aria-label={`完成：${item.content}`} />
            <Paragraph
              className="me-todo-text"
              editable={{ tooltip: "改内容", maxLength: 120, onChange: (value) => { if (value.trim() && value !== item.content) void patch(item, { content: value.trim() }, "改好了"); } }}
            >
              {item.content}
            </Paragraph>
            <span className="me-todo-meta">
              <Select<TodoItem["priority"]>
                size="small"
                variant="borderless"
                value={item.priority}
                onChange={(value) => void patch(item, { priority: value })}
                options={(["high", "normal", "low"] as const).map((value) => ({ value, label: <Tag color={PRIORITY_META[value].color}>{PRIORITY_META[value].label}</Tag> }))}
                aria-label="优先级"
                popupMatchSelectWidth={false}
              />
              {label && <Tag color={item.overdue ? "error" : item.dueInDays === 0 ? "warning" : "default"}>{label}</Tag>}
              {item.done && item.doneAt && <Text type="secondary" className="me-hint">完成于 {item.doneAt.slice(5)}</Text>}
              <Tooltip title={item.due ? `截止 ${item.due}（清空即去掉）` : "设截止日期"}>
                <Input type="date" size="small" className="me-todo-date" value={item.due ?? ""} onChange={(event) => void patch(item, { due: event.target.value || "" })} aria-label="截止日期" />
              </Tooltip>
              <Popconfirm title="删掉这条待办？" okText="删掉" okButtonProps={{ danger: true }} onConfirm={async () => { const next = await run(`del-${item.id}`, () => spaceApi.deleteTodo(item.id)); if (next) setData(next); }}>
                <Button type="text" size="small" icon={<DeleteOutlined />} aria-label="删除" loading={busy === `del-${item.id}`} />
              </Popconfirm>
            </span>
          </li>;
        })}
      </ul>
    )}
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
  const rows = useMemo(() => filterNotes(data?.items ?? [], keyword, activeTag), [data, keyword, activeTag]);

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 4 }} />;

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
      <Text type="secondary" className="me-hint">{data.items.length}/{data.limit} 条</Text>
    </div>

    {rows.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data.items.length ? "没有匹配的笔记" : "还没有笔记。也可以在私聊里说“帮我记一下……”"} />
    ) : (
      <div className="me-note-grid">
        {rows.map((item) => (
          <article key={item.id} className={`me-note${item.pinned ? " is-pinned" : ""}`}>
            {editing === item.id ? (
              <>
                <Input.TextArea autoFocus autoSize={{ minRows: 2, maxRows: 8 }} maxLength={300} value={draft} onChange={(event) => setDraft(event.target.value)} />
                <div className="me-note-actions">
                  <Button size="small" onClick={() => setEditing(null)}>取消</Button>
                  <Button size="small" type="primary" loading={busy === `note-${item.id}`} disabled={!draft.trim()} onClick={() => void save(item.id)}>保存</Button>
                </div>
              </>
            ) : (
              <>
                <Paragraph className="me-note-text" ellipsis={{ rows: 6, expandable: true, symbol: "展开" }}>{item.content}</Paragraph>
                <div className="me-note-foot">
                  {item.tag && <Tag className="me-note-tag" onClick={() => setActiveTag(item.tag)}>#{item.tag}</Tag>}
                  <Text type="secondary" className="me-hint">{item.at}{item.editedAt ? " · 改过" : ""}</Text>
                  <span className="me-toolbar-spacer" />
                  <Tooltip title={item.pinned ? "取消置顶" : "置顶"}>
                    <Button type="text" size="small" aria-label={item.pinned ? "取消置顶" : "置顶"} icon={item.pinned ? <PushpinFilled /> : <PushpinOutlined />} onClick={async () => { const next = await run(`pin-${item.id}`, () => spaceApi.editNote(item.id, { pinned: !item.pinned })); if (next) setData(next); }} />
                  </Tooltip>
                  <Tooltip title="复制">
                    <Button type="text" size="small" aria-label="复制" icon={<CopyOutlined />} onClick={() => void navigator.clipboard?.writeText(item.content)} />
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

  if (query.error && !data) return <QueryErrorAlert error={query.error} onRetry={query.reload} />;
  if (!data) return <Skeleton active paragraph={{ rows: 3 }} />;

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
    <Text type="secondary" className="me-hint">最多 {data.limit} 个。开着“提醒”的，Bot 会在临近和当天私聊提醒你。</Text>
    {data.items.length === 0 ? (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有倒数日" />
    ) : (
      <div className="me-countdown-grid">
        {data.items.map((item) => (
          <article key={item.id} className={`me-countdown is-${item.status}`}>
            <Paragraph className="me-countdown-title" editable={{ tooltip: "改名字", maxLength: 40, onChange: async (value) => { if (value.trim() && value !== item.title) { const next = await run(`cd-${item.id}`, () => spaceApi.editCountdown(item.id, { title: value.trim() })); if (next) setData(next); } } }}>{item.title}</Paragraph>
            <strong className="me-countdown-days">{item.daysLeft === 0 ? "今天" : Math.abs(item.daysLeft)}<small>{item.daysLeft === 0 ? "" : " 天"}</small></strong>
            <Text type="secondary" className="me-hint">{countdownLabel(item.daysLeft)} · {item.date}</Text>
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

    {data.items.length > 0 && (
      <ul className="me-plain-list">
        {data.items.slice(0, 30).map((item) => (
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
    )}
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
