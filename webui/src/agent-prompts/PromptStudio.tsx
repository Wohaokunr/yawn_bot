import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  HistoryOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  UndoOutlined,
  UploadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Checkbox,
  Drawer,
  Input,
  Modal,
  Segmented,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api";
import { CopyButton } from "../agent-debug/debug-utils";
import { collapseDiff, diffStats, downloadJson, estimateTokens, lineDiff } from "../agent-debug/trace-analysis";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentDebugResponse, AgentMessageItem } from "../types";
import {
  bulkApply,
  clearOverride,
  exportOverrides,
  fetchBackgroundPrompts,
  fetchHistory,
  fetchPromptCatalog,
  fetchPromptStatus,
  saveOverride,
} from "./prompt-api";
import {
  baseText,
  categoryCounts,
  composeText,
  filterSections,
  inheritedText,
  isModified,
  KIND_LABELS,
  MODE_META,
  modeLabel,
  parseImport,
  type PromptCatalog,
  type PromptHistoryEntry,
  type PromptMode,
  type PromptScope,
  type PromptSectionRow,
  resetChanges,
  safetyRemovalChanges,
  SCOPE_LABELS,
  SECTION_FILTERS,
  type SectionFilter,
  scopedOverride,
  taskLabel,
  validateDraft,
} from "./prompt-meta";
import { PromptSnapshotView } from "./PromptSnapshotView";

const { Text, Paragraph, Title } = Typography;
const PREVIEW_LINES_CHARS = 360;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function usePromptParams(): {
  scope: PromptScope;
  category: string;
  filter: SectionFilter;
  query: string;
  sectionId: string;
  update: (patch: Record<string, string | null>) => void;
} {
  const [searchParams, setSearchParams] = useSearchParams();
  const scope = searchParams.get("prompts.scope") === "group" ? "group" : "global";
  const category = searchParams.get("prompts.cat") ?? "all";
  const filter = (searchParams.get("prompts.filter") ?? "all") as SectionFilter;
  const query = searchParams.get("prompts.q") ?? "";
  const sectionId = searchParams.get("prompts.section") ?? "";
  const update = (patch: Record<string, string | null>): void => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value === "" || (key === "prompts.cat" && value === "all") || (key === "prompts.filter" && value === "all") || (key === "prompts.scope" && value === "global")) next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next, { replace: true });
  };
  return { scope, category, filter, query, sectionId, update };
}

function SectionBadges({ row }: { row: PromptSectionRow }): React.JSX.Element {
  return <span className="pst-badges">
    {row.safety && <Tag color="red" icon={<SafetyCertificateOutlined />}>安全</Tag>}
    {row.protocol && <Tooltip title="规定了模型的输出格式，改坏了会解析失败"><Tag color="orange">输出协议</Tag></Tooltip>}
    <Tag className="dbg-tag-soft">{KIND_LABELS[row.kind]}</Tag>
    {row.tasks.map((task) => <Tag key={task} className="dbg-tag-soft">{taskLabel(task)}</Tag>)}
  </span>;
}

function EffectiveTag({ row, scope }: { row: PromptSectionRow; scope: PromptScope }): React.JSX.Element {
  const own = scopedOverride(row, scope);
  if (row.effectiveMode === "default" && !own) {
    return <Tag>{scope === "group" && row.globalOverride ? "继承全局" : "默认"}</Tag>;
  }
  const meta = MODE_META[row.effectiveMode];
  const from = row.effectiveScope ? SCOPE_LABELS[row.effectiveScope] : "";
  return <Tooltip title={meta.hint}><Tag color={meta.color}>{from}{meta.label}</Tag></Tooltip>;
}

function SectionCard({
  row,
  scope,
  active,
  onEdit,
  onReset,
}: {
  row: PromptSectionRow;
  scope: PromptScope;
  active: boolean;
  onEdit: () => void;
  onReset: () => void;
}): React.JSX.Element {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (active) ref.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [active]);
  const own = scopedOverride(row, scope);
  const text = row.effectiveText ?? "";
  const disabled = row.effectiveMode === "disable";
  const preview = text.length > PREVIEW_LINES_CHARS ? `${text.slice(0, PREVIEW_LINES_CHARS)}…` : text;
  return <article ref={ref} className={`pst-card${active ? " is-active" : ""}${disabled ? " is-disabled" : ""}${row.safety ? " is-safety" : ""}`} data-section-id={row.id}>
    <header className="pst-card-head">
      <div className="pst-card-title">
        <Text strong>{row.title}</Text>
        <Text type="secondary" code copyable={{ text: row.id, tooltips: ["复制分段 ID", "已复制"] }}>{row.id}</Text>
      </div>
      <Space size={4} wrap>
        <EffectiveTag row={row} scope={scope} />
        {row.editable && <Button size="small" icon={<EditOutlined />} onClick={onEdit}>编辑</Button>}
        {own && <Tooltip title={scope === "group" ? "去掉本群覆盖，回到继承全局" : "去掉全局覆盖，回到代码默认"}><Button size="small" icon={<UndoOutlined />} onClick={onReset} aria-label={`恢复 ${row.title}`} /></Tooltip>}
      </Space>
    </header>
    <div className="pst-card-meta">
      <SectionBadges row={row} />
      <Text type="secondary" className="dbg-small">{row.layerLabel} · {row.when}</Text>
    </div>
    {row.description && <Paragraph type="secondary" className="pst-card-desc">{row.description}</Paragraph>}
    {disabled
      ? <div className="pst-card-text is-empty">已删除：模型看不到这一段{row.safety ? "。代码里的权限、配额与出站校验仍然有效。" : "。"}</div>
      : text
        ? <pre className="pst-card-text">{preview}</pre>
        : <div className="pst-card-text is-empty">{row.kind === "generated" ? "动态生成：本进程还没跑到这一段，跑一轮对话或在调试台模拟一次就能看到样例。" : "（空）"}</div>}
    <footer className="pst-card-foot">
      <Text type="secondary" className="dbg-small">生效约 {row.effectiveTokens} Token{row.defaultText != null ? ` · 默认 ${row.defaultTokens}` : ""}</Text>
      {row.sample && <Text type="secondary" className="dbg-small">最近出现 {formatTime(row.sample.lastSeenAt)} · 共 {row.sample.seen} 次</Text>}
      {own?.note && <Text type="secondary" className="dbg-small">备注：{own.note}</Text>}
    </footer>
  </article>;
}

function DiffView({ before, after }: { before: string; after: string }): React.JSX.Element {
  const lines = useMemo(() => lineDiff(before, after), [before, after]);
  const stats = diffStats(lines);
  if (stats.added === 0 && stats.removed === 0) return <Text type="secondary">和默认一致</Text>;
  return <div className="pst-diff">
    <Text type="secondary" className="dbg-small">+{stats.added} 行 / −{stats.removed} 行</Text>
    <pre className="agent-debug-raw">
      {collapseDiff(lines).map((line, index) => line.type === "skip"
        ? <div key={index} className="pst-diff-skip">… {line.count} 行相同 …</div>
        : <div key={index} className={`pst-diff-${line.type}`}>{line.type === "add" ? "+ " : line.type === "del" ? "− " : "  "}{line.text}</div>)}
    </pre>
  </div>;
}

function previewMode(row: PromptSectionRow): "dialogue" | "active" | "warmup" | "followup" {
  if (row.tasks.includes("agent_dialogue")) return "dialogue";
  if (row.id === "proactive.followup") return "followup";
  if (row.id === "proactive.warmup" || row.id === "proactive.silence") return "warmup";
  return "active";
}

interface EditorState {
  mode: PromptMode | "inherit";
  text: string;
  note: string;
  confirmSafety: boolean;
}

function initialEditor(row: PromptSectionRow, scope: PromptScope): EditorState {
  const own = scopedOverride(row, scope);
  if (own) return { mode: own.mode, text: own.text || (own.mode === "replace" ? row.effectiveText ?? "" : ""), note: own.note, confirmSafety: false };
  return { mode: "inherit", text: "", note: "", confirmSafety: false };
}

function SectionEditor({
  row,
  scope,
  groupId,
  revision,
  onClose,
  onSaved,
}: {
  row: PromptSectionRow;
  scope: PromptScope;
  groupId: string;
  revision: number;
  onClose: () => void;
  onSaved: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [state, setState] = useState<EditorState>(() => initialEditor(row, scope));
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<PromptHistoryEntry[] | null>(null);
  const [preview, setPreview] = useState<AgentDebugResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const inherited = inheritedText(row, scope);
  const defaultLabel = scope === "group" ? "继承全局" : "默认";
  const textMode = state.mode === "replace" || state.mode === "append" || state.mode === "prepend";
  const result = state.mode === "inherit" ? inherited : state.mode === "builtin" ? baseText(row) : composeText(inherited, state.mode, state.text);
  const error = state.mode === "inherit" ? null : validateDraft(row, state.mode, state.text, state.confirmSafety);
  const modeOptions = [
    { value: "inherit", label: defaultLabel },
    ...row.modes.filter((mode) => scope === "group" || mode !== "builtin").map((mode) => ({ value: mode, label: MODE_META[mode].label })),
  ];

  const setMode = (mode: PromptMode | "inherit"): void => {
    setState((current) => ({
      ...current,
      mode,
      text: mode === "replace" && !current.text ? inherited : current.text,
    }));
  };

  const save = async (): Promise<void> => {
    if (error) { message.error(error); return; }
    setSaving(true);
    try {
      if (state.mode === "inherit") {
        await clearOverride(row.id, scope, groupId, revision);
        message.success(scope === "group" ? "已恢复继承全局" : "已恢复默认");
      } else {
        await saveOverride(row.id, { scope, groupId, mode: state.mode, text: state.text, note: state.note, confirmSafety: state.confirmSafety, expectedRevision: revision });
        message.success(`已保存：${row.title} → ${modeLabel(state.mode)}，下一轮对话立即生效`);
      }
      onSaved();
      onClose();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        message.warning("别处刚改过提示词，已刷新，请确认后再保存");
        onSaved();
      } else {
        message.error(errorText(reason));
      }
    } finally {
      setSaving(false);
    }
  };

  const loadHistory = async (): Promise<void> => {
    try {
      setHistory(await fetchHistory(row.id, scope, groupId));
    } catch (reason) {
      message.error(errorText(reason));
    }
  };

  const runPreview = async (): Promise<void> => {
    setPreviewing(true);
    try {
      const latest = (await api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=1&pageSize=1`)).data[0];
      if (!latest) { message.warning("这个群还没有可回放的消息，先在群里说句话再预览"); return; }
      const body = {
        mode: previewMode(row),
        messageId: Number(latest.messageId),
        runModel: false,
        promptOverrides: { [row.id]: state.mode === "inherit" ? { mode: "inherit", text: "" } : { mode: state.mode, text: state.text } },
      };
      const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, { method: "POST", body: JSON.stringify(body) });
      setPreview(response.data);
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setPreviewing(false);
    }
  };

  return <Drawer
    open
    width={760}
    onClose={onClose}
    title={<span className="pst-drawer-title">{row.safety && <SafetyCertificateOutlined className="pst-safety-icon" />}{row.title}<Text type="secondary" code>{row.id}</Text></span>}
    extra={<Space>
      <Button icon={<EyeOutlined />} loading={previewing} onClick={() => void runPreview()} disabled={!row.tasks.some((task) => task === "agent_dialogue" || task === "agent_proactive")}>预览完整提示词</Button>
      <Button type="primary" loading={saving} disabled={Boolean(error)} onClick={() => void save()}>保存</Button>
    </Space>}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <div className="pst-editor-meta">
        <SectionBadges row={row} />
        <Text type="secondary">{row.layerLabel} · {row.when}</Text>
        {row.description && <Text type="secondary">{row.description}</Text>}
        <Text type="secondary">正在编辑：<Text strong>{scope === "group" ? `本群（${groupId}）` : "全局（所有群）"}</Text>{scope === "group" ? "；本群设置优先于全局。" : "；某个群单独设置过时以本群为准。"}</Text>
      </div>

      {row.safety && <Alert type="warning" showIcon icon={<WarningOutlined />} message="这是安全相关分段" description="删除或替换后，模型就不再看到这条约束，可能更容易被群消息里的指令带偏、说出内部信息或谎称完成。代码里的权限、配额、critical 动作门槛和出站校验不受影响。" />}
      {row.protocol && <Alert type="info" showIcon message="这一段规定了模型的输出格式" description="改动后请用“预览完整提示词”或调试台真实试跑确认模型还能按格式回答，否则该任务会解析失败并降级。" />}

      <div>
        <Text strong>怎么改</Text>
        <Segmented block value={state.mode} onChange={(value) => setMode(value as PromptMode | "inherit")} options={modeOptions} />
        <Text type="secondary" className="dbg-small">{state.mode === "inherit" ? (scope === "group" ? MODE_META.inherit.hint : MODE_META.default.hint) : MODE_META[state.mode].hint}</Text>
      </div>

      {textMode && <div>
        <Text strong>{state.mode === "replace" ? "替换成" : state.mode === "append" ? "追加的文字" : "前置的文字"}</Text>
        <Input.TextArea
          value={state.text}
          onChange={(event) => setState((current) => ({ ...current, text: event.target.value }))}
          autoSize={{ minRows: 4, maxRows: 18 }}
          maxLength={12_000}
          aria-label="分段文字"
          className="pst-textarea"
        />
        <Text type="secondary" className="dbg-small">{state.text.length.toLocaleString()} / 12000 字 · 约 {estimateTokens(state.text)} Token</Text>
      </div>}

      {state.mode !== "inherit" && <Input value={state.note} onChange={(event) => setState((current) => ({ ...current, note: event.target.value }))} placeholder="备注（可选）：为什么这么改" maxLength={200} aria-label="备注" />}

      {row.safety && (state.mode === "disable" || state.mode === "replace") && <Checkbox checked={state.confirmSafety} onChange={(event) => setState((current) => ({ ...current, confirmSafety: event.target.checked }))}>
        我了解删除/替换这条安全约束的影响
      </Checkbox>}

      {error && state.mode !== "inherit" && <Text type="danger">{error}</Text>}

      <div>
        <div className="pst-section-head"><Text strong>保存后模型看到的内容</Text><Text type="secondary">约 {estimateTokens(result)} Token</Text>{result && <CopyButton text={result} label="复制生效文字" />}</div>
        {result ? <pre className="pst-card-text">{result}</pre> : <div className="pst-card-text is-empty">（这一段不会出现在发给模型的请求里）</div>}
      </div>

      <div>
        <div className="pst-section-head"><Text strong>{row.defaultText != null ? "代码默认" : "最近一次动态生成的样例"}</Text>{baseText(row) && <CopyButton text={baseText(row)} label="复制默认文字" />}</div>
        {baseText(row)
          ? <pre className="pst-card-text is-muted">{baseText(row)}</pre>
          : <div className="pst-card-text is-empty">还没有样例：跑一轮对话或在调试台模拟一次。</div>}
        {row.sample?.truncated && <Text type="secondary" className="dbg-small">样例只保留了前 {row.sample.text.length} 字（实际 {row.sample.chars} 字）。</Text>}
      </div>

      {baseText(row) && result !== baseText(row) && <div><Text strong>和代码默认的差异</Text><DiffView before={baseText(row)} after={result} /></div>}

      <div>
        <div className="pst-section-head"><Text strong>修改历史</Text><Button size="small" icon={<HistoryOutlined />} onClick={() => void loadHistory()}>{history ? "刷新" : "查看"}</Button></div>
        {history && (history.length === 0
          ? <Text type="secondary">这一层还没有改过</Text>
          : <ol className="pst-history">
            {history.map((entry, index) => <li key={`${entry.revision}-${index}`}>
              <Text type="secondary">{formatTime(entry.at)} · 第 {entry.revision} 版 · {entry.action === "clear" ? "恢复默认" : entry.override ? modeLabel(entry.override.mode) : "—"}{entry.by ? ` · ${entry.by}` : ""}</Text>
              {entry.override?.text && <pre className="pst-card-text is-muted">{entry.override.text.length > 240 ? `${entry.override.text.slice(0, 240)}…` : entry.override.text}</pre>}
              <Button size="small" type="link" onClick={() => setState((current) => ({ ...current, mode: entry.override?.mode ?? "inherit", text: entry.override?.text ?? "", note: entry.override?.note ?? "" }))}>载入这一版到编辑器</Button>
            </li>)}
          </ol>)}
      </div>

      {preview && <div className="pst-preview">
        <div className="pst-section-head"><Text strong>按草稿构建的完整提示词（用最近一条群消息回放，未调用模型）</Text></div>
        <PromptSnapshotView snapshots={preview.promptSnapshots ?? []} filenamePrefix={`preview-${row.id}`} onOpenSection={() => undefined} />
      </div>}
    </Space>
  </Drawer>;
}

function BackgroundPrompts(): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const query = useApiQuery({
    queryKey: ["agent-background-prompts", open],
    fetcher: (signal) => (open ? fetchBackgroundPrompts(signal) : Promise.resolve(null)),
  });
  return <section className="dbg-panel pst-background">
    <div className="pst-section-head">
      <Text strong>Agent 后台请求</Text>
      <Text type="secondary" className="dbg-small">记忆整理、周摘要、收图转述这类不属于某次群对话的模型请求（全进程最近 16 次）。</Text>
      <span className="dbg-grow" />
      <Button size="small" onClick={() => (open ? query.reload() : setOpen(true))}>{open ? "刷新" : "查看"}</Button>
    </div>
    {open && (query.error
      ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
      : query.data
        ? <PromptSnapshotView snapshots={query.data.prompts} filenamePrefix="background" emptyHint="最近还没有后台请求" />
        : <Skeleton active paragraph={{ rows: 3 }} />)}
  </section>;
}

export function PromptStudio({ groupId }: { groupId: string }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const params = usePromptParams();
  const { scope, category, filter, query, sectionId } = params;
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [busy, setBusy] = useState(false);
  const catalog = useApiQuery<PromptCatalog>({
    queryKey: ["agent-prompt-catalog", groupId],
    fetcher: (signal) => fetchPromptCatalog(groupId, signal),
    invalidation: { resources: ["agent_prompts"] },
  });
  const status = useApiQuery({
    queryKey: ["agent-prompt-status"],
    fetcher: (signal) => fetchPromptStatus(signal),
  });
  const rows = catalog.data?.sections ?? [];
  const counts = useMemo(() => categoryCounts(rows), [rows]);
  const visible = useMemo(() => filterSections(rows, { category, filter, query }), [rows, category, filter, query]);
  const editing = rows.find((row) => row.id === sectionId) ?? null;
  const revision = catalog.data?.revision ?? 0;
  const scopeCount = rows.filter((row) => scopedOverride(row, scope)).length;
  const safetyGone = rows.filter((row) => row.safety && (row.effectiveMode === "disable" || row.effectiveMode === "replace"));

  useEffect(() => {
    if (sectionId && rows.length && !editing) message.warning(`没有找到分段 ${sectionId}`);
  }, [sectionId, rows.length, editing, message]);

  const reload = (): void => { catalog.reload(); status.reload(); };

  const runBulk = async (changes: Array<{ sectionId: string; mode: string; text?: string; note?: string }>, done: string, confirmSafety = false, replaceScope = false): Promise<void> => {
    setBusy(true);
    try {
      const result = await bulkApply({ scope, groupId, changes, confirmSafety, expectedRevision: revision, replaceScope });
      message.success(`${done}（${result.changed} 处）`);
      reload();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("别处刚改过提示词，已刷新，请重试"); reload(); } else message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };

  const removeSafety = (): void => {
    const changes = safetyRemovalChanges(rows, scope);
    if (changes.length === 0) { message.info("这一层的安全分段已经全部删除了"); return; }
    modal.confirm({
      title: `删除${scope === "group" ? "本群" : "全局"}全部 ${changes.length} 个安全分段？`,
      icon: <WarningOutlined />,
      content: <div>
        <Paragraph>模型将不再看到防注入、保密、诚实、工具边界等约束。下面这些代码层保护<Text strong>不受影响</Text>：工具权限分级与二次校验、特权配额、critical 动作门槛、出站消息校验、隐私退出过滤。</Paragraph>
        <Paragraph type="secondary">之后可以随时点“恢复默认”或单独恢复某一段。</Paragraph>
      </div>,
      okText: "确认删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => runBulk(changes, "已删除安全分段", true),
    });
  };

  const resetScope = (): void => {
    const changes = resetChanges(rows, scope, category);
    if (changes.length === 0) { message.info("这一层没有覆盖"); return; }
    modal.confirm({
      title: `${scope === "group" ? "本群恢复继承全局" : "全局恢复代码默认"}？`,
      content: `将去掉${category === "all" ? "" : `「${counts[category] ? rows.find((row) => row.category === category)?.categoryLabel : category}」里`} ${changes.length} 处覆盖。修改历史会保留。`,
      okText: "恢复",
      cancelText: "取消",
      onOk: () => runBulk(changes, scope === "group" ? "已恢复继承" : "已恢复默认"),
    });
  };

  const doExport = async (): Promise<void> => {
    try {
      const data = await exportOverrides(scope, groupId);
      downloadJson(`yawnbot-prompts-${scope === "group" ? groupId : "global"}.json`, data);
    } catch (reason) {
      message.error(errorText(reason));
    }
  };

  const doImport = async (): Promise<void> => {
    const parsed = parseImport(importText);
    if (parsed.error) { message.error(parsed.error); return; }
    const touchesSafety = parsed.changes.some((item) => rows.find((row) => row.id === item.sectionId)?.safety && (item.mode === "disable" || item.mode === "replace"));
    await runBulk(parsed.changes, "导入完成", touchesSafety);
    setImportOpen(false);
    setImportText("");
  };

  const resetOne = async (row: PromptSectionRow): Promise<void> => {
    try {
      await clearOverride(row.id, scope, groupId, revision);
      message.success(scope === "group" ? "已恢复继承全局" : "已恢复默认");
      reload();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("别处刚改过提示词，已刷新"); reload(); } else message.error(errorText(reason));
    }
  };

  if (catalog.error && !catalog.data) return <QueryErrorAlert error={catalog.error} onRetry={reload} />;
  if (!catalog.data) return <Skeleton active paragraph={{ rows: 12 }} />;
  const data = catalog.data;
  const categoryOptions = [{ key: "all", label: "全部", hint: "" }, ...data.categories];

  return <div className="pst dbg">
    <section className="dbg-panel pst-top">
      <div className="pst-top-row">
        <div>
          <Title level={4} className="pst-title">提示词工作台</Title>
          <Text type="secondary">Agent 发给模型的每一段文字都在这里：看默认、看实际生成的样例，按全局或本群替换、追加、前置或删除。保存后下一轮立即生效。</Text>
        </div>
        <Space wrap>
          <Segmented value={scope} onChange={(value) => params.update({ "prompts.scope": String(value) })} options={[{ value: "global", label: "全局（所有群）" }, { value: "group", label: `本群（${groupId}）` }]} />
          <Tooltip title="刷新"><Button icon={<ReloadOutlined />} aria-label="刷新提示词" onClick={reload} loading={catalog.refreshing} /></Tooltip>
        </Space>
      </div>
      <div className="pst-kpis">
        <span><strong>{data.summary.total}</strong> 个分段</span>
        <span>全局覆盖 <strong>{data.summary.globalOverrides}</strong></span>
        <span>本群覆盖 <strong>{data.summary.groupOverrides}</strong></span>
        <span>已删除 <strong>{data.summary.disabled}</strong></span>
        <span>Prompt 版本 <Text code>{data.promptVersion}</Text></span>
        <span>覆盖版本 <Text code>#{data.revision}</Text>{data.updatedAt ? ` · ${formatTime(data.updatedAt)}` : ""}</span>
        {status.data && <Tooltip title={`每条 Trace 最多 ${status.data.capture.perTrace} 次请求、单次 ${Math.round(status.data.capture.snapshotChars / 1000)}K 字符；最近 ${status.data.capture.tracesWithPrompts} 条 Trace 保留完整提示词`}>
          <Tag color={status.data.capture.enabled ? "green" : "default"}>完整提示词抓取{status.data.capture.enabled ? "已开启" : "已关闭（AGENT_TRACE_PROMPT_CAPTURE=off）"}</Tag>
        </Tooltip>}
      </div>
      <div className="pst-actions">
        <Button icon={<DownloadOutlined />} onClick={() => void doExport()}>导出{scope === "group" ? "本群" : "全局"}覆盖</Button>
        <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>导入</Button>
        <Button icon={<UndoOutlined />} onClick={resetScope} disabled={busy || scopeCount === 0}>{scope === "group" ? "本群恢复继承" : "恢复默认"}{category !== "all" ? "（当前分类）" : ""}</Button>
        <Button danger icon={<DeleteOutlined />} onClick={removeSafety} disabled={busy}>一键删除安全上下文</Button>
      </div>
      {data.loadError && <Alert type="error" showIcon message="覆盖文件读取失败，当前按代码默认运行" description={`${data.storeFile}：${data.loadError}`} />}
      {safetyGone.length > 0 && <Alert
        type="warning"
        showIcon
        message={`在本群生效的设置里，有 ${safetyGone.length} 个安全分段被删除或替换`}
        description={<span>{safetyGone.map((row) => <Tag key={row.id} color="red" className="pst-link-tag" onClick={() => params.update({ "prompts.section": row.id })}>{row.title}</Tag>)}</span>}
      />}
    </section>

    <div className="pst-body">
      <nav className="dbg-panel pst-cats" aria-label="提示词分类">
        {categoryOptions.map((item) => {
          const count = item.key === "all" ? { total: rows.length, modified: rows.filter(isModified).length, disabled: data.summary.disabled } : counts[item.key] ?? { total: 0, modified: 0, disabled: 0 };
          return <button
            key={item.key}
            type="button"
            className={`pst-cat${category === item.key ? " is-active" : ""}`}
            onClick={() => params.update({ "prompts.cat": item.key })}
            title={item.hint}
            aria-pressed={category === item.key}
          >
            <span>{item.label}</span>
            <span className="pst-cat-count">{count.modified > 0 && <em>{count.modified}</em>}{count.total}</span>
          </button>;
        })}
      </nav>

      <section className="pst-main">
        <div className="dbg-panel pst-filters">
          <Input.Search allowClear defaultValue={query} onSearch={(value) => params.update({ "prompts.q": value })} placeholder="搜索标题、ID 或文字" aria-label="搜索提示词分段" className="pst-search" />
          <Segmented size="small" value={filter} onChange={(value) => params.update({ "prompts.filter": String(value) })} options={SECTION_FILTERS} />
          {category !== "all" && <Text type="secondary" className="dbg-small">{data.categories.find((item) => item.key === category)?.hint}</Text>}
        </div>
        {visible.length === 0
          ? <div className="dbg-panel"><AdminEmpty description="没有匹配的分段" /></div>
          : <div className="pst-list">
            {visible.map((row) => <SectionCard
              key={row.id}
              row={row}
              scope={scope}
              active={row.id === sectionId}
              onEdit={() => params.update({ "prompts.section": row.id })}
              onReset={() => void resetOne(row)}
            />)}
          </div>}
      </section>
    </div>

    <BackgroundPrompts />

    {editing && <SectionEditor
      key={`${editing.id}-${scope}-${revision}`}
      row={editing}
      scope={scope}
      groupId={groupId}
      revision={revision}
      onClose={() => params.update({ "prompts.section": null })}
      onSaved={reload}
    />}

    <Modal
      open={importOpen}
      title="导入提示词覆盖"
      okText="导入"
      cancelText="取消"
      confirmLoading={busy}
      onOk={() => void doImport()}
      onCancel={() => setImportOpen(false)}
    >
      <Paragraph type="secondary">粘贴“导出”得到的 JSON，会合并进{scope === "group" ? "本群" : "全局"}覆盖（同一分段以导入为准）。</Paragraph>
      <Input.TextArea value={importText} onChange={(event) => setImportText(event.target.value)} autoSize={{ minRows: 6, maxRows: 16 }} aria-label="导入 JSON" />
      <input
        type="file"
        accept="application/json,.json"
        aria-label="选择导出文件"
        className="pst-file"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void file.text().then(setImportText);
        }}
      />
      {importText && (() => {
        const parsed = parseImport(importText);
        return parsed.error ? <Text type="danger">{parsed.error}</Text> : <Text type="secondary">将导入 {parsed.changes.length} 处覆盖</Text>;
      })()}
    </Modal>
  </div>;
}
