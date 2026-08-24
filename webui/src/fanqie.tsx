import {
  BookOutlined,
  DownloadOutlined,
  FileTextOutlined,
  LinkOutlined,
  ReloadOutlined,
  SearchOutlined,
  TrophyOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Input,
  InputNumber,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Skeleton,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "./api";
import { formatTime, PageHeader, QueryErrorAlert, useApiQuery } from "./shared";
import type {
  FanqieBookSummary,
  FanqieChapterRef,
  FanqieJob,
  FanqieJobDetail,
  FanqieJobStatus,
  FanqieRankCategoryGroup,
  FanqieStatus,
} from "./types";

const { Text, Paragraph } = Typography;

// 与后端 state/commands 的状态语义保持一致;未知值原样展示。
export const FANQIE_JOB_STATUS_META: Record<string, { label: string; color: string }> = {
  queued: { label: "排队中", color: "blue" },
  running: { label: "下载中", color: "processing" },
  completed: { label: "已完成", color: "green" },
  failed: { label: "失败", color: "red" },
  cancelled: { label: "已取消", color: "default" },
};

const FANQIE_SEND_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "待发送", color: "default" },
  sent: { label: "已发送", color: "green" },
  failed: { label: "发送失败", color: "red" },
};

const FANQIE_JOB_STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "queued", label: "排队中" },
  { value: "running", label: "下载中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];

const FANQIE_SEARCH_ORDERS = [
  { value: "related", label: "相关优先" },
  { value: "new", label: "最新" },
  { value: "hot", label: "最热" },
];

const FANQIE_RANK_GENDERS = [
  { value: "male", label: "男频" },
  { value: "female", label: "女频" },
];

const FANQIE_RANK_TYPES = [
  { value: "read", label: "阅读榜" },
  { value: "new", label: "新书榜" },
];

const FQ_COVER_THEME_COUNT = 8;
const FQ_RANK_MEDALS = ["🥇", "🥈", "🥉"];

// 书封主题序号:按 bookId 稳定散列,同一本书永远拿到同一渐变。
export function coverThemeIndex(bookId: string): number {
  let hash = 0;
  for (let i = 0; i < bookId.length; i += 1) {
    hash = (hash * 31 + bookId.charCodeAt(i)) % 100003;
  }
  return hash % FQ_COVER_THEME_COUNT;
}

function coverFirstChar(title: string): string {
  return (title || "").trim().charAt(0) || "书";
}

// 章节范围校验:与后端 submit_job 的口径一致,前端先拦一道给即时反馈。
export function fanqieRangeError(
  start: number | null,
  end: number | null,
  totalChapters: number,
  maxChapters: number,
): string | null {
  if (start === null || end === null) return "请填写起止章节";
  if (start < 1) return "起始章必须从 1 开始";
  if (end < start) return "结束章不能小于起始章";
  if (end > totalChapters) return `本书共 ${totalChapters} 章,结束章超出范围`;
  if (end - start + 1 > maxChapters) return `单次最多下载 ${maxChapters} 章`;
  return null;
}

// 按状态推导可用操作;取消标记后等待 worker 收尾,不可重复取消。
export function fanqieJobActions(job: FanqieJob): {
  cancel: boolean;
  retry: boolean;
  send: boolean;
} {
  const active = job.status === "queued" || job.status === "running";
  return {
    cancel: active && !job.cancelRequested,
    retry: job.status === "failed" || job.status === "cancelled",
    send: job.status === "completed",
  };
}

function jobStatusTag(status: string): React.JSX.Element {
  const meta = FANQIE_JOB_STATUS_META[status];
  // 已知状态用专属胶囊(彩点+浅底);未知值回退默认 Tag。
  return <Tag className={meta ? `fq-status fq-status-${status}` : undefined}>{meta?.label ?? status}</Tag>;
}

function sendStatusTag(sendStatus: string): React.JSX.Element {
  const meta = FANQIE_SEND_STATUS_META[sendStatus];
  return <Tag className={meta ? `fq-send fq-send-${sendStatus}` : undefined}>{meta?.label ?? sendStatus}</Tag>;
}

export function FanqiePage(): React.JSX.Element {
  const statusLoad = useCallback(() => api<FanqieStatus>("/fanqie/status").then((r) => r.data), []);
  const statusQuery = useApiQuery(statusLoad, { resources: ["fanqie_job"] });
  // Tab 写入 URL,刷新或分享链接时保持找书/任务上下文。
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") === "jobs" ? "jobs" : "discover";
  const status = statusQuery.data;
  if (!status) {
    return statusQuery.error
      ? <Alert className="section-alert" type="error" showIcon message={statusQuery.error} />
      : <Spin />;
  }
  if (!status.available) {
    return <>
      <PageHeader title="番茄小说" subtitle="公开小说搜索与 TXT 任务管理" />
      <Alert
        className="section-alert"
        type="warning"
        showIcon
        message="番茄小说子插件未加载"
        description="选书、目录与任务管理不可用;子插件随 bot 启动加载后此页自动恢复。"
      />
    </>;
  }
  const active = status.active;
  return <>
    <div className="fq-hero">
      <div className="fq-hero-main">
        <span className="fq-hero-mark">🍅</span>
        <div className="fq-hero-text">
          <h2>番茄小说</h2>
          <p>公开小说搜索与 TXT 任务管理</p>
        </div>
      </div>
      <div className="fq-hero-stats">
        <span className="fq-chip fq-chip-sky">排队 {active?.queued ?? 0}</span>
        <span className="fq-chip fq-chip-tomato">下载中 {active?.running ?? 0}</span>
        <span className="fq-chip fq-chip-mint">单次上限 {status.limits?.maxChapters ?? "?"} 章</span>
      </div>
      <span className="fq-hero-deco fq-hero-deco-book">📖</span>
      <span className="fq-hero-deco fq-hero-deco-spark">✨</span>
    </div>
    <Tabs
      activeKey={tab}
      onChange={(key) => setSearchParams(key === "discover" ? {} : { tab: key }, { replace: true })}
      items={[
        {
          key: "discover",
          label: <span><BookOutlined /> 找书下载</span>,
          children: <DiscoverTab status={status} onSubmitted={() => setSearchParams({ tab: "jobs" }, { replace: true })} />,
        },
        {
          key: "jobs",
          label: <span><UnorderedListOutlined /> 下载任务</span>,
          children: <JobsTab />,
        },
      ]}
    />
  </>;
}

// ── 找书下载:搜索 / 榜单 / 链接·ID ───────────────────────

function DiscoverTab({ status, onSubmitted }: { status: FanqieStatus; onSubmitted: () => void }): React.JSX.Element {
  const [mode, setMode] = useState<"search" | "rank" | "link">("search");
  const [books, setBooks] = useState<FanqieBookSummary[] | null>(null);
  const [selected, setSelected] = useState<FanqieBookSummary | null>(null);
  const clearBooks = () => { setBooks(null); setSelected(null); };
  return <>
    <Segmented
      className="fq-modes"
      value={mode}
      onChange={(value) => { setMode(value as typeof mode); clearBooks(); }}
      options={[
        { value: "search", label: "关键词搜索", icon: <SearchOutlined /> },
        { value: "rank", label: "榜单", icon: <TrophyOutlined /> },
        { value: "link", label: "链接 / ID", icon: <LinkOutlined /> },
      ]}
    />
    {mode === "search" && <SearchPanel onResults={setBooks} searchLimit={status.limits?.searchLimit ?? 5} />}
    {mode === "rank" && <RankPanel onResults={setBooks} />}
    {mode === "link" && <LinkPanel onResult={(book) => setBooks(book ? [book] : null)} />}
    <Card
      className="section-row"
      title="选书结果"
      extra={books !== null && books.length > 0 ? <Text type="secondary">共 {books.length} 本</Text> : null}
    >
      {books === null
        ? <Empty description="先通过搜索、榜单或链接找书 🍅" />
        : books.length === 0
          ? <Empty description="没有匹配的书,换个关键词试试" />
          : <BookCardGrid books={books} ranked={mode === "rank"} onSelect={setSelected} />}
    </Card>
    <BookDrawer
      book={selected}
      maxChapters={status.limits?.maxChapters ?? 500}
      onClose={() => setSelected(null)}
      onSubmitted={onSubmitted}
    />
  </>;
}

function SearchPanel({ onResults, searchLimit }: { onResults: (books: FanqieBookSummary[]) => void; searchLimit: number }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [keyword, setKeyword] = useState("");
  const [order, setOrder] = useState("related");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const run = async () => {
    const term = keyword.trim();
    if (!term) return;
    setLoading(true);
    setError("");
    try {
      const result = await api<FanqieBookSummary[]>(
        `/fanqie/search?keyword=${encodeURIComponent(term)}&order=${order}`,
      );
      onResults(result.data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "搜索失败");
      message.error(reason instanceof Error ? reason.message : "搜索失败");
    } finally {
      setLoading(false);
    }
  };
  return <Card className="section-row">
    <Alert
      className="section-alert"
      type="info"
      showIcon
      message={`搜索由无头浏览器打开官方搜索页,首次可能需要十几秒;每次最多返回 ${searchLimit} 本。`}
    />
    <Flex gap={12} wrap>
      <Input
        style={{ maxWidth: 360 }}
        placeholder="书名或作者"
        value={keyword}
        onChange={(event) => setKeyword(event.target.value)}
        onPressEnter={run}
        allowClear
      />
      <Select value={order} onChange={setOrder} options={FANQIE_SEARCH_ORDERS} style={{ width: 120 }} />
      <Button type="primary" icon={<SearchOutlined />} loading={loading} onClick={run}>搜索</Button>
    </Flex>
    {error && <Alert className="section-alert" type="error" showIcon message={error} />}
  </Card>;
}

function RankPanel({ onResults }: { onResults: (books: FanqieBookSummary[]) => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const categoriesLoad = useCallback(
    () => api<FanqieRankCategoryGroup[]>("/fanqie/rank/categories").then((r) => r.data),
    [],
  );
  const categoriesQuery = useApiQuery(categoriesLoad);
  const [gender, setGender] = useState("male");
  const [rankType, setRankType] = useState("read");
  const [categoryId, setCategoryId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const groups = categoriesQuery.data ?? [];
  const options = groups.find((group) => group.gender === gender)?.categories ?? [];
  useEffect(() => { setCategoryId(options.length > 0 ? options[0].categoryId : null); }, [gender, categoriesQuery.data]);
  const run = async () => {
    if (!categoryId) return;
    setLoading(true);
    setError("");
    try {
      const result = await api<FanqieBookSummary[]>(
        `/fanqie/rank/books?gender=${gender}&rankType=${rankType}&categoryId=${categoryId}`,
      );
      onResults(result.data);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "榜单读取失败");
      message.error(reason instanceof Error ? reason.message : "榜单读取失败");
    } finally {
      setLoading(false);
    }
  };
  return <Card className="section-row">
    {categoriesQuery.error && !categoriesQuery.data
      ? <QueryErrorAlert error={categoriesQuery.error} onRetry={categoriesQuery.reload} />
      : <>
        <Flex gap={12} wrap>
          <Select value={gender} onChange={setGender} options={FANQIE_RANK_GENDERS} style={{ width: 110 }} />
          <Select value={rankType} onChange={setRankType} options={FANQIE_RANK_TYPES} style={{ width: 110 }} />
          <Select
            value={categoryId}
            onChange={setCategoryId}
            placeholder="分类"
            style={{ minWidth: 200 }}
            loading={categoriesQuery.loading}
            options={options.map((item) => ({ value: item.categoryId, label: item.name }))}
          />
          <Button type="primary" icon={<TrophyOutlined />} loading={loading} disabled={!categoryId} onClick={run}>看榜</Button>
        </Flex>
        {error && <Alert className="section-alert" type="error" showIcon message={error} />}
      </>}
  </Card>;
}

function LinkPanel({ onResult }: { onResult: (book: FanqieBookSummary | null) => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const run = async () => {
    const term = value.trim();
    if (!term) return;
    setLoading(true);
    try {
      const result = await api<FanqieBookSummary>(`/fanqie/resolve?source=${encodeURIComponent(term)}`);
      onResult(result.data);
    } catch (reason) {
      onResult(null);
      message.error(reason instanceof Error ? reason.message : "解析失败");
    } finally {
      setLoading(false);
    }
  };
  return <Card className="section-row">
    <Alert
      className="section-alert"
      type="info"
      showIcon
      message="支持书籍页 / 阅读页链接,或直接粘贴 book ID(纯数字)。"
    />
    <Flex gap={12} wrap>
      <Input
        style={{ maxWidth: 480 }}
        placeholder="https://fanqienovel.com/page/… 或 book ID"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onPressEnter={run}
        allowClear
      />
      <Button type="primary" icon={<LinkOutlined />} loading={loading} onClick={run}>解析</Button>
    </Flex>
  </Card>;
}

// 书封卡片网格:生成式封面(主题渐变+首字),榜单模式下标出名次。
function BookCardGrid({ books, ranked, onSelect }: { books: FanqieBookSummary[]; ranked: boolean; onSelect: (book: FanqieBookSummary) => void }): React.JSX.Element {
  return <div className="fq-book-grid">
    {books.map((book, index) => (
      <div className="fq-book-card" key={book.bookId}>
        <div className={`fq-cover fq-cover-${coverThemeIndex(book.bookId)}`}>
          <span className="fq-cover-char">{coverFirstChar(book.title)}</span>
          {ranked && <span className="fq-rank">{index < FQ_RANK_MEDALS.length ? FQ_RANK_MEDALS[index] : `#${index + 1}`}</span>}
        </div>
        <div className="fq-book-title" title={book.title}>{book.title}</div>
        <div className="fq-book-author" title={book.author}>{book.author || "—"}</div>
        <p className="fq-book-desc">{book.description || "暂无简介"}</p>
        <Button block size="small" type="primary" icon={<DownloadOutlined />} onClick={() => onSelect(book)}>选这本书</Button>
      </div>
    ))}
  </div>;
}

function BookDrawer({ book, maxChapters, onClose, onSubmitted }: {
  book: FanqieBookSummary | null;
  maxChapters: number;
  onClose: () => void;
  onSubmitted: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const chaptersLoad = useCallback(
    () => (book ? api<FanqieChapterRef[]>(`/fanqie/books/${book.bookId}/chapters`).then((r) => r.data) : Promise.resolve(null)),
    [book],
  );
  const chaptersQuery = useApiQuery(chaptersLoad);
  const chapters = chaptersQuery.data ?? [];
  const [start, setStart] = useState<number | null>(1);
  const [end, setEnd] = useState<number | null>(null);
  const [requester, setRequester] = useState<number | null>(null);
  const [groupId, setGroupId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (chapters.length > 0) { setStart(1); setEnd(chapters.length); }
  }, [chapters]);
  const rangeError = chapters.length > 0 ? fanqieRangeError(start, end, chapters.length, maxChapters) : null;
  const submit = async () => {
    if (!book || rangeError || requester === null) return;
    setSubmitting(true);
    try {
      await api<{ jobId: number }>("/fanqie/jobs", {
        method: "POST",
        body: JSON.stringify({
          source: book.bookId,
          startChapter: start,
          endChapter: end,
          requesterUserId: requester,
          ...(groupId !== null ? { groupId } : {}),
        }),
      });
      message.success("任务已提交,可在「下载任务」中跟踪进度");
      onClose();
      onSubmitted();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };
  return <Drawer
    open={book !== null}
    onClose={onClose}
    width="min(860px, 100%)"
    title={book ? <Space>{book.title}<Text type="secondary">{book.author}</Text></Space> : "书籍"}
  >
    {!book
      ? <Spin />
      : <>
        <div className="fq-book-head">
          <div className={`fq-cover fq-cover-lg fq-cover-${coverThemeIndex(book.bookId)}`}>
            <span className="fq-cover-char">{coverFirstChar(book.title)}</span>
          </div>
          <div className="fq-book-info">
            <div className="fq-book-info-title" title={book.title}>{book.title}</div>
            <Text type="secondary">{book.author}</Text>
            <Text type="secondary" copyable className="fq-book-id">{book.bookId}</Text>
            <Paragraph ellipsis={{ rows: 3, expandable: true }}>{book.description || "暂无简介"}</Paragraph>
            {chapters.length > 0 && <div className="fq-chapter-chips">
              <span className="fq-chip fq-chip-sakura">共 {chapters.length} 章</span>
              <span className="fq-chip fq-chip-mint">公开 {chapters.length - chapters.filter((c) => c.isLocked).length}</span>
              <span className="fq-chip fq-chip-gold">锁定 {chapters.filter((c) => c.isLocked).length}</span>
            </div>}
          </div>
        </div>
        {chaptersQuery.error && !chaptersQuery.data
          ? <QueryErrorAlert error={chaptersQuery.error} onRetry={chaptersQuery.reload} />
          : <>
            <Card className="section-row" size="small" title="目录">
              {chaptersQuery.loading && chapters.length === 0
                ? <Skeleton active title={false} paragraph={{ rows: 6 }} />
                : <Table
                    rowKey="itemId"
                    size="small"
                    dataSource={chapters}
                    pagination={{ pageSize: 10, showSizeChanger: false }}
                    columns={[
                      { title: "章", dataIndex: "index", width: 70 },
                      { title: "标题", dataIndex: "title", ellipsis: true },
                      { title: "状态", width: 90, render: (_, row: FanqieChapterRef) => row.isLocked ? <Tag color="orange">锁定</Tag> : <Tag color="green">公开</Tag> },
                    ]}
                  />}
            </Card>
            <Card size="small" title="提交下载任务">
              <Flex vertical gap={12}>
                <Flex gap={12} wrap align="center">
                  <span>下载范围:第</span>
                  <InputNumber min={1} max={chapters.length || undefined} precision={0} value={start} onChange={setStart} style={{ width: 100 }} />
                  <span>章 至 第</span>
                  <InputNumber min={1} max={chapters.length || undefined} precision={0} value={end} onChange={setEnd} style={{ width: 100 }} />
                  <span>章(全书 {chapters.length} 章)</span>
                </Flex>
                {rangeError && <Alert type="error" showIcon message={rangeError} />}
                <Flex gap={12} wrap align="center">
                  <span>接收人 QQ(成品私发):</span>
                  <InputNumber min={1} precision={0} placeholder="QQ 号" value={requester} onChange={setRequester} style={{ width: 160 }} />
                  <span>群号(选填,仅影响群播报与配额):</span>
                  <InputNumber min={1} precision={0} placeholder="可留空" value={groupId} onChange={setGroupId} style={{ width: 160 }} />
                </Flex>
                <Alert type="info" showIcon message={`单次最多 ${maxChapters} 章;文件投递依赖 bot 与接收人为好友。`} />
                <div>
                  <Button type="primary" icon={<DownloadOutlined />} loading={submitting} disabled={!!rangeError || requester === null || chapters.length === 0} onClick={submit}>
                    提交任务
                  </Button>
                </div>
              </Flex>
            </Card>
          </>}
      </>}
  </Drawer>;
}

// ── 下载任务:列表 / 操作 / 详情 ───────────────────────────

function JobsTab(): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const load = useCallback(
    () => api<FanqieJob[]>(`/fanqie/jobs?page=${page}&pageSize=20&search=${encodeURIComponent(search)}&status=${status}`).then((r) => ({ rows: r.data, total: r.meta.total ?? 0 })),
    [page, search, status],
  );
  const query = useApiQuery(load, { resources: ["fanqie_job"] });
  // 任务进度靠轮询兜底(entity.changed 只覆盖 WebUI 发起的写操作);
  // 页面隐藏时暂停,回到前台立即补一次。
  const reload = query.reload;
  useEffect(() => {
    const tick = () => { if (!document.hidden) reload(); };
    const timer = window.setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [reload]);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [actingId, setActingId] = useState(0);
  const act = async (job: FanqieJob, action: "cancel" | "retry" | "send" | "delete") => {
    setActingId(job.id);
    try {
      if (action === "delete") {
        await api(`/fanqie/jobs/${job.id}`, { method: "DELETE" });
        message.success(`任务 #${job.id} 已删除`);
      } else {
        const result = await api<{ message?: string }>(`/fanqie/jobs/${job.id}/${action}`, { method: "POST" });
        message.success(result.data.message ?? `任务 #${job.id} ${action === "cancel" ? "已提交取消" : action === "retry" ? "已重新排队" : "已发送"}`);
      }
      reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setActingId(0);
    }
  };
  const columns: ColumnsType<FanqieJob> = [
    { title: "任务", width: 70, render: (_, row) => <Text strong>#{row.id}</Text> },
    {
      title: "书",
      render: (_, row) => <>{row.title || "未知书名"}<br /><Text type="secondary">{row.author || "未知作者"} · 第 {row.startChapter}-{row.endChapter} 章</Text></>,
    },
    {
      title: "进度",
      width: 150,
      render: (_, row) => <Progress
        size="small"
        percent={row.totalChapters > 0 ? Math.round((row.completedChapters / row.totalChapters) * 100) : 0}
        format={() => `${row.completedChapters}/${row.totalChapters}`}
        status={row.status === "failed" ? "exception" : row.status === "completed" ? "success" : "active"}
      />,
    },
    {
      title: "状态",
      width: 110,
      render: (_, row) => <Space direction="vertical" size={0}>{jobStatusTag(row.status)}{row.cancelRequested && row.status !== "cancelled" && <Tag className="fq-status fq-status-cancelling">取消中</Tag>}</Space>,
    },
    { title: "发送", dataIndex: "sendStatus", width: 100, render: (value: string) => sendStatusTag(value) },
    {
      title: "请求者 / 群",
      render: (_, row) => <><Text copyable>{row.requesterUserId}</Text><br /><Text type="secondary">{row.groupName || row.groupId || "私聊"}</Text></>,
    },
    { title: "创建时间", dataIndex: "createdAt", render: formatTime },
    {
      title: "操作",
      width: 260,
      render: (_, row) => {
        const actions = fanqieJobActions(row);
        return <Space size={0} wrap>
          <Button type="link" size="small" icon={<FileTextOutlined />} onClick={() => setDetailId(row.id)}>详情</Button>
          {actions.cancel && <Popconfirm title="取消这个任务?" onConfirm={() => act(row, "cancel")}><Button type="link" size="small" loading={actingId === row.id}>取消</Button></Popconfirm>}
          {actions.retry && <Button type="link" size="small" loading={actingId === row.id} onClick={() => act(row, "retry")}>重试</Button>}
          {actions.send && <Button type="link" size="small" loading={actingId === row.id} onClick={() => act(row, "send")}>发送</Button>}
          <Popconfirm title={`删除任务 #${row.id}?`} description="同时清理临时章节与成品文件,不可撤销。" onConfirm={() => act(row, "delete")}>
            <Button type="link" size="small" danger>删除</Button>
          </Popconfirm>
        </Space>;
      },
    },
  ];
  return <Card extra={<Space>
    <Input.Search placeholder="搜索书名 / 作者 / QQ / 任务号" allowClear onSearch={(value) => { setSearch(value); setPage(1); }} />
    <Select value={status} onChange={(value) => { setStatus(value); setPage(1); }} options={FANQIE_JOB_STATUS_OPTIONS} style={{ width: 120 }} />
    <Button icon={<ReloadOutlined />} onClick={reload} />
  </Space>}>
    {query.error && !query.data
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : <Table
          rowKey="id"
          size="small"
          columns={columns}
          loading={query.loading}
          dataSource={query.data?.rows ?? []}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无下载任务,先去「找书下载」挑一本吧 🍅" /> }}
          pagination={{ current: page, pageSize: 20, total: query.data?.total ?? 0, showSizeChanger: false, onChange: setPage }}
        />}
    <JobDetailDrawer jobId={detailId} onClose={() => setDetailId(null)} onChanged={reload} />
  </Card>;
}

function JobDetailDrawer({ jobId, onClose, onChanged }: { jobId: number | null; onClose: () => void; onChanged: () => void }): React.JSX.Element {
  const [detail, setDetail] = useState<FanqieJobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (jobId === null) return Promise.resolve(null);
    return api<FanqieJobDetail>(`/fanqie/jobs/${jobId}`).then((r) => r.data);
  }, [jobId]);
  // 打开期间 5s 轮询看进度,页面隐藏时暂停。
  useEffect(() => {
    if (jobId === null) { setDetail(null); setError(null); return; }
    let alive = true;
    const tick = async () => {
      if (document.hidden) return;
      try {
        const data = await load();
        if (alive) { setDetail(data); setError(null); }
      } catch (reason) {
        if (alive) setError(reason instanceof Error ? reason.message : "加载失败");
      }
    };
    void tick();
    const timer = window.setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [load, jobId]);
  const job = detail?.job ?? null;
  return <Drawer
    open={jobId !== null}
    onClose={onClose}
    width="min(860px, 100%)"
    title={job ? <Space>#{job.id} {job.title || "未知书名"}{jobStatusTag(job.status)}</Space> : "任务详情"}
    extra={<Button icon={<ReloadOutlined />} onClick={() => { void load().then((data) => { if (data) { setDetail(data); onChanged(); } }).catch(() => undefined); }}>刷新</Button>}
  >
    {error && !job && <QueryErrorAlert error={error} onRetry={() => { void load().then((data) => { if (data) setDetail(data); }).catch(() => undefined); }} />}
    {!job && !error && <Spin />}
    {job && <>
      <Descriptions className="section-row" size="small" column={2} items={[
        { key: "book", label: "书", span: 2, children: `${job.title ?? "未知书名"} · ${job.author ?? "未知作者"}` },
        { key: "range", label: "章节范围", children: `第 ${job.startChapter}-${job.endChapter} 章` },
        { key: "progress", label: "进度", children: `${job.completedChapters} / ${job.totalChapters}` },
        { key: "requester", label: "请求者", children: job.requesterUserId },
        { key: "group", label: "群", children: job.groupName || job.groupId || "私聊" },
        { key: "send", label: "发送状态", children: sendStatusTag(job.sendStatus) },
        { key: "output", label: "成品文件", children: job.outputName ?? "—" },
        { key: "created", label: "创建时间", children: formatTime(job.createdAt) },
        { key: "done", label: "完成时间", children: formatTime(job.completedAt) },
      ]} />
      {job.lastError && <Alert className="section-alert" type="error" showIcon message="任务错误" description={job.lastError} />}
      {job.sendError && <Alert className="section-alert" type="warning" showIcon message="发送错误" description={job.sendError} />}
      <Card size="small" title={`章节(${detail?.chapters.length ?? 0})`}>
        <Table
          rowKey={(row) => `${row.chapterIndex}-${row.itemId}`}
          size="small"
          dataSource={detail?.chapters ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            { title: "章", dataIndex: "chapterIndex", width: 70 },
            { title: "标题", dataIndex: "title", ellipsis: true },
            { title: "锁定", width: 70, render: (_, row) => row.isLocked ? <Tag color="orange">锁</Tag> : "—" },
            {
              title: "状态",
              width: 100,
              render: (_, row) => {
                const meta = row.status === "completed" ? { label: "已完成", color: "green" }
                  : row.status === "unavailable" ? { label: "不可用", color: "orange" }
                  : row.status === "failed" ? { label: "失败", color: "red" }
                  : row.status === "running" ? { label: "下载中", color: "processing" }
                  : { label: "等待", color: "default" };
                return <Tag color={meta.color}>{meta.label}</Tag>;
              },
            },
            { title: "完成时间", dataIndex: "completedAt", render: formatTime },
            { title: "错误", dataIndex: "lastError", ellipsis: true },
          ]}
        />
      </Card>
    </>}
  </Drawer>;
}
