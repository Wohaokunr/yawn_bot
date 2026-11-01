import {
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  HistoryOutlined,
  ReloadOutlined,
  RiseOutlined,
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
  Popconfirm,
  Segmented,
  Skeleton,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useMemo, useState } from "react";
import { ApiError } from "../api";
import { CopyButton } from "../agent-debug/debug-utils";
import { downloadJson, estimateTokens } from "../agent-debug/trace-analysis";
import { composeText, MODE_META, modeLabel, type PromptHistoryEntry, type PromptMode } from "../agent-prompts/prompt-meta";
import { PromptSnapshotView } from "../agent-prompts/PromptSnapshotView";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import { privateApi, type PrivatePipeline, type PrivatePromptCatalog, type PrivateScope, type PrivateSectionRow, type PrivateSnapshot } from "./private-api";
import {
  baseOf,
  filterPrivateSections,
  groupCounts,
  inheritedOf,
  ISSUE_META,
  issueScopeLabel,
  ownOverride,
  parsePrivateImport,
  PIPELINE_TOOLS_LABELS,
  PIPELINE_WHERE_LABELS,
  PRIVATE_FILTERS,
  type PrivateFilter,
  withPipelineTask,
  worstIssue,
} from "./private-meta";

const { Text, Paragraph } = Typography;
const PREVIEW_CHARS = 320;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export interface PromptsParams {
  userId: string;
  group: string;
  filter: PrivateFilter;
  query: string;
  pipeline: string;
  sectionId: string;
  update: (patch: Record<string, string | null>) => void;
}

function EffectiveTag({ row, scope }: { row: PrivateSectionRow; scope: PrivateScope }): React.JSX.Element {
  const own = ownOverride(row, scope);
  if (row.effectiveMode === "default" && !own) return <Tag>{scope === "user" && row.globalOverride ? "继承全局" : "默认"}</Tag>;
  const meta = MODE_META[row.effectiveMode];
  const from = row.effectiveScope === "user" ? "本人" : row.effectiveScope === "global" ? "全局" : "";
  return <Tooltip title={meta.hint}><Tag color={meta.color}>{from}{meta.label}</Tag></Tooltip>;
}

function PipelineMap({
  pipelines,
  active,
  onPick,
  onOpen,
}: {
  pipelines: PrivatePipeline[];
  active: string;
  onPick: (key: string) => void;
  onOpen: (sectionId: string) => void;
}): React.JSX.Element {
  const current = pipelines.find((item) => item.key === active) ?? pipelines[0];
  return <section className="dbg-panel pa-pipeline" aria-label="私聊链路">
    <div className="pst-section-head">
      <Text strong>链路</Text>
      <Text type="secondary" className="dbg-small">每条链路按真实拼装顺序列出分段；点分段直接编辑。灰色是有条件才出现的。</Text>
    </div>
    <Segmented
      value={current?.key}
      onChange={(value) => onPick(String(value))}
      options={pipelines.map((item) => ({ value: item.key, label: `${item.label} · ${item.tokens.always}` }))}
      className="pa-pipeline-switch"
    />
    {current && <>
      <Paragraph type="secondary" className="pa-pipeline-desc">
        {current.description} 触发：{current.trigger}。工具：{PIPELINE_TOOLS_LABELS[current.tools] ?? current.tools}
        {current.toolSections.length > 0 ? `（${current.toolSections.length} 个描述，约 ${current.tokens.tools} Token）` : ""}。
        固定部分约 <Text strong>{current.tokens.always}</Text> Token，条件部分最多再加 {current.tokens.conditional}。
      </Paragraph>
      <ol className="pa-steps">
        {current.steps.map((step, index) => <li key={`${step.sectionId}-${index}`} className={`pa-step${step.condition ? " is-conditional" : ""}${step.mode === "disable" ? " is-disabled" : ""}`}>
          <button type="button" onClick={() => onOpen(step.sectionId)} className="pa-step-btn">
            <span className="pa-step-where">{PIPELINE_WHERE_LABELS[step.where] ?? step.where}</span>
            <span className="pa-step-title">{step.safety && <SafetyCertificateOutlined className="pst-safety-icon" />}{step.title}</span>
            {step.condition && <span className="pa-step-cond">{step.condition}</span>}
            <span className="pa-step-meta">
              {step.mode !== "default" && <Tag color={MODE_META[step.mode as PromptMode]?.color}>{step.scope === "user" ? "本人" : "全局"}{modeLabel(step.mode)}</Tag>}
              <Text type="secondary" className="dbg-small">{step.tokens} Token</Text>
            </span>
          </button>
        </li>)}
      </ol>
    </>}
  </section>;
}

function SectionCard({ row, scope, onEdit, onReset }: { row: PrivateSectionRow; scope: PrivateScope; onEdit: () => void; onReset: () => void }): React.JSX.Element {
  const own = ownOverride(row, scope);
  const text = row.effectiveText ?? "";
  const disabled = row.effectiveMode === "disable";
  return <article className={`pst-card${disabled ? " is-disabled" : ""}${row.safety ? " is-safety" : ""}`} data-section-id={row.id}>
    <header className="pst-card-head">
      <div className="pst-card-title">
        <Text strong>{row.title}</Text>
        <Text type="secondary" code copyable={{ text: row.id, tooltips: ["复制分段 ID", "已复制"] }}>{row.id}</Text>
      </div>
      <Space size={4} wrap>
        <EffectiveTag row={row} scope={scope} />
        {row.userOverrideCount > 0 && <Tooltip title="有多少位用户单独改过这一段"><Tag color="cyan">{row.userOverrideCount} 人改过</Tag></Tooltip>}
        {row.editable && <Button size="small" icon={<EditOutlined />} onClick={onEdit}>编辑</Button>}
        {own && <Tooltip title={scope === "user" ? "去掉本人覆盖，回到继承全局" : "去掉全局覆盖，回到代码默认"}><Button size="small" icon={<UndoOutlined />} onClick={onReset} aria-label={`恢复 ${row.title}`} /></Tooltip>}
      </Space>
    </header>
    <div className="pst-card-meta">
      {row.safety && <Tag color="red" icon={<SafetyCertificateOutlined />}>安全</Tag>}
      {row.protocol && <Tag color="orange">输出协议</Tag>}
      {row.pipelines.slice(0, 4).map((item) => <Tag key={`${item.pipeline}-${item.where}`} className="dbg-tag-soft">{item.label}</Tag>)}
      <Text type="secondary" className="dbg-small">{row.layerLabel} · {row.when}</Text>
    </div>
    {row.description && <Paragraph type="secondary" className="pst-card-desc">{row.description}</Paragraph>}
    {disabled
      ? <div className="pst-card-text is-empty">已删除：私聊里模型看不到这一段{row.safety ? "。代码里的权限、配额与出站校验仍然有效。" : "。"}</div>
      : text
        ? <pre className="pst-card-text">{text.length > PREVIEW_CHARS ? `${text.slice(0, PREVIEW_CHARS)}…` : text}</pre>
        : <div className="pst-card-text is-empty">{row.kind === "generated" ? "动态生成：私聊跑一轮后就能看到样例。" : "（空）"}</div>}
    <footer className="pst-card-foot">
      <Text type="secondary" className="dbg-small">生效约 {row.effectiveTokens} Token{row.defaultText != null ? ` · 默认 ${row.defaultTokens}` : ""}</Text>
      {own?.note && <Text type="secondary" className="dbg-small">备注：{own.note}</Text>}
    </footer>
  </article>;
}

interface EditorState {
  mode: PromptMode | "inherit";
  text: string;
  note: string;
  confirmSafety: boolean;
}

function validate(row: PrivateSectionRow, mode: PromptMode, text: string, confirmSafety: boolean): string | null {
  if (!row.editable) return "这一段是本轮数据，不能修改";
  if (!row.modes.includes(mode)) return `这一段不支持“${modeLabel(mode)}”`;
  if ((mode === "replace" || mode === "append" || mode === "prepend") && !text.trim()) return "替换/追加/前置需要填写文字；想去掉这段请选“删除”";
  if (text.length > 12_000) return "文字最长 12000 字";
  if (row.safety && (mode === "disable" || mode === "replace") && !confirmSafety) return "这是安全相关分段，请先勾选确认";
  if (row.id === "private.attachment_excerpt" && mode === "replace" && !(text.includes("{title}") && text.includes("{text}"))) return "替换文字需要保留 {title} 和 {text} 两个占位符";
  return null;
}

function SectionEditor({
  row,
  scope,
  userId,
  revision,
  onClose,
  onSaved,
}: {
  row: PrivateSectionRow;
  scope: PrivateScope;
  userId: string;
  revision: number;
  onClose: () => void;
  onSaved: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const own = ownOverride(row, scope);
  const inherited = inheritedOf(row, scope);
  const [state, setState] = useState<EditorState>(() => own
    ? { mode: own.mode, text: own.text || (own.mode === "replace" ? row.effectiveText ?? "" : ""), note: own.note, confirmSafety: false }
    : { mode: "inherit", text: "", note: "", confirmSafety: false });
  const [saving, setSaving] = useState(false);
  const [history, setHistory] = useState<PromptHistoryEntry[] | null>(null);
  const [previewUser, setPreviewUser] = useState(userId);
  const [previewMessage, setPreviewMessage] = useState("");
  const [preview, setPreview] = useState<PrivateSnapshot | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const textMode = state.mode === "replace" || state.mode === "append" || state.mode === "prepend";
  const result = state.mode === "inherit" ? inherited : state.mode === "builtin" ? baseOf(row) : composeText(inherited, state.mode, state.text);
  const error = state.mode === "inherit" ? null : validate(row, state.mode, state.text, state.confirmSafety);
  const options = [
    { value: "inherit", label: scope === "user" ? "继承全局" : "默认" },
    ...row.modes.filter((mode) => scope === "user" || mode !== "builtin").map((mode) => ({ value: mode, label: MODE_META[mode].label })),
  ];

  const save = async (): Promise<void> => {
    if (error) { message.error(error); return; }
    setSaving(true);
    try {
      if (state.mode === "inherit") {
        await privateApi.clearOverride(row.id, scope, userId, revision);
        message.success(scope === "user" ? "已恢复继承全局" : "已恢复默认");
      } else {
        await privateApi.saveOverride(row.id, { scope, userId, mode: state.mode, text: state.text, note: state.note, confirmSafety: state.confirmSafety, expectedRevision: revision });
        message.success(`已保存：${row.title} → ${modeLabel(state.mode)}，${scope === "user" ? "这位用户" : "所有私聊"}下一轮立即生效`);
      }
      onSaved();
      onClose();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("别处刚改过提示词，已刷新，请确认后再保存"); onSaved(); }
      else message.error(errorText(reason));
    } finally {
      setSaving(false);
    }
  };

  const runPreview = async (): Promise<void> => {
    if (!previewUser) { message.warning("先填一位用户的 QQ 号：预览要用他的真实资料和最近聊天"); return; }
    setPreviewing(true);
    try {
      const draft = { [row.id]: state.mode === "inherit" ? { mode: "inherit", text: "" } : { mode: state.mode, text: state.text } };
      setPreview(await privateApi.preview({
        userId: previewUser,
        message: previewMessage,
        ...(scope === "user" ? { userDrafts: draft } : { globalDrafts: draft }),
      }));
    } catch (reason) {
      message.error(errorText(reason));
    } finally {
      setPreviewing(false);
    }
  };

  return <Drawer
    open
    width="min(760px, 100vw)"
    onClose={onClose}
    title={<span className="pst-drawer-title">{row.safety && <SafetyCertificateOutlined className="pst-safety-icon" />}{row.title}<Text type="secondary" code>{row.id}</Text></span>}
    extra={<Button type="primary" loading={saving} disabled={Boolean(error)} onClick={() => void save()}>保存</Button>}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <div className="pst-editor-meta">
        <Text type="secondary">{row.layerLabel} · {row.when}</Text>
        {row.description && <Text type="secondary">{row.description}</Text>}
        <Text type="secondary">正在编辑：<Text strong>{scope === "user" ? `用户 ${userId} 的私聊` : "全局（所有私聊）"}</Text>{scope === "user" ? "；本人设置优先于全局，只影响和他的私聊。" : "；单独给某位用户改过的以他自己的为准。"}</Text>
        <span>{row.pipelines.map((item) => <Tag key={`${item.pipeline}-${item.where}`}>{item.label}{item.condition ? `：${item.condition}` : ""}</Tag>)}</span>
      </div>
      {row.safety && <Alert type="warning" showIcon icon={<WarningOutlined />} message="这是安全相关分段" description="删除或替换后，私聊里的模型不再看到这条约束。工具只作用于本人、超级管理员门槛、配额与出站校验仍由代码保证。" />}
      {row.protocol && <Alert type="info" showIcon message="这一段规定了模型的输出格式" description="改动后先用下方“预览完整请求”确认格式没坏。" />}
      <div>
        <Text strong>怎么改</Text>
        <Segmented block value={state.mode} onChange={(value) => setState((current) => ({ ...current, mode: value as PromptMode | "inherit", text: value === "replace" && !current.text ? inherited : current.text }))} options={options} />
        <Text type="secondary" className="dbg-small">{state.mode === "inherit" ? (scope === "user" ? "这位用户不单独设置，跟随全局" : MODE_META.default.hint) : state.mode === "builtin" ? "这位用户强制用代码默认，不继承全局覆盖" : MODE_META[state.mode].hint}</Text>
      </div>
      {textMode && <div>
        <Text strong>{state.mode === "replace" ? "替换成" : state.mode === "append" ? "追加的文字" : "前置的文字"}</Text>
        <Input.TextArea value={state.text} onChange={(event) => setState((current) => ({ ...current, text: event.target.value }))} autoSize={{ minRows: 4, maxRows: 18 }} maxLength={12_000} aria-label="分段文字" className="pst-textarea" />
        <Text type="secondary" className="dbg-small">{state.text.length.toLocaleString()} / 12000 字 · 约 {estimateTokens(state.text)} Token</Text>
      </div>}
      {state.mode !== "inherit" && <Input value={state.note} onChange={(event) => setState((current) => ({ ...current, note: event.target.value }))} placeholder="备注（可选）：为什么这么改" maxLength={200} aria-label="备注" />}
      {row.safety && (state.mode === "disable" || state.mode === "replace") && <Checkbox checked={state.confirmSafety} onChange={(event) => setState((current) => ({ ...current, confirmSafety: event.target.checked }))}>我了解删除/替换这条安全约束的影响</Checkbox>}
      {error && state.mode !== "inherit" && <Text type="danger">{error}</Text>}
      <div>
        <div className="pst-section-head"><Text strong>保存后模型看到的内容</Text><Text type="secondary">约 {estimateTokens(result)} Token</Text>{result && <CopyButton text={result} label="复制生效文字" />}</div>
        {result ? <pre className="pst-card-text">{result}</pre> : <div className="pst-card-text is-empty">（这一段不会出现在请求里）</div>}
      </div>
      <div>
        <div className="pst-section-head"><Text strong>{row.defaultText != null ? "代码默认" : "最近一次生成的样例"}</Text></div>
        {baseOf(row) ? <pre className="pst-card-text is-muted">{baseOf(row)}</pre> : <div className="pst-card-text is-empty">还没有样例。</div>}
      </div>
      <div>
        <div className="pst-section-head">
          <Text strong>修改历史</Text>
          <Button size="small" icon={<HistoryOutlined />} onClick={() => void privateApi.history(row.id, scope, userId).then(setHistory, (reason: unknown) => message.error(errorText(reason)))}>{history ? "刷新" : "查看"}</Button>
        </div>
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
      <div className="pst-preview">
        <div className="pst-section-head"><Text strong>预览完整请求</Text><Text type="secondary" className="dbg-small">用这位用户的真实资料与最近聊天，按草稿拼出主回合请求，不调用模型</Text></div>
        <Space wrap className="pa-preview-form">
          <Input value={previewUser} onChange={(event) => setPreviewUser(event.target.value.trim())} placeholder="用户 QQ" aria-label="预览用户" style={{ width: 140 }} />
          <Input value={previewMessage} onChange={(event) => setPreviewMessage(event.target.value)} placeholder="假设他发来（可选）" aria-label="预览消息" style={{ width: 260 }} maxLength={2000} />
          <Button icon={<EyeOutlined />} loading={previewing} onClick={() => void runPreview()}>预览</Button>
        </Space>
        {preview && <PromptSnapshotView snapshots={[withPipelineTask(preview)]} filenamePrefix={`private-preview-${row.id}`} onOpenSection={() => undefined} />}
      </div>
    </Space>
  </Drawer>;
}

function OverrideUsers({ revision, onPick, onChanged }: { revision: number; onPick: (userId: string) => void; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery({
    queryKey: ["private-prompt-users", revision],
    fetcher: (signal) => privateApi.overrideUsers(signal),
    invalidation: { resources: ["private_agent_prompts"] },
  });
  const rows = query.data ?? [];
  const act = async (work: () => Promise<unknown>, done: string): Promise<void> => {
    try { await work(); message.success(done); onChanged(); query.reload(); }
    catch (reason) { message.error(errorText(reason)); }
  };
  return <section className="dbg-panel pa-users">
    <div className="pst-section-head"><Text strong>单独改过提示词的用户</Text><Text type="secondary" className="dbg-small">{rows.length} 位</Text></div>
    {rows.length === 0
      ? <Text type="secondary">还没有。在上面填一位用户的 QQ，就能只给他改私聊提示词。</Text>
      : <ul className="pa-user-list">
        {rows.map((row) => <li key={row.userId}>
          <button type="button" className="pa-user-btn" onClick={() => onPick(row.userId)}>{row.userId}</button>
          <Text type="secondary" className="dbg-small">{row.count} 处{row.safety ? ` · ${row.safety} 处安全段` : ""}{row.updatedAt ? ` · ${formatTime(row.updatedAt)}` : ""}</Text>
          <span className="dbg-grow" />
          <Popconfirm title={`把 ${row.userId} 的 ${row.count} 处覆盖提升为全局？`} description="所有私聊都会用这些改动，并从他的本人层删掉。" onConfirm={() => act(() => privateApi.promote(row.userId, { expectedRevision: revision, confirmSafety: row.safety > 0 }), "已提升为全局")}>
            <Button size="small" icon={<RiseOutlined />}>提升为全局</Button>
          </Popconfirm>
          <Popconfirm title={`清空 ${row.userId} 的全部本人覆盖？`} onConfirm={() => act(() => privateApi.resetUser(row.userId, revision), "已清空")}>
            <Button size="small" danger icon={<UndoOutlined />}>清空</Button>
          </Popconfirm>
        </li>)}
      </ul>}
  </section>;
}

export function PromptsTab({ params }: { params: PromptsParams }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const { userId, group, filter, query, pipeline, sectionId } = params;
  const scope: PrivateScope = userId ? "user" : "global";
  const [userInput, setUserInput] = useState(userId);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [busy, setBusy] = useState(false);
  const [pipelineFocus, setPipelineFocus] = useState(false);
  const catalog = useApiQuery<PrivatePromptCatalog>({
    queryKey: ["private-prompt-catalog", userId],
    fetcher: (signal) => privateApi.catalog(userId, signal),
    invalidation: { resources: ["private_agent_prompts"] },
  });
  const rows = catalog.data?.sections ?? [];
  const counts = useMemo(() => groupCounts(rows), [rows]);
  const visible = useMemo(() => filterPrivateSections(rows, { group, filter, query, pipeline: pipelineFocus ? pipeline || "turn" : "" }), [rows, group, filter, query, pipeline, pipelineFocus]);
  const editing = rows.find((row) => row.id === sectionId) ?? null;
  const revision = catalog.data?.revision ?? 0;

  const runBulk = async (changes: Array<{ sectionId: string; mode: string; text?: string; note?: string }>, done: string, confirmSafety = false, replaceScope = false): Promise<void> => {
    setBusy(true);
    try {
      const result = await privateApi.bulk({ scope, userId, changes, confirmSafety, expectedRevision: revision, replaceScope });
      message.success(`${done}（${result.changed} 处）`);
      catalog.reload();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("别处刚改过提示词，已刷新，请重试"); catalog.reload(); } else message.error(errorText(reason));
    } finally {
      setBusy(false);
    }
  };

  const resetLayer = (): void => {
    const changes = rows.filter((row) => ownOverride(row, scope) && (group === "all" || row.group === group)).map((row) => ({ sectionId: row.id, mode: "inherit" }));
    if (changes.length === 0) { message.info("这一层没有覆盖"); return; }
    modal.confirm({
      title: scope === "user" ? `用户 ${userId} 恢复继承全局？` : "私聊全局恢复代码默认？",
      content: `将去掉 ${changes.length} 处覆盖，修改历史会保留。`,
      okText: "恢复",
      cancelText: "取消",
      onOk: () => runBulk(changes, scope === "user" ? "已恢复继承" : "已恢复默认"),
    });
  };

  const doExport = async (): Promise<void> => {
    try { downloadJson(`yawnbot-private-prompts-${scope === "user" ? userId : "global"}.json`, await privateApi.exportOverrides(scope, userId)); }
    catch (reason) { message.error(errorText(reason)); }
  };

  const doImport = async (): Promise<void> => {
    const parsed = parsePrivateImport(importText);
    if (parsed.error) { message.error(parsed.error); return; }
    const touchesSafety = parsed.changes.some((item) => rows.find((row) => row.id === item.sectionId)?.safety && (item.mode === "disable" || item.mode === "replace"));
    await runBulk(parsed.changes, "导入完成", touchesSafety);
    setImportOpen(false);
    setImportText("");
  };

  const resetOne = async (row: PrivateSectionRow): Promise<void> => {
    try {
      await privateApi.clearOverride(row.id, scope, userId, revision);
      message.success(scope === "user" ? "已恢复继承全局" : "已恢复默认");
      catalog.reload();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) { message.warning("别处刚改过提示词，已刷新"); catalog.reload(); } else message.error(errorText(reason));
    }
  };

  const pickUser = (value: string): void => {
    const clean = value.trim();
    if (clean && !/^\d{5,12}$/.test(clean)) { message.warning("请输入 5–12 位 QQ 号"); return; }
    setUserInput(clean);
    params.update({ "pa.user": clean || null, "pa.section": null });
  };

  if (catalog.error && !catalog.data) return <QueryErrorAlert error={catalog.error} onRetry={catalog.reload} />;
  if (!catalog.data) return <Skeleton active paragraph={{ rows: 10 }} />;
  const data = catalog.data;
  const worst = worstIssue(data.issues);
  const groupOptions = [{ key: "all", label: "全部", hint: "" }, ...data.groups];

  return <div className="pst dbg pa-prompts">
    <section className="dbg-panel pst-top">
      <div className="pst-top-row">
        <div>
          <Text strong className="pa-subtitle">私聊提示词注册表</Text>
          <Text type="secondary" className="pa-block">私聊 Agent 发给模型的每一段：主回合、汇报、协调、后台小助手和记忆整理。可以对所有私聊改，也可以只给某一位用户改。</Text>
        </div>
        <Space wrap>
          <Segmented value={scope} onChange={(value) => (value === "global" ? pickUser("") : setUserInput(userInput || ""))} options={[{ value: "global", label: "全局（所有私聊）" }, { value: "user", label: "某位用户", disabled: !userId && !userInput }]} />
          <Input.Search value={userInput} onChange={(event) => setUserInput(event.target.value)} onSearch={pickUser} placeholder="用户 QQ" enterButton="切换" allowClear aria-label="按用户查看" className="pa-user-search" />
          <Tooltip title="刷新"><Button icon={<ReloadOutlined />} aria-label="刷新私聊提示词" onClick={catalog.reload} loading={catalog.refreshing} /></Tooltip>
        </Space>
      </div>
      <div className="pst-kpis">
        <span><strong>{data.summary.total}</strong> 个私聊分段</span>
        <span>全局覆盖 <strong>{data.summary.globalOverrides}</strong></span>
        {scope === "user" && <span>本人覆盖 <strong>{data.summary.userOverrides}</strong></span>}
        <span>单独改过的用户 <strong>{data.summary.usersWithOverrides}</strong></span>
        <span>已删除 <strong>{data.summary.disabled}</strong></span>
        <span>覆盖版本 <Text code>#{data.revision}</Text>{data.updatedAt ? ` · ${formatTime(data.updatedAt)}` : ""}</span>
      </div>
      <div className="pst-actions">
        <Button icon={<DownloadOutlined />} onClick={() => void doExport()}>导出{scope === "user" ? "本人" : "全局"}覆盖</Button>
        <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>导入</Button>
        <Button icon={<UndoOutlined />} onClick={resetLayer} disabled={busy}>{scope === "user" ? "本人恢复继承" : "恢复默认"}{group !== "all" ? "（当前分组）" : ""}</Button>
      </div>
      {data.loadError && <Alert type="error" showIcon message="覆盖文件读取失败，当前按代码默认运行" description={data.loadError} />}
      {worst && <Alert
        type={ISSUE_META[worst].alert}
        showIcon
        message={`体检发现 ${data.issues.length} 个问题`}
        description={<ul className="pa-issues">
          {data.issues.slice(0, 8).map((issue, index) => <li key={`${issue.code}-${issue.sectionId}-${index}`}>
            <Tag color={ISSUE_META[issue.level].color}>{ISSUE_META[issue.level].label}</Tag>
            <button type="button" className="pa-link" onClick={() => params.update({ "pa.section": issue.sectionId })}>{rows.find((row) => row.id === issue.sectionId)?.title ?? issue.sectionId}</button>
            <Text type="secondary"> · {issueScopeLabel(issue.scope)} · {issue.message}</Text>
          </li>)}
          {data.issues.length > 8 && <li><Text type="secondary">还有 {data.issues.length - 8} 个…</Text></li>}
        </ul>}
      />}
    </section>

    <PipelineMap pipelines={data.pipelines} active={pipeline || "turn"} onPick={(key) => params.update({ "pa.pipe": key === "turn" ? null : key })} onOpen={(id) => params.update({ "pa.section": id })} />

    <div className="pst-body">
      <nav className="dbg-panel pst-cats" aria-label="私聊分段分组">
        {groupOptions.map((item) => {
          const count = item.key === "all" ? { total: rows.length, modified: rows.filter((row) => ownOverride(row, scope)).length } : counts[item.key] ?? { total: 0, modified: 0 };
          return <button key={item.key} type="button" className={`pst-cat${group === item.key ? " is-active" : ""}`} onClick={() => params.update({ "pa.group": item.key === "all" ? null : item.key })} title={item.hint} aria-pressed={group === item.key}>
            <span>{item.label}</span>
            <span className="pst-cat-count">{count.modified > 0 && <em>{count.modified}</em>}{count.total}</span>
          </button>;
        })}
      </nav>
      <section className="pst-main">
        <div className="dbg-panel pst-filters">
          <Input.Search allowClear defaultValue={query} onSearch={(value) => params.update({ "pa.q": value || null })} placeholder="搜索标题、ID 或文字" aria-label="搜索私聊分段" className="pst-search" />
          <Segmented size="small" value={filter} onChange={(value) => params.update({ "pa.filter": value === "all" ? null : String(value) })} options={PRIVATE_FILTERS} />
          <Space size={4}><Switch size="small" checked={pipelineFocus} onChange={setPipelineFocus} aria-label="只看当前链路" /><Text type="secondary" className="dbg-small">只看当前链路</Text></Space>
        </div>
        {visible.length === 0
          ? <div className="dbg-panel"><AdminEmpty description="没有匹配的分段" /></div>
          : <div className="pst-list">
            {visible.map((row) => <SectionCard key={row.id} row={row} scope={scope} onEdit={() => params.update({ "pa.section": row.id })} onReset={() => void resetOne(row)} />)}
          </div>}
      </section>
    </div>

    <OverrideUsers revision={revision} onPick={pickUser} onChanged={catalog.reload} />

    {editing && <SectionEditor
      key={`${editing.id}-${scope}-${userId}-${revision}`}
      row={editing}
      scope={scope}
      userId={userId}
      revision={revision}
      onClose={() => params.update({ "pa.section": null })}
      onSaved={catalog.reload}
    />}

    <Modal open={importOpen} title="导入私聊提示词覆盖" okText="导入" cancelText="取消" confirmLoading={busy} onOk={() => void doImport()} onCancel={() => setImportOpen(false)}>
      <Paragraph type="secondary">粘贴导出的 JSON，会合并进{scope === "user" ? `用户 ${userId} 的本人` : "全局"}覆盖。只导入私聊分段，群聊分段会被跳过。</Paragraph>
      <Input.TextArea value={importText} onChange={(event) => setImportText(event.target.value)} autoSize={{ minRows: 6, maxRows: 16 }} aria-label="导入 JSON" />
      {importText && (() => {
        const parsed = parsePrivateImport(importText);
        return parsed.error ? <Text type="danger">{parsed.error}</Text> : <Text type="secondary">将导入 {parsed.changes.length} 处{parsed.skipped ? `，跳过 ${parsed.skipped} 处非私聊分段` : ""}</Text>;
      })()}
    </Modal>
  </div>;
}
