import { DownloadOutlined, EditOutlined, WarningOutlined } from "@ant-design/icons";
import { Alert, Button, Collapse, Descriptions, Input, Segmented, Switch, Tag, Tooltip, Typography } from "antd";
import { Fragment, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AdminEmpty } from "../shared";
import { CopyButton, debugJson } from "../agent-debug/debug-utils";
import { downloadJson, formatDuration } from "../agent-debug/trace-analysis";
import {
  curlCommand,
  messageMedia,
  messageText,
  modeLabel,
  newMessageIndexes,
  OUTCOME_META,
  type PromptSnapshot,
  type PromptSpan,
  requestBody,
  SCOPE_LABELS,
  sectionCoverage,
  segmentText,
  snapshotAsText,
  snapshotLabel,
  spansByMessage,
} from "./prompt-meta";

const { Text, Paragraph } = Typography;
const COLLAPSE_CHARS = 2_400;
const ROLE_COLORS: Record<string, string> = { system: "purple", user: "blue", assistant: "magenta", tool: "gold" };

/** 跳到提示词工作台的某个分段（同一个 Agent 工作台里切 tab）。 */
export function useOpenPromptSection(): (sectionId: string) => void {
  const [searchParams, setSearchParams] = useSearchParams();
  return (sectionId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "prompts");
    next.set("prompts.section", sectionId);
    setSearchParams(next);
  };
}

function Highlighted({ text, query }: { text: string; query: string }): React.JSX.Element {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let index = lower.indexOf(needle);
  let guard = 0;
  while (index >= 0 && guard < 400) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(<mark key={`${index}-${guard}`} className="dbg-mark">{text.slice(index, index + needle.length)}</mark>);
    cursor = index + needle.length;
    index = lower.indexOf(needle, cursor);
    guard += 1;
  }
  parts.push(text.slice(cursor));
  return <>{parts.map((part, key) => <Fragment key={key}>{part}</Fragment>)}</>;
}

function spanClass(span: PromptSpan | null): string {
  if (!span) return "";
  const classes = ["ps-seg"];
  if (span.safety) classes.push("is-safety");
  if (span.category === "context") classes.push("is-data");
  if (span.mode && span.mode !== "default") classes.push("is-modified");
  return classes.join(" ");
}

function MessageBody({
  text,
  spans,
  showSections,
  query,
  onOpenSection,
}: {
  text: string;
  spans: PromptSpan[];
  showSections: boolean;
  query: string;
  onOpenSection: (sectionId: string) => void;
}): React.JSX.Element {
  if (!showSections || spans.length === 0) return <Highlighted text={text} query={query} />;
  return <>
    {segmentText(text, spans).map((segment, index) => segment.span
      ? <Tooltip
          key={index}
          title={<span>{segment.span.title ?? segment.sectionId}<br /><code>{segment.sectionId}</code>{segment.span.mode && segment.span.mode !== "default" ? <><br />{SCOPE_LABELS[segment.span.scope ?? ""] ?? ""}{modeLabel(segment.span.mode)}</> : null}<br />点击去改这一段</span>}
        >
          <span
            className={spanClass(segment.span)}
            role="button"
            tabIndex={0}
            data-section={segment.sectionId}
            onClick={() => onOpenSection(segment.sectionId ?? "")}
            onKeyDown={(event) => { if (event.key === "Enter") onOpenSection(segment.sectionId ?? ""); }}
          ><Highlighted text={segment.text} query={query} /></span>
        </Tooltip>
      : <Fragment key={index}><Highlighted text={segment.text} query={query} /></Fragment>)}
  </>;
}

function SectionLegend({ snapshot, onOpenSection }: { snapshot: PromptSnapshot; onOpenSection: (id: string) => void }): React.JSX.Element | null {
  const sections = useMemo(() => {
    const seen = new Map<string, PromptSpan & { chars: number }>();
    for (const span of snapshot.sections) {
      const entry = seen.get(span.sectionId);
      const chars = span.end - span.start;
      if (entry) entry.chars += chars; else seen.set(span.sectionId, { ...span, chars });
    }
    return [...seen.values()];
  }, [snapshot]);
  if (sections.length === 0) return null;
  const coverage = sectionCoverage(snapshot);
  return <div className="ps-legend" aria-label="本次请求包含的提示词分段">
    <Text type="secondary" className="dbg-small">
      本次请求由 {sections.length} 个分段组成，标出的文字占全部消息的 {Math.round(coverage.ratio * 100)}%（其余是历史消息、工具结果等运行数据）。点分段可直接去改。
    </Text>
    <div className="ps-legend-chips">
      {sections.map((span) => <button
        type="button"
        key={span.sectionId}
        className={`ps-chip${span.safety ? " is-safety" : ""}${span.mode && span.mode !== "default" ? " is-modified" : ""}`}
        onClick={() => onOpenSection(span.sectionId)}
        title={`${span.sectionId} · ${span.chars} 字`}
      >
        {span.safety && <WarningOutlined aria-hidden />}
        <span>{span.title ?? span.sectionId}</span>
        {span.mode && span.mode !== "default" && <em>{modeLabel(span.mode)}</em>}
      </button>)}
    </div>
  </div>;
}

function SnapshotMessages({
  snapshot,
  previous,
  onOpenSection,
}: {
  snapshot: PromptSnapshot;
  previous: PromptSnapshot | null;
  onOpenSection: (id: string) => void;
}): React.JSX.Element {
  const [query, setQuery] = useState("");
  const [role, setRole] = useState("all");
  const [wrap, setWrap] = useState(true);
  const [showSections, setShowSections] = useState(true);
  const [onlyNew, setOnlyNew] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const spanMap = useMemo(() => spansByMessage(snapshot.sections), [snapshot]);
  const added = useMemo(() => newMessageIndexes(previous, snapshot), [previous, snapshot]);
  const roles = useMemo(() => [...new Set(snapshot.messages.map((item) => item.role))], [snapshot]);
  const needle = query.trim();
  const rows = snapshot.messages
    .map((message, index) => ({ message, index, text: messageText(message), stat: snapshot.messageStats[index] }))
    .filter((row) => (role === "all" || row.message.role === role)
      && (!onlyNew || added.has(row.index))
      && (!needle || `${row.text}\n${debugJson(row.message.tool_calls ?? "")}`.toLowerCase().includes(needle.toLowerCase())));

  return <div className="ps-messages">
    <div className="dbg-toolbar">
      <Input allowClear size="small" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="在完整提示词里搜索" aria-label="搜索完整提示词" className="dbg-toolbar-search" />
      <Segmented size="small" value={role} onChange={(value) => setRole(String(value))} options={[{ value: "all", label: "全部" }, ...roles.map((name) => ({ value: name, label: name }))]} />
      <label className="dbg-inline-switch"><Switch size="small" checked={showSections} onChange={setShowSections} /><span>标出分段</span></label>
      {previous && previous.task === snapshot.task && <label className="dbg-inline-switch"><Switch size="small" checked={onlyNew} onChange={setOnlyNew} /><span>只看比上一轮新增（{added.size}）</span></label>}
      <label className="dbg-inline-switch"><Switch size="small" checked={wrap} onChange={setWrap} /><span>自动换行</span></label>
    </div>
    {rows.length === 0 ? <AdminEmpty description="没有匹配的消息" /> : <ol className="dbg-prompt-list">
      {rows.map(({ message, index, text, stat }) => {
        const long = text.length > COLLAPSE_CHARS;
        const open = !long || expanded.has(index) || Boolean(needle);
        const shown = open ? text : text.slice(0, COLLAPSE_CHARS);
        const spans = open ? (spanMap.get(index) ?? []) : (spanMap.get(index) ?? []).filter((span) => span.start < COLLAPSE_CHARS);
        const media = messageMedia(message);
        return <li key={index} className={`dbg-prompt-item${added.has(index) ? " ps-is-new" : ""}`} data-message-index={index}>
          <div className="dbg-prompt-head">
            <Tag color={ROLE_COLORS[message.role]}>{message.role}</Tag>
            <Text type="secondary">#{index + 1}</Text>
            {stat && <Text type="secondary">{stat.chars.toLocaleString()} 字符 · 约 {stat.tokens.toLocaleString()} Token</Text>}
            {message.tool_call_id && <Text type="secondary" code>{message.tool_call_id}</Text>}
            {added.has(index) && <Tag color="green">本轮新增</Tag>}
            <span className="dbg-grow" />
            <CopyButton text={text} label="复制这条消息" />
          </div>
          {text && <pre className={`agent-debug-prompt-content${wrap ? "" : " is-nowrap"}`}>
            <MessageBody text={open ? shown : `${shown}`} spans={spans} showSections={showSections} query={needle} onOpenSection={onOpenSection} />
            {!open && "…"}
          </pre>}
          {media.length > 0 && <div className="ps-media">{media.map((item, mediaIndex) => <Tag key={mediaIndex} className="dbg-tag-soft">{item}</Tag>)}</div>}
          {(message.tool_calls ?? []).length > 0 && <pre className="agent-debug-raw ps-calls">{debugJson(message.tool_calls)}</pre>}
          {long && !needle && <Button size="small" type="link" onClick={() => setExpanded((current) => {
            const next = new Set(current);
            if (next.has(index)) next.delete(index); else next.add(index);
            return next;
          })}>{open ? "收起" : `展开全部（还有 ${(text.length - COLLAPSE_CHARS).toLocaleString()} 字符）`}</Button>}
        </li>;
      })}
    </ol>}
  </div>;
}

function SnapshotTools({ snapshot, onOpenSection }: { snapshot: PromptSnapshot; onOpenSection: (id: string) => void }): React.JSX.Element {
  if (snapshot.tools.length === 0) return <AdminEmpty description="这次请求没有带 tools 参数" />;
  return <Collapse
    size="small"
    items={snapshot.tools.map((tool, index) => {
      const fn = (tool.function ?? {}) as { name?: string; description?: string; parameters?: unknown };
      const name = String(fn.name ?? `tool-${index}`);
      return {
        key: name,
        label: <span className="ps-tool-label"><Text strong code>{name}</Text><Text type="secondary" ellipsis>{fn.description}</Text></span>,
        extra: <Tooltip title="改这个工具的描述"><Button size="small" type="text" icon={<EditOutlined />} aria-label={`改 ${name} 的描述`} onClick={(event) => { event.stopPropagation(); onOpenSection(`tool.${name}`); }} /></Tooltip>,
        children: <>
          <Paragraph className="ps-tool-desc">{fn.description || <Text type="secondary">（描述为空）</Text>}</Paragraph>
          <pre className="agent-debug-raw">{debugJson(fn.parameters)}</pre>
        </>,
      };
    })}
  />;
}

function SnapshotResponse({ snapshot }: { snapshot: PromptSnapshot }): React.JSX.Element {
  const response = snapshot.response;
  if (!response) {
    return <AdminEmpty description={snapshot.kind === "unsent" ? "调试“仅构建提示词”没有调用模型" : `没有拿到模型回复（${OUTCOME_META[snapshot.outcome]?.label ?? snapshot.outcome}）`} />;
  }
  return <div className="ps-response">
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} items={[
      { key: "finish", label: "结束原因", children: response.finishReason ?? "—" },
      { key: "model", label: "服务商返回模型", children: response.model ?? "—" },
      { key: "prompt", label: "输入 Token", children: response.usage.prompt_tokens ?? "—" },
      { key: "completion", label: "输出 Token", children: response.usage.completion_tokens ?? "—" },
      { key: "cached", label: "缓存命中 Token", children: response.usage.cached_tokens ?? "—" },
      { key: "reasoning", label: "推理 Token", children: response.usage.reasoning_tokens ?? "—" },
    ]} />
    {response.reasoning && <Collapse size="small" items={[{ key: "reasoning", label: `推理内容（${response.reasoningChars.toLocaleString()} 字符）`, children: <pre className="agent-debug-prompt-content">{response.reasoning}</pre> }]} />}
    <div className="dbg-prompt-head"><Tag color="magenta">assistant</Tag><Text type="secondary">{response.contentChars.toLocaleString()} 字符</Text><span className="dbg-grow" /><CopyButton text={response.content} label="复制回复正文" /></div>
    {response.content ? <pre className="agent-debug-prompt-content">{response.content}</pre> : <Text type="secondary">（没有正文）</Text>}
    {response.toolCalls.length > 0 && <>
      <Text strong>工具调用 {response.toolCalls.length}</Text>
      {response.toolCalls.map((call) => <div key={call.id || call.name} className="ps-call">
        <Text code>{call.name}</Text> <Text type="secondary">{call.id}</Text>
        <pre className="agent-debug-raw">{call.arguments}</pre>
      </div>)}
    </>}
  </div>;
}

export function PromptSnapshotView({
  snapshots,
  emptyHint,
  filenamePrefix = "prompt",
  onOpenSection,
}: {
  snapshots: PromptSnapshot[];
  emptyHint?: React.ReactNode;
  filenamePrefix?: string;
  onOpenSection?: (sectionId: string) => void;
}): React.JSX.Element {
  const fallbackOpen = useOpenPromptSection();
  const openSection = onOpenSection ?? fallbackOpen;
  const [selected, setSelected] = useState(0);
  const [view, setView] = useState("messages");
  if (snapshots.length === 0) return <AdminEmpty description={emptyHint ?? "这次执行没有抓到模型请求"} />;
  const index = Math.min(selected, snapshots.length - 1);
  const snapshot = snapshots[index];
  const previous = snapshots.slice(0, index).reverse().find((item) => item.task === snapshot.task) ?? null;
  const outcome = OUTCOME_META[snapshot.outcome] ?? { label: snapshot.outcome, color: "default" };

  return <div className="ps-view">
    {snapshots.length > 1 && <div className="ps-rounds" role="tablist" aria-label="本次执行的模型请求">
      {snapshots.map((item, itemIndex) => <button
        key={item.id}
        type="button"
        role="tab"
        aria-selected={itemIndex === index}
        className={`ps-round${itemIndex === index ? " is-active" : ""}`}
        onClick={() => setSelected(itemIndex)}
      >
        <span className="ps-round-title">{snapshotLabel(item)}</span>
        <span className="ps-round-meta">{item.totals.messages} 条 · 约 {item.totals.tokens.toLocaleString()} Token · {formatDuration(item.durationMs)}</span>
        <span className={`ps-dot is-${OUTCOME_META[item.outcome]?.color ?? "default"}`} aria-label={OUTCOME_META[item.outcome]?.label ?? item.outcome} />
      </button>)}
    </div>}

    <div className="ps-head">
      <div className="ps-head-tags">
        <Tag color={outcome.color}>{outcome.label}</Tag>
        <Tag className="dbg-tag-soft">{snapshotLabel(snapshot)}</Tag>
        {snapshot.model && <Text code>{snapshot.model}</Text>}
        {snapshot.provider && <Text type="secondary">服务商 {snapshot.provider}{snapshot.profile ? ` · ${snapshot.profile} 档` : ""}</Text>}
        <Text type="secondary">{formatDuration(snapshot.durationMs)}</Text>
      </div>
      <div className="ps-head-actions">
        <CopyButton text={() => debugJson(requestBody(snapshot))} label="复制为请求体 JSON" />
        <CopyButton text={() => curlCommand(snapshot)} label="复制为 curl 命令（不含密钥）" />
        <CopyButton text={() => snapshotAsText(snapshot)} label="复制为纯文本" />
        <Tooltip title="下载这次请求 JSON"><Button size="small" type="text" aria-label="下载这次请求 JSON" icon={<DownloadOutlined />} onClick={() => downloadJson(`${filenamePrefix}-${snapshot.task}-${snapshot.round}.json`, snapshot)} /></Tooltip>
      </div>
    </div>

    <div className="ps-kpis">
      <span><strong>{snapshot.totals.messages}</strong> 条消息</span>
      <span><strong>{snapshot.totals.chars.toLocaleString()}</strong> 字符</span>
      <span>约 <strong>{snapshot.totals.tokens.toLocaleString()}</strong> Token</span>
      <span><strong>{snapshot.tools.length}</strong> 个工具 · 约 {snapshot.totals.toolsTokens.toLocaleString()} Token</span>
      {snapshot.response?.usage.prompt_tokens != null && <span>服务商计费输入 <strong>{snapshot.response.usage.prompt_tokens.toLocaleString()}</strong></span>}
      <div className="dbg-role-bar" aria-label="各角色 Token 占比">
        {Object.entries(snapshot.totals.byRole).map(([name, tokens]) => <span key={name} className={`role-${name}`} style={{ flexGrow: Math.max(tokens, 1) }} title={`${name}：约 ${tokens} Token`} />)}
      </div>
    </div>

    {(snapshot.redactions > 0 || snapshot.truncated) && <Alert
      type={snapshot.truncated ? "warning" : "info"}
      showIcon
      className="ps-alert"
      message={snapshot.truncated ? `这次请求太长，快照里截掉了 ${snapshot.omittedChars.toLocaleString()} 字符（实际发送时是完整的）` : "除下面说明的脱敏外，内容与实际发送逐字一致"}
      description={snapshot.redactions > 0 ? `${snapshot.redactions} 处链接、本机路径或图片/文件内容块按 Trace 规则替换成了占位（如“[链接:example.com]”“[内嵌媒体 image/png · 12KB]”），其余逐字保留。` : undefined}
    />}

    <SectionLegend snapshot={snapshot} onOpenSection={openSection} />

    <Segmented
      value={view}
      onChange={(value) => setView(String(value))}
      options={[
        { value: "messages", label: `messages ${snapshot.messages.length}` },
        { value: "tools", label: `tools ${snapshot.tools.length}` },
        { value: "params", label: "请求参数" },
        { value: "response", label: "模型回复" },
        { value: "raw", label: "原始 JSON" },
      ]}
    />
    {view === "messages" && <SnapshotMessages key={snapshot.id} snapshot={snapshot} previous={previous} onOpenSection={openSection} />}
    {view === "tools" && <SnapshotTools snapshot={snapshot} onOpenSection={openSection} />}
    {view === "params" && <Descriptions size="small" bordered column={1} items={[
      { key: "task", label: "任务", children: <Text code>{snapshot.task}</Text> },
      { key: "model", label: "model", children: snapshot.model ?? "—" },
      { key: "timeout", label: "超时", children: snapshot.timeoutSeconds != null ? `${snapshot.timeoutSeconds} 秒` : "—" },
      ...Object.entries(snapshot.params).map(([key, value]) => ({ key, label: key, children: <Text code>{typeof value === "string" ? value : JSON.stringify(value)}</Text> })),
      ...(Object.keys(snapshot.params).includes("max_tokens") ? [] : [{ key: "max_tokens", label: "max_tokens", children: <Text type="secondary">未发送（不限制输出长度，由推理等级控制）</Text> }]),
      { key: "captured", label: "抓取时间", children: snapshot.capturedAt },
    ]} />}
    {view === "response" && <SnapshotResponse snapshot={snapshot} />}
    {view === "raw" && <pre className="agent-debug-raw">{debugJson(requestBody(snapshot))}</pre>}
  </div>;
}
