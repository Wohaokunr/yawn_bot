import { Button, Input, Segmented, Switch, Tag, Typography } from "antd";
import { Fragment, useMemo, useState } from "react";
import { AdminEmpty } from "../shared";
import type { AgentDebugResponse } from "../types";
import { CopyButton, debugJson } from "./debug-utils";
import { promptAsText, promptStats } from "./trace-analysis";

const { Text } = Typography;
const COLLAPSE_CHARS = 1_600;

const ROLE_COLORS: Record<string, string> = {
  system: "purple",
  user: "blue",
  assistant: "magenta",
  tool: "gold",
};

function Highlight({ text, query }: { text: string; query: string }): React.JSX.Element {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let index = lower.indexOf(needle);
  let guard = 0;
  while (index >= 0 && guard < 500) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(<mark key={`${index}-${guard}`} className="dbg-mark">{text.slice(index, index + needle.length)}</mark>);
    cursor = index + needle.length;
    index = lower.indexOf(needle, cursor);
    guard += 1;
  }
  parts.push(text.slice(cursor));
  return <>{parts.map((part, key) => <Fragment key={key}>{part}</Fragment>)}</>;
}

export function PromptInspector({
  messages,
}: {
  messages: AgentDebugResponse["promptMessages"];
}): React.JSX.Element {
  const [query, setQuery] = useState("");
  const [role, setRole] = useState("all");
  const [wrap, setWrap] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const stats = useMemo(() => promptStats(messages), [messages]);
  const roles = useMemo(() => [...new Set(messages.map((item) => item.role))], [messages]);
  const needle = query.trim();
  const rows = stats.rows.filter((row) => (role === "all" || row.role === role) && (!needle || row.text.toLowerCase().includes(needle.toLowerCase())));
  const hits = needle ? stats.rows.reduce((sum, row) => sum + (row.text.toLowerCase().split(needle.toLowerCase()).length - 1), 0) : 0;

  if (messages.length === 0) return <AdminEmpty description="Prompt 为空" />;
  return <div className="dbg-prompt">
    <div className="dbg-prompt-stats">
      <span><strong>{messages.length}</strong> 条消息</span>
      <span><strong>{stats.chars.toLocaleString()}</strong> 字符</span>
      <span>约 <strong>{stats.tokens.toLocaleString()}</strong> Token</span>
      <div className="dbg-role-bar" aria-label="各角色 Token 占比">
        {Object.entries(stats.byRole).map(([name, tokens]) => <span
          key={name}
          className={`role-${name}`}
          style={{ flexGrow: Math.max(tokens, 1) }}
          title={`${name}：约 ${tokens} Token`}
        />)}
      </div>
    </div>
    <div className="dbg-toolbar">
      <Input allowClear size="small" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="在 Prompt 里搜索" aria-label="搜索 Prompt" className="dbg-toolbar-search" />
      <Segmented size="small" value={role} onChange={(value) => setRole(String(value))} options={[{ value: "all", label: "全部" }, ...roles.map((name) => ({ value: name, label: name }))]} />
      <label className="dbg-inline-switch"><Switch size="small" checked={wrap} onChange={setWrap} /><span>自动换行</span></label>
      <CopyButton text={() => debugJson(messages)} label="复制为 messages JSON" />
      <CopyButton text={() => promptAsText(messages)} label="复制为纯文本" />
    </div>
    {needle && <Text type="secondary" className="dbg-small">“{needle}” 命中 {hits} 处，分布在 {rows.length} 条消息中</Text>}
    <Text type="secondary" className="dbg-small">Token 是按“中文 1 字 ≈ 1 Token、其他 4 字符 ≈ 1 Token”粗估的，只用于比较相对大小。</Text>
    {rows.length === 0 ? <AdminEmpty description="没有匹配的消息" /> : <ol className="dbg-prompt-list">
      {rows.map((row) => {
        const long = row.chars > COLLAPSE_CHARS;
        const open = !long || expanded.has(row.index) || Boolean(needle);
        const text = open ? row.text : `${row.text.slice(0, COLLAPSE_CHARS)}…`;
        return <li key={row.index} className="dbg-prompt-item">
          <div className="dbg-prompt-head">
            <Tag color={ROLE_COLORS[row.role]}>{row.role}</Tag>
            <Text type="secondary">#{row.index + 1}</Text>
            <Text type="secondary">{row.chars.toLocaleString()} 字符 · 约 {row.tokens.toLocaleString()} Token</Text>
            <span className="dbg-grow" />
            <CopyButton text={row.text} label="复制这条消息" />
          </div>
          <pre className={`agent-debug-prompt-content${wrap ? "" : " is-nowrap"}`}><Highlight text={text} query={needle} /></pre>
          {long && !needle && <Button size="small" type="link" onClick={() => setExpanded((current) => {
            const next = new Set(current);
            if (next.has(row.index)) next.delete(row.index); else next.add(row.index);
            return next;
          })}>{open ? "收起" : `展开全部（还有 ${(row.chars - COLLAPSE_CHARS).toLocaleString()} 字符）`}</Button>}
        </li>;
      })}
    </ol>}
  </div>;
}
