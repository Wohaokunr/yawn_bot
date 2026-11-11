// 表情包库：筛选/排序/密度都记在 URL 和本机，卡片、编辑框、批量操作、标签整理、导入导出。
import {
  CheckOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  DownOutlined,
  ImportOutlined,
  LeftOutlined,
  PushpinFilled,
  PushpinOutlined,
  RightOutlined,
  RobotOutlined,
  RollbackOutlined,
  SaveOutlined,
  StopOutlined,
  SwapOutlined,
  TagsOutlined,
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Checkbox, Dropdown, Empty, Input, InputNumber, Modal, Popconfirm, Segmented, Select, Statistic, Switch, Tag, Tooltip } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { parseGroupIds } from "../web-search/web-search-meta";
import { JobBanner, useLabelJobs } from "./StickerAiTab";
import { ImportModal, saveBlob } from "./StickerTransfer";
import { UploadModal } from "./StickerUpload";
import { exportStickers, replaceStickerImage, stickerImageUrl, stickersApi } from "./stickers-api";
import {
  AI_FILTER_LABELS,
  cleanAliases,
  cleanName,
  confidenceText,
  describeOrigin,
  FILTER_LABELS,
  filterStickers,
  FLAG_LABELS,
  formatBytes,
  isAiLabeled,
  LABEL_MODE_HINTS,
  LABEL_MODE_LABELS,
  parseEnum,
  parseTags,
  SORT_LABELS,
  sortStickers,
  SOURCE_LABELS,
  stickerState,
  WEIGHT_HINTS,
} from "./stickers-meta";
import type { AiFilter, FlagFilter, SourceFilter, StickerFilter, StickerSort } from "./stickers-meta";
import type { LabelMode, Sticker, StickerBulkAction, StickerBulkExtra, StickerLabel, StickerOverview } from "./types";

const STATE_COLORS: Record<Exclude<StickerFilter, "all">, string> = { active: "green", pending: "gold", disabled: "default" };
const PAGE = 120;
const DENSITY_KEY = "yawnbot.stk.density";
type Density = "cozy" | "compact";

export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function readDensity(): Density {
  try {
    return window.localStorage.getItem(DENSITY_KEY) === "compact" ? "compact" : "cozy";
  } catch {
    return "cozy";
  }
}

function StickerCard({ sticker, selected, onSelect, onOpen, onPin }: { sticker: Sticker; selected: boolean; onSelect: (checked: boolean) => void; onOpen: () => void; onPin: () => void }): React.JSX.Element {
  const state = stickerState(sticker);
  return (
    <div className={`stk-card is-${state}${selected ? " is-selected" : ""}${sticker.pinned ? " is-pinned" : ""}`}>
      <Checkbox className="stk-check" checked={selected} onChange={(event) => onSelect(event.target.checked)} aria-label={`选择 ${sticker.name}`} />
      <Tooltip title={sticker.pinned ? "取消置顶" : "置顶：每轮都进菜单"}>
        <button type="button" className={`stk-pin${sticker.pinned ? " is-on" : ""}`} onClick={onPin} aria-label={`${sticker.pinned ? "取消置顶" : "置顶"} ${sticker.name}`} aria-pressed={sticker.pinned}>
          {sticker.pinned ? <PushpinFilled /> : <PushpinOutlined />}
        </button>
      </Tooltip>
      <button type="button" className="stk-thumb" onClick={onOpen} aria-label={`编辑 ${sticker.name}`}>
        {sticker.fileMissing ? <span className="stk-missing">文件丢失</span> : <img src={stickerImageUrl(sticker)} alt={sticker.name} loading="lazy" />}
        {sticker.animated && <span className="stk-badge">动图</span>}
      </button>
      <div className="stk-meta">
        <strong title={sticker.aliases.length ? `${sticker.name}（也叫 ${sticker.aliases.join("、")}）` : sticker.name}>{sticker.name}</strong>
        <div className="stk-tags">
          {state !== "active" && <Tag color={STATE_COLORS[state]}>{FILTER_LABELS[state]}</Tag>}
          {sticker.source === "stolen" && <Tag color="purple">偷的</Tag>}
          {sticker.weight === 0 && <Tooltip title={WEIGHT_HINTS[0]}><Tag>不主动用</Tag></Tooltip>}
          {sticker.weight >= 2 && <Tooltip title={WEIGHT_HINTS[sticker.weight]}><Tag color="magenta">{sticker.weight >= 3 ? "最爱" : "多用"}</Tag></Tooltip>}
          {isAiLabeled(sticker) && (
            <Tooltip title={`AI 标注${sticker.ai?.mood ? ` · ${sticker.ai.mood}` : ""}${sticker.ai?.text ? ` · 图上写着“${sticker.ai.text}”` : ""}${sticker.ai?.safe === false ? " · AI 觉得可能不合适" : ""}`}>
              <Tag color={sticker.ai?.safe === false ? "red" : "blue"} icon={<RobotOutlined />} className="stk-ai-tag" aria-label="AI 标注过" />
            </Tooltip>
          )}
          {sticker.tags.slice(0, 3).map((tag) => <Tag key={tag}>{tag}</Tag>)}
        </div>
        <span className="sbx-muted">
          {sticker.uses > 0 ? `发过 ${sticker.uses} 次` : "还没发过"}
          {sticker.sentToday > 0 ? ` · 今天 ${sticker.sentToday}` : ""}
          {sticker.groups.length > 0 ? ` · 限 ${sticker.groups.length} 个群` : ""}
        </span>
      </div>
    </div>
  );
}

function EditModal({ sticker, overview, position, onStep, onClose, onChanged }: {
  sticker: Sticker | null;
  overview: StickerOverview;
  position: { index: number; total: number };
  onStep: (delta: number) => void;
  onClose: () => void;
  onChanged: () => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const groupNames = overview.groupNames;
  const [name, setName] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [aliases, setAliases] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const [groups, setGroups] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [pinned, setPinned] = useState(false);
  const [weight, setWeight] = useState(1);
  const [packs, setPacks] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [dailyCap, setDailyCap] = useState(0);
  const [busy, setBusy] = useState(false);
  const [suggestion, setSuggestion] = useState<StickerLabel | null>(null);
  const [labeling, setLabeling] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setSuggestion(null);
    if (!sticker) return;
    setName(sticker.name);
    setTags(sticker.tags);
    setAliases(sticker.aliases);
    setDescription(sticker.description);
    setGroups(sticker.groups.join("\n"));
    setEnabled(sticker.enabled);
    setPinned(sticker.pinned);
    setWeight(sticker.weight);
    setPacks(sticker.packs);
    setNote(sticker.note);
    setDailyCap(sticker.dailyCap);
  }, [sticker]);
  if (!sticker) return <Modal open={false} onCancel={onClose} footer={null} />;
  const parsedGroups = parseGroupIds(groups);
  const run = async (action: () => Promise<unknown>, done: string, close = true) => {
    setBusy(true);
    try {
      await action();
      message.success(done);
      onChanged();
      if (close) onClose();
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
          aliases: cleanAliases(aliases, name),
          pinned,
          weight,
          packs,
          note,
          dailyCap,
          ...(approve ? { status: "active" as const } : {}),
        }),
      approve ? "已审核通过" : "已保存",
    );
  const addedGroup = sticker.addedGroup ? groupNames[sticker.addedGroup] ?? sticker.addedGroup : null;
  const origin = describeOrigin(sticker, groupNames);
  const askAi = async () => {
    setLabeling(true);
    try {
      const result = await stickersApi.label(sticker.id, { apply: false, mode: "replace" });
      if (result.label?.ok) setSuggestion(result.label);
      else message.warning(result.label?.reasonText ?? "AI 没认出来");
    } catch (error) {
      message.error(errorText(error, "AI 识别失败"));
    } finally {
      setLabeling(false);
    }
  };
  const adopt = (label: StickerLabel) => {
    setName(label.name);
    setTags(label.tags);
    if (label.description) setDescription(label.description);
    if (label.aliases?.length) setAliases(cleanAliases(label.aliases, label.name));
    setSuggestion(null);
  };
  const trashDays = overview.settings.trashDays;
  return (
    <Modal
      open
      title={
        <span className="stk-edit-title">
          编辑表情包 · {sticker.name}
          {position.total > 1 && (
            <span className="stk-edit-nav">
              <Button size="small" type="text" icon={<LeftOutlined />} disabled={position.index <= 0} onClick={() => onStep(-1)} aria-label="上一张" />
              <span className="sbx-muted">{position.index + 1} / {position.total}</span>
              <Button size="small" type="text" icon={<RightOutlined />} disabled={position.index >= position.total - 1} onClick={() => onStep(1)} aria-label="下一张" />
            </span>
          )}
        </span>
      }
      onCancel={onClose}
      width={620}
      footer={[
        <Dropdown
          key="delete"
          disabled={busy}
          menu={{
            items: [
              { key: "trash", label: trashDays > 0 ? `放进回收站（留 ${trashDays} 天）` : "删除" },
              { key: "purge", label: "彻底删除（不进回收站）", danger: true },
              { key: "block", label: "彻底删除并拉黑（以后不再收这张）", danger: true },
            ],
            onClick: ({ key }) => void run(() => stickersApi.remove(sticker.id, trashDays > 0 ? (key as "trash" | "purge" | "block") : key === "block" ? "block" : "purge"), key === "trash" && trashDays > 0 ? "已放进回收站" : key === "block" ? "已删除并拉黑" : "已删除"),
          }}
        >
          <Button danger icon={<DeleteOutlined />}>删除 <DownOutlined /></Button>
        </Dropdown>,
        sticker.status === "pending" ? (
          <Button key="approve" icon={<CheckOutlined />} loading={busy} disabled={!cleanName(name)} onClick={() => void save(true)}>审核通过</Button>
        ) : null,
        <Button key="save" type="primary" icon={<SaveOutlined />} loading={busy} disabled={!cleanName(name)} onClick={() => void save(false)}>保存</Button>,
      ]}
    >
      <div className="stk-edit">
        <div className="stk-edit-preview">
          {sticker.fileMissing ? <span className="stk-missing">文件丢失</span> : <img src={stickerImageUrl(sticker)} alt={sticker.name} />}
          <span className="sbx-muted">
            {formatBytes(sticker.size)}{sticker.dimensions ? ` · ${sticker.dimensions}` : ""}{sticker.shape ? ` · ${sticker.shape}` : ""} · {SOURCE_LABELS[sticker.source]}{addedGroup ? ` · 来自 ${addedGroup}` : ""}{sticker.addedBy ? `（${sticker.addedBy}）` : ""}
          </span>
          {origin && <span className="sbx-muted">{origin}</span>}
          <span className="sbx-muted">
            {sticker.uses > 0 ? `一共发过 ${sticker.uses} 次${sticker.lastUsed ? `，上次 ${sticker.lastUsed}` : ""}` : "还没发过"}
            {sticker.groupUses.length > 0 ? `；${sticker.groupUses.slice(0, 4).map((row) => `${row.groupId ? groupNames[row.groupId] ?? row.groupId : "私聊"} ${row.uses}`).join("、")}` : ""}
          </span>
          <input
            ref={fileInput}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            hidden
            aria-label="选择要换上的图片"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void run(() => replaceStickerImage(sticker.id, file), "图片已换好", false);
            }}
          />
          <Button size="small" icon={<SwapOutlined />} disabled={busy} onClick={() => fileInput.current?.click()}>换一张图（名字和统计都留着）</Button>
        </div>
        <div className="stk-ai-box">
          <div className="stk-ai-box-head">
            <span>
              <RobotOutlined /> {isAiLabeled(sticker) ? `AI 标注过${sticker.ai?.labeledAt ? `（${sticker.ai.labeledAt}）` : ""}` : "还没让 AI 看过"}
              {sticker.ai?.confidence != null && isAiLabeled(sticker) ? ` · ${confidenceText(sticker.ai.confidence)}` : ""}
              {sticker.ai?.mood && isAiLabeled(sticker) ? ` · ${sticker.ai.mood}` : ""}
            </span>
            <span className="stk-actions">
              {sticker.canUndoLabel && (
                <Button size="small" icon={<RollbackOutlined />} disabled={busy} onClick={() => void run(() => stickersApi.undoLabel(sticker.id), "已撤销 AI 标注")}>撤销 AI 标注</Button>
              )}
              <Button size="small" icon={<RobotOutlined />} loading={labeling} onClick={() => void askAi()}>AI 重新识别</Button>
            </span>
          </div>
          {sticker.ai?.text ? <span className="sbx-muted">图上的字：{sticker.ai.text}</span> : null}
          {sticker.ai && !sticker.ai.safe && <Alert type="warning" showIcon message="AI 觉得这张可能不适合当表情包" />}
          {suggestion && (
            <div className="stk-suggestion" aria-label="AI 建议">
              <span>
                建议：<strong>{suggestion.name}</strong> {suggestion.tags.map((tag) => <Tag key={tag}>{tag}</Tag>)}
                {suggestion.aliases?.length ? <span className="sbx-muted"> · 也叫 {suggestion.aliases.join("、")}</span> : null}
                {suggestion.description ? <span className="sbx-muted"> · {suggestion.description}</span> : null}
              </span>
              <span className="stk-actions">
                <Button size="small" type="primary" onClick={() => adopt(suggestion)}>填进表单</Button>
                <Button size="small" type="text" onClick={() => setSuggestion(null)}>不用</Button>
              </span>
            </div>
          )}
        </div>
        <label className="sbx-field">
          <span className="sbx-field-label">名字（模型写 [表情:名字] 发它）</span>
          <Input value={name} maxLength={16} showCount onChange={(event) => setName(event.target.value)} status={cleanName(name) ? undefined : "error"} />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">别名（模型写别名也能发出这张，最多 4 个）</span>
          <Select mode="tags" value={aliases} tokenSeparators={[",", "，", "、"]} onChange={(values: string[]) => setAliases(cleanAliases(values, name))} placeholder="如 doge、狗头保命" aria-label="别名" />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">情绪 / 场景标签（模型也可以写 [表情:标签]，同标签的随机挑一张）</span>
          <Select mode="tags" value={tags} tokenSeparators={[",", "，", " ", "、"]} onChange={(values: string[]) => setTags(parseTags(values.join(",")))} placeholder="如 无语、尴尬、开心" />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">什么时候适合发（会出现在菜单里，越短越好）</span>
          <Input value={description} maxLength={160} onChange={(event) => setDescription(event.target.value)} placeholder="如：被戳穿又不想承认时" />
        </label>
        <div className="sbx-field-grid">
          <label className="sbx-field">
            <span className="sbx-field-label">多用还是少用</span>
            <Select
              value={weight}
              aria-label="使用倾向"
              onChange={(value: number) => setWeight(value)}
              options={[0, 1, 2, 3].map((value) => ({ value, label: overview.weightLabels[String(value)] ?? String(value) }))}
            />
            <span className="sbx-muted">{WEIGHT_HINTS[weight]}</span>
          </label>
          <label className="sbx-field">
            <span className="sbx-field-label">这张每天最多发（0 = 不限）</span>
            <InputNumber min={0} max={1000} value={dailyCap} onChange={(value) => setDailyCap(typeof value === "number" ? Math.round(value) : 0)} addonAfter="次" style={{ width: "100%" }} aria-label="单张每日上限" />
            <span className="sbx-muted">今天已经发了 {sticker.sentToday} 次（所有群加私聊）</span>
          </label>
        </div>
        {overview.packs.length > 0 && (
          <label className="sbx-field">
            <span className="sbx-field-label">所在合集</span>
            <Select mode="multiple" value={packs} onChange={(values: string[]) => setPacks(values)} options={overview.packs.map((pack) => ({ value: pack.id, label: pack.enabled ? pack.name : `${pack.name}（已停用）` }))} placeholder="不属于任何合集" aria-label="所在合集" />
          </label>
        )}
        <label className="sbx-field">
          <span className="sbx-field-label">只在这些群可用（留空 = 所有群）</span>
          <Input.TextArea value={groups} autoSize={{ minRows: 1, maxRows: 4 }} onChange={(event) => setGroups(event.target.value)} placeholder="每行一个群号" status={parsedGroups.invalid.length ? "error" : undefined} />
        </label>
        <label className="sbx-field">
          <span className="sbx-field-label">备注（只有管理员看得到，模型看不到）</span>
          <Input value={note} maxLength={120} onChange={(event) => setNote(event.target.value)} placeholder="如：群主专属，别删" aria-label="备注" />
        </label>
        <div className="sbx-switch-row">
          <Switch checked={pinned} onChange={setPinned} aria-label="置顶" />
          <div>
            <strong>置顶</strong>
            <span className="sbx-muted">每一轮都放进菜单最前面，不参与轮换。</span>
          </div>
        </div>
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

type BulkPrompt = { action: "add_tags" | "remove_tags" | "set_groups" | "add_pack" | "remove_pack" | "set_weight" } | null;

const BULK_PROMPT_TITLES: Record<NonNullable<BulkPrompt>["action"], string> = {
  add_tags: "给选中的加标签",
  remove_tags: "从选中的去掉标签",
  set_groups: "设置选中的适用群",
  add_pack: "放进合集",
  remove_pack: "移出合集",
  set_weight: "设置多用还是少用",
};

function BulkPromptModal({ prompt, count, overview, onClose, onSubmit }: {
  prompt: BulkPrompt;
  count: number;
  overview: StickerOverview;
  onClose: () => void;
  onSubmit: (action: StickerBulkAction, extra: StickerBulkExtra) => Promise<void>;
}): React.JSX.Element {
  const [tags, setTags] = useState<string[]>([]);
  const [groups, setGroups] = useState("");
  const [pack, setPack] = useState<string | undefined>(undefined);
  const [weight, setWeight] = useState(1);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setTags([]);
    setGroups("");
    setPack(undefined);
    setWeight(1);
  }, [prompt]);
  if (!prompt) return <Modal open={false} onCancel={onClose} footer={null} />;
  const parsed = parseGroupIds(groups);
  const ready =
    prompt.action === "add_tags" || prompt.action === "remove_tags" ? tags.length > 0 : prompt.action === "add_pack" || prompt.action === "remove_pack" ? Boolean(pack) : prompt.action === "set_groups" ? parsed.invalid.length === 0 : true;
  const submit = async () => {
    setBusy(true);
    try {
      await onSubmit(prompt.action, { tags, groups: parsed.ids, pack, weight });
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open title={`${BULK_PROMPT_TITLES[prompt.action]}（${count} 张）`} onCancel={onClose} okText="应用" okButtonProps={{ disabled: !ready, loading: busy }} onOk={() => void submit()} width={460}>
      {(prompt.action === "add_tags" || prompt.action === "remove_tags") && (
        <Select
          mode="tags"
          style={{ width: "100%" }}
          value={tags}
          tokenSeparators={[",", "，", " ", "、"]}
          onChange={(values: string[]) => setTags(parseTags(values.join(",")))}
          options={overview.tags.map((item) => ({ value: item.tag, label: `${item.tag}（${item.count}）` }))}
          placeholder={prompt.action === "add_tags" ? "写要加的标签" : "选要去掉的标签"}
          aria-label="标签"
        />
      )}
      {prompt.action === "set_groups" && (
        <>
          <Input.TextArea value={groups} autoSize={{ minRows: 2, maxRows: 6 }} onChange={(event) => setGroups(event.target.value)} placeholder="每行一个群号；留空 = 所有群都能用" status={parsed.invalid.length ? "error" : undefined} aria-label="适用群" />
          <p className="sbx-muted">会把这些表情包原来的适用群整个换掉。</p>
        </>
      )}
      {(prompt.action === "add_pack" || prompt.action === "remove_pack") && (
        <Select style={{ width: "100%" }} value={pack} onChange={(value: string) => setPack(value)} options={overview.packs.map((item) => ({ value: item.id, label: `${item.name}（${item.count}）` }))} placeholder="选一个合集" aria-label="合集" notFoundContent="还没有合集，先到“合集”标签建一个" />
      )}
      {prompt.action === "set_weight" && (
        <>
          <Segmented value={weight} onChange={(value) => setWeight(Number(value))} options={[0, 1, 2, 3].map((value) => ({ value, label: overview.weightLabels[String(value)] ?? String(value) }))} />
          <p className="sbx-muted">{WEIGHT_HINTS[weight]}</p>
        </>
      )}
    </Modal>
  );
}

function TagManager({ open, overview, onClose, onChanged }: { open: boolean; overview: StickerOverview; onClose: () => void; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [source, setSource] = useState<string | undefined>(undefined);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) {
      setSource(undefined);
      setTarget("");
    }
  }, [open]);
  const cleaned = parseTags(target)[0] ?? "";
  const merging = Boolean(cleaned) && overview.tags.some((item) => item.tag === cleaned && item.tag !== source);
  const run = async (action: () => Promise<{ changed: number }>, verb: string) => {
    setBusy(true);
    try {
      const result = await action();
      message.success(`${verb}，改了 ${result.changed} 张`);
      onChanged();
      setSource(undefined);
      setTarget("");
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} title="整理标签" onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>} width={480}>
      <p className="sbx-muted">同一个意思的标签写法不一样（“搞笑 / 好笑”），模型按情绪挑图时就会分散。这里可以全库改名、合并或删除一个标签。</p>
      <label className="sbx-field">
        <span className="sbx-field-label">要整理的标签</span>
        <Select showSearch value={source} onChange={(value: string) => setSource(value)} options={overview.tags.map((item) => ({ value: item.tag, label: `${item.tag}（${item.count} 张）` }))} placeholder="选一个标签" aria-label="要整理的标签" />
      </label>
      <label className="sbx-field">
        <span className="sbx-field-label">改成（写一个已有的标签就是合并）</span>
        <Input value={target} maxLength={10} onChange={(event) => setTarget(event.target.value)} placeholder="新的标签名" aria-label="新的标签名" />
        {merging && <span className="sbx-muted">“{cleaned}”已经有了，会把两个标签合并成一个。</span>}
      </label>
      <div className="stk-actions">
        <Button type="primary" disabled={!source || !cleaned || cleaned === source} loading={busy} onClick={() => source && void run(() => stickersApi.renameTag(source, cleaned), merging ? "已合并" : "已改名")}>{merging ? "合并" : "改名"}</Button>
        <Popconfirm title={`从所有表情包上去掉“${source ?? ""}”？`} onConfirm={() => source && void run(() => stickersApi.deleteTag(source), "已删除标签")} disabled={!source}>
          <Button danger disabled={!source || busy}>删除这个标签</Button>
        </Popconfirm>
      </div>
    </Modal>
  );
}

export function LibraryTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const setParam = (changes: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, changes), { replace: true });
  const filterKeys = Object.keys(FILTER_LABELS) as StickerFilter[];
  const filter = parseEnum(searchParams.get("stk.f"), filterKeys, overview.counts.pending > 0 ? "pending" : "all");
  const keyword = searchParams.get("stk.q") ?? "";
  const tag = searchParams.get("stk.tag");
  const source = parseEnum<SourceFilter>(searchParams.get("stk.src"), ["any", ...(Object.keys(SOURCE_LABELS) as Sticker["source"][])], "any");
  const ai = parseEnum(searchParams.get("stk.ai"), Object.keys(AI_FILTER_LABELS) as AiFilter[], "any");
  const pack = searchParams.get("stk.pack");
  const mood = searchParams.get("stk.mood");
  const flagParam = searchParams.get("stk.flag");
  const flag = flagParam && flagParam in FLAG_LABELS ? (flagParam as FlagFilter) : null;
  const sort = parseEnum(searchParams.get("stk.sort"), Object.keys(SORT_LABELS) as StickerSort[], "default");
  const editing = searchParams.get("stk.edit");
  const [labelMode, setLabelMode] = useState<LabelMode>("fill");
  const { running, track, cancel } = useLabelJobs(overview.labelJobs, onChanged);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [uploading, setUploading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [tagging, setTagging] = useState(false);
  const [prompt, setPrompt] = useState<BulkPrompt>(null);
  const [limit, setLimit] = useState(PAGE);
  const [density, setDensity] = useState<Density>(readDensity);
  const rows = useMemo(
    () => sortStickers(filterStickers(overview.stickers, filter, keyword, tag, source, ai, { pack, mood, flag }), sort),
    [overview.stickers, filter, keyword, tag, source, ai, pack, mood, flag, sort],
  );
  useEffect(() => setLimit(PAGE), [filter, keyword, tag, source, ai, pack, mood, flag, sort]);
  const visible = rows.slice(0, limit);
  const aiReady = overview.vision.ready && overview.settings.aiLabelEnabled;
  const counts = { all: overview.counts.total, active: overview.counts.active, pending: overview.counts.pending, disabled: overview.counts.disabled };
  const editingSticker = overview.stickers.find((item) => item.id === editing) ?? null;
  const editingIndex = editingSticker ? rows.findIndex((item) => item.id === editingSticker.id) : -1;
  const ids = [...selected].filter((id) => overview.stickers.some((item) => item.id === id));
  const filtered = Boolean(keyword || tag || pack || mood || flag || source !== "any" || ai !== "any");
  const changeDensity = (next: Density) => {
    setDensity(next);
    try {
      window.localStorage.setItem(DENSITY_KEY, next);
    } catch {
      // 本机偏好存不下就算了
    }
  };
  const bulk = async (action: StickerBulkAction, extra: StickerBulkExtra = {}) => {
    try {
      const result = await stickersApi.bulk(ids, action, extra);
      message.success(`已处理 ${result.done.length} 张${result.missing.length ? `，${result.missing.length} 张没找到` : ""}`);
      setSelected(new Set());
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  const submitPrompt = (action: StickerBulkAction, extra: StickerBulkExtra) => {
    const body: StickerBulkExtra =
      action === "add_tags" || action === "remove_tags" ? { tags: extra.tags } : action === "set_groups" ? { groups: extra.groups } : action === "set_weight" ? { weight: extra.weight } : { pack: extra.pack };
    return bulk(action, body);
  };
  const labelSelected = async () => {
    try {
      const job = await stickersApi.startJob({ scope: "selected", ids, mode: labelMode });
      track(job);
      setSelected(new Set());
      message.success(`开始让 AI 标注 ${job.total} 张`);
    } catch (error) {
      message.error(errorText(error, "没能开始"));
    }
  };
  const exportNow = async (which: "selected" | "all" | "filtered") => {
    try {
      const wanted = which === "selected" ? ids : which === "filtered" ? rows.map((item) => item.id) : undefined;
      const result = await exportStickers({ ids: wanted });
      saveBlob(result.blob, "yawnbot-stickers.zip");
      message.success(`导出了 ${result.count} 张${result.skipped ? `，${result.skipped} 张文件丢了没带上` : ""}`);
    } catch (error) {
      message.error(errorText(error, "导出失败"));
    }
  };
  const togglePin = async (sticker: Sticker) => {
    try {
      await stickersApi.update(sticker.id, { pinned: !sticker.pinned });
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  const confirmBulk = (title: string, content: string, action: StickerBulkAction) =>
    modal.confirm({ title, content, okButtonProps: { danger: true }, okText: "确定", onOk: () => bulk(action) });
  const trashDays = overview.settings.trashDays;
  return (
    <div className="stk-library">
      {overview.index.corrupt && (
        <Alert
          type="error"
          showIcon
          message="表情包索引文件坏过"
          description={overview.index.recoveredFromBackup ? "这次是从自动备份读回来的，最近一次修改可能丢了。随便编辑一张保存一次就会写出新的好索引；坏掉的文件留在库目录里（index.corrupt-…）。" : "没有可用的备份，库现在是空的。坏掉的文件还在库目录里，没有被覆盖。"}
        />
      )}
      <div className="stk-stats">
        <Card size="small"><Statistic title="库里" value={overview.counts.total} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日发出" value={overview.usage.totals.sent ?? 0} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日私聊发出" value={overview.usage.totals.private_sent ?? 0} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日被拦下" value={overview.usage.totals.dropped ?? 0} suffix="次" /></Card>
        <Card size="small"><Statistic title="今日群友收藏" value={overview.usage.totals.collected ?? 0} suffix="张" /></Card>
        <Card size="small"><Statistic title="今日 Bot 偷图" value={overview.usage.totals.stolen ?? 0} suffix="张" /></Card>
      </div>
      {running && <JobBanner job={running} onCancel={() => void cancel(running.id)} />}
      <div className="web-toolbar">
        <Segmented
          value={filter}
          onChange={(value) => setParam({ "stk.f": String(value) })}
          options={filterKeys.map((key) => ({ value: key, label: `${FILTER_LABELS[key]} ${counts[key]}` }))}
        />
        <Input.Search allowClear placeholder="搜名字、别名、标签、说明、图上的字、备注" style={{ maxWidth: 280 }} value={keyword} onChange={(event) => setParam({ "stk.q": event.target.value || null })} />
        <span className="sbx-toolbar-spacer" />
        <Dropdown
          menu={{
            items: [
              { key: "all", label: `导出整个库（${overview.counts.total} 张）`, disabled: overview.counts.total === 0 },
              { key: "filtered", label: `导出当前筛出来的（${rows.length} 张）`, disabled: rows.length === 0 },
              { type: "divider" },
              { key: "import", label: "导入一个导出的包…", icon: <ImportOutlined /> },
            ],
            onClick: ({ key }) => (key === "import" ? setImporting(true) : void exportNow(key as "all" | "filtered")),
          }}
        >
          <Button icon={<DownloadOutlined />}>导入导出 <DownOutlined /></Button>
        </Dropdown>
        <Button icon={<TagsOutlined />} onClick={() => setTagging(true)} disabled={overview.tags.length === 0}>整理标签</Button>
        <Button type="primary" icon={<CloudUploadOutlined />} onClick={() => setUploading(true)}>上传</Button>
      </div>
      <div className="web-toolbar stk-filters">
        <Select
          allowClear
          placeholder="按标签"
          style={{ minWidth: 130 }}
          value={tag ?? undefined}
          onChange={(value: string | undefined) => setParam({ "stk.tag": value ?? null })}
          options={overview.tags.map((item) => ({ value: item.tag, label: `${item.tag}（${item.count}）` }))}
        />
        {overview.packs.length > 0 && (
          <Select allowClear placeholder="按合集" aria-label="按合集" style={{ minWidth: 130 }} value={pack ?? undefined} onChange={(value: string | undefined) => setParam({ "stk.pack": value ?? null })} options={overview.packs.map((item) => ({ value: item.id, label: `${item.name}（${item.count}）` }))} />
        )}
        <Select allowClear placeholder="按情绪" aria-label="按情绪" style={{ minWidth: 110 }} value={mood ?? undefined} onChange={(value: string | undefined) => setParam({ "stk.mood": value ?? null })} options={overview.moods.map((item) => ({ value: item, label: item }))} />
        <Select
          aria-label="按来源"
          style={{ minWidth: 120 }}
          value={source}
          onChange={(value: SourceFilter) => setParam({ "stk.src": value === "any" ? null : value })}
          options={[{ value: "any", label: "全部来源" }, ...(Object.keys(SOURCE_LABELS) as Sticker["source"][]).map((key) => ({ value: key, label: SOURCE_LABELS[key] }))]}
        />
        <Select
          aria-label="按 AI 标注"
          style={{ minWidth: 140 }}
          value={ai}
          onChange={(value: AiFilter) => setParam({ "stk.ai": value === "any" ? null : value })}
          options={(Object.keys(AI_FILTER_LABELS) as AiFilter[]).map((key) => ({ value: key, label: AI_FILTER_LABELS[key] }))}
        />
        <Select allowClear placeholder="更多条件" aria-label="更多条件" style={{ minWidth: 130 }} value={flag ?? undefined} onChange={(value: FlagFilter | undefined) => setParam({ "stk.flag": value ?? null })} options={(Object.keys(FLAG_LABELS) as FlagFilter[]).map((key) => ({ value: key, label: FLAG_LABELS[key] }))} />
        <Select aria-label="排序" style={{ minWidth: 150 }} value={sort} onChange={(value: StickerSort) => setParam({ "stk.sort": value === "default" ? null : value })} options={(Object.keys(SORT_LABELS) as StickerSort[]).map((key) => ({ value: key, label: SORT_LABELS[key] }))} />
        {filtered && <Button type="link" size="small" onClick={() => setParam({ "stk.q": null, "stk.tag": null, "stk.pack": null, "stk.mood": null, "stk.flag": null, "stk.src": null, "stk.ai": null })}>清掉筛选</Button>}
        <span className="sbx-toolbar-spacer" />
        <Segmented size="small" value={density} onChange={(value) => changeDensity(value as Density)} options={[{ value: "cozy", label: "大图" }, { value: "compact", label: "小图" }]} aria-label="卡片大小" />
      </div>
      <div className="stk-selectbar">
        <span className="sbx-muted">{filtered || filter !== "all" ? `筛出 ${rows.length} 张` : `共 ${rows.length} 张`}{rows.length > visible.length ? `，先显示 ${visible.length} 张` : ""}</span>
        {rows.length > 0 && (
          <>
            <Button type="link" size="small" onClick={() => setSelected(new Set(rows.map((item) => item.id)))}>全选这 {rows.length} 张</Button>
            <Button type="link" size="small" onClick={() => setSelected((current) => new Set(rows.filter((item) => !current.has(item.id)).map((item) => item.id)))}>反选</Button>
          </>
        )}
      </div>
      {ids.length > 0 && (
        <div className="stk-bulk" role="toolbar" aria-label="批量操作">
          <span>已选 {ids.length} 张</span>
          <Button size="small" icon={<CheckOutlined />} onClick={() => void bulk("approve")}>审核通过</Button>
          {aiReady && (
            <span className="stk-bulk-ai">
              <Select size="small" value={labelMode} onChange={(value: LabelMode) => setLabelMode(value)} aria-label="AI 标注方式" style={{ minWidth: 132 }} options={(Object.keys(LABEL_MODE_LABELS) as LabelMode[]).map((key) => ({ value: key, label: LABEL_MODE_LABELS[key], title: LABEL_MODE_HINTS[key] }))} />
              <Button size="small" icon={<RobotOutlined />} disabled={Boolean(running)} onClick={() => void labelSelected()}>AI 标注</Button>
            </span>
          )}
          <Button size="small" onClick={() => void bulk("enable")}>启用</Button>
          <Button size="small" icon={<StopOutlined />} onClick={() => void bulk("disable")}>停用</Button>
          <Button size="small" icon={<PushpinOutlined />} onClick={() => void bulk("pin")}>置顶</Button>
          <Dropdown
            menu={{
              items: [
                { key: "add_tags", label: "加标签…" },
                { key: "remove_tags", label: "去掉标签…" },
                { key: "set_groups", label: "设置适用群…" },
                { key: "add_pack", label: "放进合集…" },
                { key: "remove_pack", label: "移出合集…" },
                { key: "set_weight", label: "多用 / 少用…" },
                { type: "divider" },
                { key: "unpin", label: "取消置顶" },
                { key: "hold", label: "退回待审核" },
                { key: "undo_label", label: "撤销 AI 标注", icon: <RollbackOutlined /> },
                { key: "export", label: "导出选中的", icon: <DownloadOutlined /> },
                { type: "divider" },
                { key: "purge", label: "彻底删除（不进回收站）", danger: true },
                { key: "block", label: "彻底删除并拉黑", danger: true },
              ],
              onClick: ({ key }) => {
                if (key === "export") void exportNow("selected");
                else if (key === "unpin" || key === "hold" || key === "undo_label") void bulk(key);
                else if (key === "purge") confirmBulk(`彻底删除 ${ids.length} 张？`, "图片文件也会一起删掉，不进回收站，不能恢复。", "purge");
                else if (key === "block") confirmBulk(`删除并拉黑 ${ids.length} 张？`, "以后群友收藏、Bot 偷图都不会再收这些图；可以在回收站的拉黑名单里解除。", "block");
                else setPrompt({ action: key as NonNullable<BulkPrompt>["action"] });
              },
            }}
          >
            <Button size="small">更多 <DownOutlined /></Button>
          </Dropdown>
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() =>
              modal.confirm({
                title: `删除 ${ids.length} 张表情包？`,
                content: trashDays > 0 ? `会先放进回收站，${trashDays} 天内可以恢复。` : "图片文件也会一起删掉，不能恢复。",
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
          description={overview.stickers.length === 0 ? "库里还没有表情包：上传几张（AI 会帮你起名字），或者在群里让 Bot“收藏这个表情”，Bot 也会自己偷群友的表情包" : "没有符合条件的表情包"}
        />
      ) : (
        <>
          <div className={`stk-grid is-${density}`}>
            {visible.map((sticker) => (
              <StickerCard
                key={sticker.id}
                sticker={sticker}
                selected={selected.has(sticker.id)}
                onOpen={() => setParam({ "stk.edit": sticker.id })}
                onPin={() => void togglePin(sticker)}
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
          {rows.length > visible.length && (
            <div className="stk-more">
              <Button onClick={() => setLimit(limit + PAGE)}>再显示 {Math.min(PAGE, rows.length - visible.length)} 张（还有 {rows.length - visible.length} 张）</Button>
            </div>
          )}
        </>
      )}
      <EditModal
        sticker={editingSticker}
        overview={overview}
        position={{ index: editingIndex, total: editingIndex >= 0 ? rows.length : 0 }}
        onStep={(delta) => {
          const next = rows[editingIndex + delta];
          if (next) setParam({ "stk.edit": next.id });
        }}
        onClose={() => setParam({ "stk.edit": null })}
        onChanged={onChanged}
      />
      <BulkPromptModal prompt={prompt} count={ids.length} overview={overview} onClose={() => setPrompt(null)} onSubmit={submitPrompt} />
      <TagManager open={tagging} overview={overview} onClose={() => setTagging(false)} onChanged={onChanged} />
      <ImportModal open={importing} onClose={() => setImporting(false)} onImported={onChanged} />
      <UploadModal open={uploading} maxBytes={overview.settings.maxBytes} aiReady={overview.vision.ready} aiEnabled={overview.settings.aiLabelEnabled && overview.settings.aiLabelOnUpload} packs={overview.packs} onClose={() => setUploading(false)} onUploaded={onChanged} />
    </div>
  );
}
