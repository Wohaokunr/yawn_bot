import {
  CheckOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  EyeOutlined,
  PictureOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  SmileOutlined,
  StopOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Segmented,
  Select,
  Skeleton,
  Statistic,
  Switch,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, SaveStatus, useApiQuery, useUnsavedChanges } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { parseGroupIds } from "../web-search/web-search-meta";
import { stickerImageUrl, stickersApi, uploadSticker } from "./stickers-api";
import {
  cleanName,
  COLLECT_LABELS,
  describeStickerAccess,
  FILTER_LABELS,
  filterStickers,
  formatBytes,
  FREQUENCY_HINTS,
  nameFromFile,
  parseTags,
  SOURCE_LABELS,
  STICKER_SETTING_RANGES,
  stickerSettingsEqual,
  stickerState,
  validateStickerSettings,
} from "./stickers-meta";
import type { StickerFilter, StickerNumericKey } from "./stickers-meta";
import type { Sticker, StickerBulkAction, StickerOverview, StickerPromptPreview, StickerSettings } from "./types";

type StickerTab = "library" | "policy" | "preview";
const TABS: StickerTab[] = ["library", "policy", "preview"];
const STATE_COLORS: Record<Exclude<StickerFilter, "all">, string> = { active: "green", pending: "gold", disabled: "default" };

function parseTab(value: string | null): StickerTab {
  return TABS.includes(value as StickerTab) ? (value as StickerTab) : "library";
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function StickerStatusBar({ overview, onReload, reloading }: { overview: StickerOverview; onReload: () => void; reloading: boolean }): React.JSX.Element {
  const { settings, counts, usage } = overview;
  const tone = !settings.enabled ? "muted" : counts.active === 0 ? "warn" : "ok";
  const label = !settings.enabled ? "已关闭" : counts.active === 0 ? "库里还没有可用的表情包" : `${overview.frequencyLabels[settings.frequency]}发`;
  return (
    <section className={`sbx-status is-${tone}`} aria-label="表情包状态">
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>表情包 · {label}</strong>
          <span>{describeStickerAccess(settings)}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        <Tag color="green">可用 {counts.active}</Tag>
        {counts.pending > 0 && <Tag color="gold">待审核 {counts.pending}</Tag>}
        <Tooltip title="今日真正发出去的表情包（全部群）">
          <Tag>今日发出 {usage.totals.sent ?? 0}</Tag>
        </Tooltip>
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={reloading} onClick={onReload}>刷新</Button>
      </div>
    </section>
  );
}

function StickerCard({ sticker, selected, onSelect, onOpen }: { sticker: Sticker; selected: boolean; onSelect: (checked: boolean) => void; onOpen: () => void }): React.JSX.Element {
  const state = stickerState(sticker);
  return (
    <div className={`stk-card is-${state}${selected ? " is-selected" : ""}`}>
      <Checkbox className="stk-check" checked={selected} onChange={(event) => onSelect(event.target.checked)} aria-label={`选择 ${sticker.name}`} />
      <button type="button" className="stk-thumb" onClick={onOpen} aria-label={`编辑 ${sticker.name}`}>
        {sticker.fileMissing ? <span className="stk-missing">文件丢失</span> : <img src={stickerImageUrl(sticker)} alt={sticker.name} loading="lazy" />}
      </button>
      <div className="stk-meta">
        <strong title={sticker.name}>{sticker.name}</strong>
        <div className="stk-tags">
          {state !== "active" && <Tag color={STATE_COLORS[state]}>{FILTER_LABELS[state]}</Tag>}
          {sticker.tags.slice(0, 3).map((tag) => <Tag key={tag}>{tag}</Tag>)}
        </div>
        <span className="sbx-muted">{sticker.uses > 0 ? `发过 ${sticker.uses} 次` : "还没发过"}{sticker.groups.length > 0 ? ` · 限 ${sticker.groups.length} 个群` : ""}</span>
      </div>
    </div>
  );
}

function EditModal({ sticker, groupNames, onClose, onChanged }: { sticker: Sticker | null; groupNames: Record<string, string>; onClose: () => void; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [name, setName] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const [groups, setGroups] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!sticker) return;
    setName(sticker.name);
    setTags(sticker.tags);
    setDescription(sticker.description);
    setGroups(sticker.groups.join("\n"));
    setEnabled(sticker.enabled);
  }, [sticker]);
  if (!sticker) return <Modal open={false} onCancel={onClose} footer={null} />;
  const parsedGroups = parseGroupIds(groups);
  const run = async (action: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await action();
      message.success(done);
      onChanged();
      onClose();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    } finally {
      setBusy(false);
    }
  };
  const save = (approve: boolean) =>
    run(
      () =>
        stickersApi.update(sticker.id, {
          name: cleanName(name),
          tags,
          description,
          enabled,
          groups: parsedGroups.ids,
          ...(approve ? { status: "active" as const } : {}),
        }),
      approve ? "已审核通过" : "已保存",
    );
  const addedGroup = sticker.addedGroup ? groupNames[sticker.addedGroup] ?? sticker.addedGroup : null;
  return (
    <Modal
      open
      title={`编辑表情包 · ${sticker.name}`}
      onCancel={onClose}
      width={560}
      footer={[
        <Popconfirm key="delete" title="删除这张表情包？" description="图片文件也会一起删掉，不能恢复。" onConfirm={() => void run(() => stickersApi.remove(sticker.id), "已删除")}>
          <Button danger icon={<DeleteOutlined />} disabled={busy}>删除</Button>
        </Popconfirm>,
        sticker.status === "pending" ? (
          <Button key="approve" icon={<CheckOutlined />} loading={busy} disabled={!cleanName(name)} onClick={() => void save(true)}>审核通过</Button>
        ) : null,
        <Button key="save" type="primary" icon={<SaveOutlined />} loading={busy} disabled={!cleanName(name)} onClick={() => void save(false)}>保存</Button>,
      ]}
    >
      <div className="stk-edit">
        <div className="stk-edit-preview">
          {sticker.fileMissing ? <span className="stk-missing">文件丢失</span> : <img src={stickerImageUrl(sticker)} alt={sticker.name} />}
          <span className="sbx-muted">{formatBytes(sticker.size)} · {SOURCE_LABELS[sticker.source]}{addedGroup ? ` · 来自 ${addedGroup}` : ""}{sticker.addedBy ? `（${sticker.addedBy}）` : ""}</span>
        </div>
        <label className="sbx-field">
          <span className="sbx-field-label">名字（模型写 [表情:名字] 发它）</span>
          <Input value={name} maxLength={16} showCount onChange={(event) => setName(event.target.value)} status={cleanName(name) ? undefined : "error"} />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">情绪 / 场景标签（模型也可以写 [表情:标签]，同标签的随机挑一张）</span>
          <Select mode="tags" value={tags} tokenSeparators={[",", "，", " ", "、"]} onChange={(values: string[]) => setTags(parseTags(values.join(",")))} placeholder="如 无语、尴尬、开心" />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">什么时候适合发（会出现在菜单里，越短越好）</span>
          <Input value={description} maxLength={160} onChange={(event) => setDescription(event.target.value)} placeholder="如：被戳穿又不想承认时" />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">只在这些群可用（留空 = 所有群）</span>
          <Input.TextArea value={groups} autoSize={{ minRows: 1, maxRows: 4 }} onChange={(event) => setGroups(event.target.value)} placeholder="每行一个群号" status={parsedGroups.invalid.length ? "error" : undefined} />
        </label>
        <div className="sbx-switch-row">
          <Switch checked={enabled} onChange={setEnabled} aria-label="启用" />
          <div>
            <strong>启用</strong>
            <span className="sbx-muted">停用后保留在库里，但不会出现在菜单、也发不出去。</span>
          </div>
        </div>
      </div>
    </Modal>
  );
}

interface UploadItem {
  key: string;
  file: File;
  name: string;
  tags: string;
  status: "waiting" | "uploading" | "done" | "duplicate" | "failed";
  error?: string;
  preview: string;
}

function UploadModal({ open, onClose, onUploaded, maxBytes }: { open: boolean; onClose: () => void; onUploaded: () => void; maxBytes: number }): React.JSX.Element {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) {
      setItems((current) => {
        for (const item of current) URL.revokeObjectURL(item.preview);
        return [];
      });
    }
  }, [open]);
  const addFiles = (files: FileList | File[]) => {
    const next = [...files]
      .filter((file) => file.type.startsWith("image/") || /\.(png|jpe?g|gif|webp)$/i.test(file.name))
      .map((file, index) => ({
        key: `${Date.now()}-${index}-${file.name}`,
        file,
        name: nameFromFile(file.name),
        tags: "",
        status: file.size > maxBytes ? ("failed" as const) : ("waiting" as const),
        error: file.size > maxBytes ? `超过 ${formatBytes(maxBytes)}` : undefined,
        preview: URL.createObjectURL(file),
      }));
    setItems((current) => [...current, ...next]);
  };
  const patch = (key: string, change: Partial<UploadItem>) => setItems((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));
  const pending = items.filter((item) => item.status === "waiting");
  const ready = pending.length > 0 && pending.every((item) => cleanName(item.name));
  const uploadAll = async () => {
    setBusy(true);
    let uploaded = 0;
    for (const item of pending) {
      patch(item.key, { status: "uploading" });
      try {
        const result = await uploadSticker(item.file, { name: cleanName(item.name), tags: parseTags(item.tags) });
        patch(item.key, { status: result.created ? "done" : "duplicate" });
        if (result.created) uploaded += 1;
      } catch (error) {
        patch(item.key, { status: "failed", error: errorText(error, "上传失败") });
      }
    }
    setBusy(false);
    if (uploaded > 0) onUploaded();
  };
  return (
    <Modal
      open={open}
      title="上传表情包"
      onCancel={onClose}
      width={640}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        <Button key="upload" type="primary" icon={<CloudUploadOutlined />} loading={busy} disabled={!ready} onClick={() => void uploadAll()}>上传 {pending.length || ""} 张</Button>,
      ]}
    >
      <div
        className="stk-drop"
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
        }}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          addFiles(event.dataTransfer.files);
        }}
      >
        <PictureOutlined />
        <span>点击或把图片拖到这里（PNG / JPG / GIF / WebP，单张不超过 {formatBytes(maxBytes)}）</span>
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          hidden
          aria-label="选择表情包图片"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files);
            event.target.value = "";
          }}
        />
      </div>
      {items.length > 0 && (
        <ul className="stk-upload-list" aria-label="待上传">
          {items.map((item) => (
            <li key={item.key} className={`is-${item.status}`}>
              <img src={item.preview} alt="" />
              <div className="stk-upload-fields">
                <Input size="small" value={item.name} maxLength={16} placeholder="名字（必填）" disabled={item.status !== "waiting"} status={item.status === "waiting" && !cleanName(item.name) ? "error" : undefined} onChange={(event) => patch(item.key, { name: event.target.value })} aria-label="名字" />
                <Input size="small" value={item.tags} placeholder="标签，逗号分隔" disabled={item.status !== "waiting"} onChange={(event) => patch(item.key, { tags: event.target.value })} aria-label="标签" />
              </div>
              <span className="stk-upload-state">
                {item.status === "done" && <Tag color="green">已上传</Tag>}
                {item.status === "duplicate" && <Tag>库里已有</Tag>}
                {item.status === "uploading" && <Tag color="blue">上传中</Tag>}
                {item.status === "failed" && <Tooltip title={item.error}><Tag color="red">失败</Tag></Tooltip>}
                {item.status === "waiting" && <Button size="small" type="text" onClick={() => setItems((current) => current.filter((row) => row.key !== item.key))}>移除</Button>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function LibraryTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const [filter, setFilter] = useState<StickerFilter>(overview.counts.pending > 0 ? "pending" : "all");
  const [keyword, setKeyword] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const rows = useMemo(() => filterStickers(overview.stickers, filter, keyword, tag), [overview.stickers, filter, keyword, tag]);
  const counts = { all: overview.counts.total, active: overview.counts.active, pending: overview.counts.pending, disabled: overview.counts.disabled };
  const editingSticker = overview.stickers.find((item) => item.id === editing) ?? null;
  const ids = [...selected].filter((id) => overview.stickers.some((item) => item.id === id));
  const bulk = async (action: StickerBulkAction) => {
    try {
      const result = await stickersApi.bulk(ids, action);
      message.success(`已处理 ${result.done.length} 张`);
      setSelected(new Set());
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  return (
    <div className="stk-library">
      <div className="stk-stats">
        <Card size="small"><Statistic title="库里" value={overview.counts.total} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日发出" value={overview.usage.totals.sent ?? 0} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日被拦下" value={overview.usage.totals.dropped ?? 0} suffix="次" /></Card>
        <Card size="small"><Statistic title="今日群友收藏" value={overview.usage.totals.collected ?? 0} suffix="张" /></Card>
      </div>
      <div className="web-toolbar">
        <Segmented
          value={filter}
          onChange={(value) => setFilter(value as StickerFilter)}
          options={(Object.keys(FILTER_LABELS) as StickerFilter[]).map((key) => ({ value: key, label: `${FILTER_LABELS[key]} ${counts[key]}` }))}
        />
        <Input.Search allowClear placeholder="搜名字、标签、说明" style={{ maxWidth: 240 }} value={keyword} onChange={(event) => setKeyword(event.target.value)} />
        <Select
          allowClear
          placeholder="按标签"
          style={{ minWidth: 140 }}
          value={tag ?? undefined}
          onChange={(value: string | undefined) => setTag(value ?? null)}
          options={overview.tags.map((item) => ({ value: item.tag, label: `${item.tag}（${item.count}）` }))}
        />
        <span className="sbx-toolbar-spacer" />
        <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => setUploading(true)}>上传</Button>
      </div>
      {ids.length > 0 && (
        <div className="stk-bulk" role="toolbar" aria-label="批量操作">
          <span>已选 {ids.length} 张</span>
          <Button size="small" icon={<CheckOutlined />} onClick={() => void bulk("approve")}>审核通过</Button>
          <Button size="small" onClick={() => void bulk("enable")}>启用</Button>
          <Button size="small" icon={<StopOutlined />} onClick={() => void bulk("disable")}>停用</Button>
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() =>
              modal.confirm({
                title: `删除 ${ids.length} 张表情包？`,
                content: "图片文件也会一起删掉，不能恢复。",
                okButtonProps: { danger: true },
                okText: "删除",
                onOk: () => bulk("delete"),
              })
            }
          >删除</Button>
          <Button size="small" type="text" onClick={() => setSelected(new Set())}>取消选择</Button>
        </div>
      )}
      {rows.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={overview.stickers.length === 0 ? "库里还没有表情包：上传几张，或者在群里让 Bot“收藏这个表情”" : "没有符合条件的表情包"}
        />
      ) : (
        <div className="stk-grid">
          {rows.map((sticker) => (
            <StickerCard
              key={sticker.id}
              sticker={sticker}
              selected={selected.has(sticker.id)}
              onOpen={() => setEditing(sticker.id)}
              onSelect={(checked) =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (checked) next.add(sticker.id);
                  else next.delete(sticker.id);
                  return next;
                })
              }
            />
          ))}
        </div>
      )}
      <EditModal sticker={editingSticker} groupNames={overview.groupNames} onClose={() => setEditing(null)} onChanged={onChanged} />
      <UploadModal open={uploading} maxBytes={overview.settings.maxBytes} onClose={() => setUploading(false)} onUploaded={onChanged} />
    </div>
  );
}

function NumberField({ field, draft, server, errors, onChange }: {
  field: StickerNumericKey;
  draft: StickerSettings;
  server: StickerSettings;
  errors: Record<string, string>;
  onChange: (key: StickerNumericKey, value: number) => void;
}): React.JSX.Element {
  const range = STICKER_SETTING_RANGES[field];
  const changed = draft[field] !== server[field];
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">
        {range.label}
        {changed && <Tag color="gold">原 {server[field]}</Tag>}
      </span>
      <InputNumber
        min={range.min}
        max={range.max}
        value={draft[field]}
        status={errors[field] ? "error" : undefined}
        onChange={(value) => onChange(field, typeof value === "number" ? Math.round(value) : range.min)}
        addonAfter={range.unit}
        style={{ width: "100%" }}
      />
      {errors[field] && <span className="sbx-field-error">{errors[field]}</span>}
    </label>
  );
}

function GroupListField({ label, value, placeholder, error, onChange }: { label: string; value: string[]; placeholder: string; error?: string; onChange: (values: string[]) => void }): React.JSX.Element {
  const [text, setText] = useState(value.join("\n"));
  useEffect(() => {
    if (parseGroupIds(text).ids.join(",") !== value.join(",")) setText(value.join("\n"));
  }, [value]);
  const invalid = parseGroupIds(text).invalid;
  return (
    <label className="sbx-field">
      <span className="sbx-field-label">{label}</span>
      <Input.TextArea
        value={text}
        autoSize={{ minRows: 2, maxRows: 6 }}
        placeholder={placeholder}
        status={error || invalid.length ? "error" : undefined}
        onChange={(event) => {
          setText(event.target.value);
          onChange(parseGroupIds(event.target.value).ids);
        }}
      />
      {(error || invalid.length > 0) && <span className="sbx-field-error">{invalid.length ? `忽略了不合法的项：${invalid.slice(0, 3).join("、")}` : error}</span>}
    </label>
  );
}

function PolicyTab({ overview, onSaved }: { overview: StickerOverview; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const server = overview.settings;
  const [draft, setDraft] = useState<StickerSettings>(server);
  const [base, setBase] = useState<StickerSettings>(server);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (stickerSettingsEqual(base, server)) return;
    setDraft((current) => (stickerSettingsEqual(current, base) ? server : current));
    setBase(server);
  }, [server]);
  const dirty = !stickerSettingsEqual(draft, server);
  useUnsavedChanges(dirty);
  const errors = useMemo(() => validateStickerSettings(draft), [draft]);
  const update = <K extends keyof StickerSettings>(key: K, value: StickerSettings[K]) => setDraft({ ...draft, [key]: value });
  const num = (key: StickerNumericKey) => <NumberField key={key} field={key} draft={draft} server={server} errors={errors} onChange={(field, value) => update(field, value)} />;
  const save = async () => {
    setSaving(true);
    try {
      setDraft(await stickersApi.saveSettings(draft));
      onSaved();
      message.success("表情包策略已保存，立即生效");
    } catch (error) {
      message.error(errorText(error, "保存失败"));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="sbx-policy">
      <div className="sbx-policy-summary">
        <div>
          <strong>{describeStickerAccess(draft)}</strong>
          <span className="sbx-muted">设置保存在 data/agent_stickers/settings.json，不需要重启 Bot。</span>
        </div>
        <span className="sbx-toolbar-spacer" />
        <SaveStatus dirty={dirty} saving={saving} />
        <Button icon={<UndoOutlined />} disabled={!dirty} onClick={() => setDraft(server)}>撤销修改</Button>
        <Popconfirm title="恢复为默认策略？" description="只改草稿，点保存后才生效。" onConfirm={() => setDraft(overview.defaults)}>
          <Button>恢复默认</Button>
        </Popconfirm>
        <Button type="primary" icon={<SaveOutlined />} disabled={!dirty || Object.keys(errors).length > 0} loading={saving} onClick={() => void save()}>保存</Button>
      </div>
      <div className="sbx-grid">
        <Card size="small" title="开放范围" className="sbx-card-wide">
          <div className="sbx-switch-row">
            <Switch checked={draft.enabled} onChange={(value) => update("enabled", value)} aria-label="允许 Agent 发表情包" />
            <div>
              <strong>允许 Agent 发表情包</strong>
              <span className="sbx-muted">关闭后模型看不到表情包菜单，写了 [表情:…] 也会被去掉。</span>
            </div>
          </div>
          <div className="sbx-switch-row">
            <Switch checked={draft.allowProactive} onChange={(value) => update("allowProactive", value)} aria-label="主动发言也能发" />
            <div>
              <strong>主动插话、暖场、续聊里也能发</strong>
              <span className="sbx-muted">人设“接梗”倾向低的角色仍然不会主动发。</span>
            </div>
          </div>
          <Radio.Group
            className="sbx-gap"
            value={draft.groupMode}
            onChange={(event) => update("groupMode", event.target.value as StickerSettings["groupMode"])}
            optionType="button"
            options={[
              { value: "all", label: "所有群" },
              { value: "allowlist", label: "只开放白名单群" },
            ]}
          />
          <div className="sbx-field-grid">
            {draft.groupMode === "allowlist" && (
              <GroupListField label="白名单群号" value={draft.allowedGroups} error={errors.allowedGroups} placeholder="每行一个群号" onChange={(values) => update("allowedGroups", values)} />
            )}
            <GroupListField label="禁用的群（优先于白名单）" value={draft.blockedGroups} placeholder="这些群永远不发表情包" onChange={(values) => update("blockedGroups", values)} />
          </div>
        </Card>
        <Card size="small" title="发送频率">
          <Radio.Group
            value={draft.frequency}
            onChange={(event) => update("frequency", event.target.value as StickerSettings["frequency"])}
            optionType="button"
            options={(["rare", "normal", "often"] as const).map((key) => ({ value: key, label: overview.frequencyLabels[key] }))}
          />
          <p className="sbx-muted">{FREQUENCY_HINTS[draft.frequency]}。这只改变提示词里的语气，下面的数字才是硬上限。</p>
          {num("groupCooldownSeconds")}
          {num("groupDailyLimit")}
          {num("maxPerMessage")}
          {num("menuSize")}
        </Card>
        <Card size="small" title="群友收藏（save_sticker）">
          <Radio.Group
            value={draft.collectMode}
            onChange={(event) => update("collectMode", event.target.value as StickerSettings["collectMode"])}
            optionType="button"
            options={(["off", "review", "auto"] as const).map((key) => ({ value: key, label: COLLECT_LABELS[key] }))}
          />
          <p className="sbx-muted">群友回复一张图说“收藏这个表情”，Bot 会把它存进库；群主/管理员收藏的直接可用。</p>
          <div className="sbx-switch-row">
            <Switch checked={draft.collectAdminOnly} onChange={(value) => update("collectAdminOnly", value)} aria-label="只有管理员能收藏" />
            <div><strong>只有群主/管理员能收藏</strong></div>
          </div>
          {num("memberDailyCollect")}
        </Card>
      </div>
    </div>
  );
}

function PreviewTab({ overview }: { overview: StickerOverview }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const groups = useMemo(() => {
    const ids = new Set<string>([...Object.keys(overview.groupNames), ...overview.usage.groups.map((row) => row.groupId)]);
    return [...ids].map((id) => ({ value: id, label: overview.groupNames[id] ? `${overview.groupNames[id]}（${id}）` : id }));
  }, [overview]);
  const [groupId, setGroupId] = useState("");
  const [preview, setPreview] = useState<StickerPromptPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const load = async () => {
    if (!/^\d{4,12}$/.test(groupId.trim())) {
      message.warning("填一个群号");
      return;
    }
    setLoading(true);
    try {
      setPreview(await stickersApi.promptPreview(groupId.trim()));
    } catch (error) {
      message.error(errorText(error, "预览失败"));
    } finally {
      setLoading(false);
    }
  };
  return (
    <div className="web-test">
      <Card size="small" title="这个群现在一轮对话里，模型看到的表情包说明">
        <div className="web-toolbar">
          <Select
            showSearch
            style={{ minWidth: 240 }}
            placeholder="选择或输入群号"
            value={groupId || undefined}
            options={groups}
            onSearch={(value) => setGroupId(value.replace(/\D/g, ""))}
            onChange={(value: string) => setGroupId(value)}
            notFoundContent={null}
          />
          <Button type="primary" icon={<EyeOutlined />} loading={loading} onClick={() => void load()}>预览</Button>
        </div>
        <p className="sbx-muted">菜单按“本群可用、最近没发过、每小时换一批”挑，不看聊天内容；人设的接梗倾向和发言场景也会影响是否出现（在调试页看完整提示词）。</p>
        {preview && (
          <>
            {!preview.open && <Alert type="info" showIcon message={`这一轮不会给模型看表情包：${preview.reasonText ?? preview.reason}`} description={preview.reason === "cooldown" ? `还要冷却 ${Math.ceil(preview.cooldownSeconds)} 秒` : undefined} />}
            {preview.open && (
              <>
                <p className="sbx-muted">本群可用 {preview.available} 张，今日还能发 {preview.leftToday} 张。</p>
                <pre className="stk-prompt" aria-label="提示词预览">{preview.text}</pre>
              </>
            )}
          </>
        )}
      </Card>
    </div>
  );
}

export function StickersPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseTab(searchParams.get("stk.tab"));
  const query = useApiQuery<StickerOverview>({
    queryKey: ["stickers-overview"],
    fetcher: (signal) => stickersApi.overview(signal),
    invalidation: { resources: ["stickers"] },
  });
  const overview = query.data;
  const setTab = (next: StickerTab) => {
    setSearchParams(updateSearchParams(searchParams, { "stk.tab": next === "library" ? null : next }), { replace: true });
  };
  if (!overview) {
    return (
      <div className="sbx-page">
        <h2 className="sbx-title">表情包</h2>
        {query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />}
      </div>
    );
  }
  return (
    <div className="sbx-page">
      <div className="sbx-heading">
        <div>
          <h2 className="sbx-title"><SmileOutlined /> 表情包</h2>
          <p>Agent 在回复里写 [表情:名字] 就会单独发一张表情包，和分条消息一样有节奏；这里管表情包库、发送频率和群友收藏</p>
        </div>
      </div>
      <StickerStatusBar overview={overview} reloading={query.refreshing} onReload={query.reload} />
      <Tabs
        className="sbx-tabs"
        activeKey={tab}
        onChange={(key) => setTab(key as StickerTab)}
        destroyOnHidden
        items={[
          { key: "library", label: <span><PictureOutlined /> 表情包库</span>, children: <LibraryTab overview={overview} onChanged={query.reload} /> },
          { key: "policy", label: <span><SafetyCertificateOutlined /> 策略与频率</span>, children: <PolicyTab overview={overview} onSaved={query.reload} /> },
          { key: "preview", label: <span><EyeOutlined /> 菜单预览</span>, children: <PreviewTab overview={overview} /> },
        ]}
      />
    </div>
  );
}
