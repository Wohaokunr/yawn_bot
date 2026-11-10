import { BulbOutlined, CalendarOutlined, CheckSquareOutlined, CommentOutlined, FieldTimeOutlined, FileTextOutlined, RightOutlined, SearchOutlined, SmileOutlined } from "@ant-design/icons";
import { Empty, Input, Skeleton, Tag, Typography } from "antd";
import type { InputRef } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { USER_NAV } from "../nav-config";
import { PageHeader, QueryErrorAlert } from "../shared";
import { Highlight } from "./hub-ui";
import { hubApi, type SearchResult } from "./me-hub-api";
import { MeSection } from "./space-ui";
import "./me.css";
import "./me-space.css";
import "./me-v3.css";

const { Text } = Typography;

const KIND_ICON: Record<string, React.ReactNode> = {
  todos: <CheckSquareOutlined />,
  notes: <FileTextOutlined />,
  countdowns: <CalendarOutlined />,
  reminders: <FieldTimeOutlined />,
  moods: <SmileOutlined />,
  memory: <BulbOutlined />,
  chats: <CommentOutlined />,
};

const RECENT_KEY = "yawnbot.me.search.recent";

function readRecent(): string[] {
  try {
    const raw = JSON.parse(window.localStorage.getItem(RECENT_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string").slice(0, 8) : [];
  } catch {
    return [];
  }
}

function pushRecent(term: string): string[] {
  const next = [term, ...readRecent().filter((item) => item !== term)].slice(0, 8);
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // 存不了就不记
  }
  return next;
}

export function MySearchPage(): React.JSX.Element {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [recent, setRecent] = useState<string[]>(readRecent);
  const [kind, setKind] = useState<string>("all");
  const input = useRef<InputRef>(null);

  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => { setDraft(q); }, [q]);

  // 打字停 300ms 自动搜，不用按回车
  useEffect(() => {
    const term = draft.trim();
    if (term === q) return undefined;
    const timer = window.setTimeout(() => {
      const next = new URLSearchParams(params);
      if (term) next.set("q", term);
      else next.delete("q");
      setParams(next, { replace: true });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [draft, q, params, setParams]);

  useEffect(() => {
    if (!q) {
      setResult(null);
      setError("");
      return undefined;
    }
    let alive = true;
    setLoading(true);
    hubApi.search(q)
      .then((data) => { if (alive) { setResult(data); setError(""); setKind("all"); if (data.total > 0) setRecent(pushRecent(q)); } })
      .catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [q]);

  const pages = useMemo(() => {
    const needle = q.toLowerCase();
    if (!needle) return [];
    return USER_NAV.filter((entry) => entry.key !== "/me/search" && `${entry.label} ${entry.description} ${entry.keywords ?? ""}`.toLowerCase().includes(needle));
  }, [q]);
  const groups = (result?.groups ?? []).filter((group) => kind === "all" || group.kind === kind);

  return <div className="me-page">
    <PageHeader title="搜一搜" subtitle="一次搜遍你自己的东西：待办、笔记、倒数日、提醒、心情备注、Bot 记得你的事、聊天记录。只搜你自己的。" />
    <Input
      ref={input}
      size="large"
      allowClear
      prefix={<SearchOutlined aria-hidden="true" />}
      placeholder="想找什么？"
      maxLength={40}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onPressEnter={() => { const first = groups[0]?.items[0]; if (first) navigate(first.to); }}
      aria-label="搜索我的东西"
      className="me-search-input"
    />
    {error && <QueryErrorAlert error={error} onRetry={() => setParams(new URLSearchParams(params), { replace: true })} />}
    {!q && (
      <MeSection title="最近搜过" hint="只记在这台设备上">
        {recent.length === 0 ? <Text type="secondary" className="me-hint">还没搜过。也可以在任何页面按 / 直接来这里。</Text> : (
          <div className="me-chip-row">
            {recent.map((term) => <Tag key={term} className="me-clickable-tag" onClick={() => setDraft(term)}>{term}</Tag>)}
            <Tag className="me-clickable-tag" onClick={() => { try { window.localStorage.removeItem(RECENT_KEY); } catch { /* 忽略 */ } setRecent([]); }}>清空</Tag>
          </div>
        )}
      </MeSection>
    )}
    {q && loading && !result && <Skeleton active paragraph={{ rows: 5 }} />}
    {q && result && (
      <>
        <div className="me-chip-row" role="group" aria-label="按类别筛选">
          <Tag.CheckableTag checked={kind === "all"} onChange={() => setKind("all")}>全部 {result.total}</Tag.CheckableTag>
          {result.groups.map((group) => <Tag.CheckableTag key={group.kind} checked={kind === group.kind} onChange={(on) => setKind(on ? group.kind : "all")}>{group.label} {group.total}</Tag.CheckableTag>)}
        </div>
        {pages.length > 0 && kind === "all" && (
          <MeSection title="页面">
            <div className="me-chip-row">{pages.map((entry) => <Link key={entry.key} to={entry.key} className="me-page-chip">{entry.icon} {entry.label}</Link>)}</div>
          </MeSection>
        )}
        {groups.length === 0 && pages.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`没有找到“${q}”`} />}
        {groups.map((group) => (
          <MeSection key={group.kind} icon={KIND_ICON[group.kind]} title={`${group.label} · ${group.total}${group.more ? "+" : ""}`} hint={group.total > group.items.length ? `只显示前 ${group.items.length} 条` : undefined}>
            <ul className="me-search-list">
              {group.items.map((item) => (
                <li key={item.id}>
                  <Link to={item.to} className="me-search-hit">
                    <span className="me-plain-main"><span className="me-search-title"><Highlight text={item.title} needle={q} /></span>{item.sub && <Text type="secondary" className="me-hint">{item.sub}</Text>}</span>
                    <RightOutlined className="me-card-arrow" />
                  </Link>
                </li>
              ))}
            </ul>
          </MeSection>
        ))}
      </>
    )}
  </div>;
}
