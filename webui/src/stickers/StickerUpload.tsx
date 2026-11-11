// 上传表情包（AI 标注版）：选图后自动让识图模型看一遍，把建议的名字/标签填进还空着的格子；
// 名字可以留空，上传时由后端让 AI 补（识图不可用时退回文件名）。库里已有的同一张图会提前标出来。
import { CloudUploadOutlined, PictureOutlined, RobotOutlined } from "@ant-design/icons";
import { Alert, Button, Input, Modal, Select, Switch, Tag, Tooltip } from "antd";
import { useEffect, useRef, useState } from "react";
import { previewLabel, uploadSticker } from "./stickers-api";
import { applySuggestion, cleanName, confidenceText, formatBytes, looksLikePlaceholder, nameFromFile, parseTags } from "./stickers-meta";
import type { StickerLabel, StickerPack } from "./types";

type ItemStatus = "waiting" | "uploading" | "done" | "duplicate" | "failed";
type LabelState = "idle" | "labeling" | "ready" | "failed";

export interface UploadItem {
  key: string;
  file: File;
  name: string;
  tags: string;
  status: ItemStatus;
  error?: string;
  preview: string;
  labelState: LabelState;
  label?: StickerLabel;
  labelError?: string;
  duplicateOf?: string;
  /** 用户自己改过名字/标签：AI 建议不再覆盖。 */
  touched: boolean;
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

const LABEL_CONCURRENCY = 2;

export function UploadModal({
  open,
  onClose,
  onUploaded,
  maxBytes,
  aiReady,
  aiEnabled,
  packs = [],
}: {
  open: boolean;
  onClose: () => void;
  onUploaded: () => void;
  maxBytes: number;
  aiReady: boolean;
  aiEnabled: boolean;
  packs?: StickerPack[];
}): React.JSX.Element {
  const [pack, setPack] = useState<string | undefined>(undefined);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [autoLabel, setAutoLabel] = useState(aiReady && aiEnabled);
  const inputRef = useRef<HTMLInputElement>(null);
  const itemsRef = useRef<UploadItem[]>([]);
  itemsRef.current = items;
  const canLabel = aiReady && aiEnabled;
  useEffect(() => {
    if (!open) {
      setItems((current) => {
        for (const item of current) URL.revokeObjectURL(item.preview);
        return [];
      });
    }
  }, [open]);
  const patch = (key: string, change: Partial<UploadItem>) => setItems((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));

  const label = async (targets: UploadItem[]) => {
    const queue = [...targets];
    const worker = async () => {
      for (let next = queue.shift(); next; next = queue.shift()) {
        const target = next;
        patch(target.key, { labelState: "labeling", labelError: undefined });
        try {
          const result = await previewLabel(target.file, target.file.name);
          const latest = itemsRef.current.find((item) => item.key === target.key);
          if (!latest) continue;
          const change: Partial<UploadItem> = { labelState: result.label.ok ? "ready" : "failed", label: result.label };
          if (!result.label.ok) change.labelError = result.label.reasonText ?? "识别失败";
          if (result.duplicate) change.duplicateOf = result.duplicate.name;
          if (result.label.ok && !latest.touched) Object.assign(change, applySuggestion(latest, result.label, looksLikePlaceholder(latest.name)));
          patch(target.key, change);
        } catch (error) {
          patch(target.key, { labelState: "failed", labelError: errorText(error, "识别失败") });
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(LABEL_CONCURRENCY, queue.length) }, worker));
  };

  const addFiles = (files: FileList | File[]) => {
    const next: UploadItem[] = [...files]
      .filter((file) => file.type.startsWith("image/") || /\.(png|jpe?g|gif|webp)$/i.test(file.name))
      .map((file, index) => ({
        key: `${Date.now()}-${index}-${file.name}`,
        file,
        name: nameFromFile(file.name),
        tags: "",
        status: file.size > maxBytes ? ("failed" as const) : ("waiting" as const),
        error: file.size > maxBytes ? `超过 ${formatBytes(maxBytes)}` : undefined,
        preview: URL.createObjectURL(file),
        labelState: "idle" as const,
        touched: false,
      }));
    setItems((current) => [...current, ...next]);
    if (canLabel && autoLabel) void label(next.filter((item) => item.status === "waiting"));
  };

  const pending = items.filter((item) => item.status === "waiting");
  // 名字留空也能上传：后端让 AI 起名（识图不可用时用文件名）
  const ready = pending.length > 0 && (canLabel || pending.every((item) => cleanName(item.name)));
  const labeling = items.some((item) => item.labelState === "labeling");
  const uploadAll = async () => {
    setBusy(true);
    let uploaded = 0;
    for (const item of pending) {
      patch(item.key, { status: "uploading" });
      try {
        const result = await uploadSticker(item.file, {
          name: cleanName(item.name),
          tags: parseTags(item.tags),
          auto: canLabel,
          fallback: nameFromFile(item.file.name) || item.file.name.slice(0, 16),
          pack,
        });
        patch(item.key, {
          status: result.created ? "done" : "duplicate",
          name: result.sticker.name,
          tags: result.sticker.tags.join("，"),
          error: result.heldForReview ? "AI 觉得不太合适，已转待审核" : undefined,
        });
        if (result.created) uploaded += 1;
      } catch (error) {
        patch(item.key, { status: "failed", error: errorText(error, "上传失败") });
      }
    }
    setBusy(false);
    if (uploaded > 0) onUploaded();
  };
  const unlabeled = pending.filter((item) => item.labelState === "idle" || item.labelState === "failed");
  return (
    <Modal
      open={open}
      title="上传表情包"
      onCancel={onClose}
      width={720}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        canLabel ? (
          <Button key="label" icon={<RobotOutlined />} disabled={unlabeled.length === 0 || labeling} onClick={() => void label(unlabeled)}>
            AI 识别{unlabeled.length ? ` ${unlabeled.length} 张` : ""}
          </Button>
        ) : null,
        <Button key="upload" type="primary" icon={<CloudUploadOutlined />} loading={busy} disabled={!ready} onClick={() => void uploadAll()}>上传 {pending.length || ""} 张</Button>,
      ]}
    >
      {!aiReady && (
        <Alert className="stk-ai-alert" type="info" showIcon message="没有配置能看图的模型，名字要自己填" description="在「环境配置」里填 AI_VISION_MODEL（或用能看图的对话模型）后，上传时就能让 AI 自动起名、打标签。" />
      )}
      {aiReady && !aiEnabled && <Alert className="stk-ai-alert" type="info" showIcon message="AI 标注在「策略与频率」里关掉了，名字要自己填" />}
      {canLabel && (
        <div className="sbx-switch-row stk-ai-toggle">
          <Switch checked={autoLabel} onChange={setAutoLabel} aria-label="选图后自动 AI 识别" />
          <div>
            <strong>选图后自动 AI 识别</strong>
            <span className="sbx-muted">识图模型看图起名字、打标签，填进还空着的格子；你改过的不会被覆盖。名字留空时上传会由 AI 补。</span>
          </div>
        </div>
      )}
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
        onPaste={(event) => {
          const files = [...event.clipboardData.files];
          if (files.length) addFiles(files);
        }}
      >
        <PictureOutlined />
        <span>点击、拖进来或粘贴图片（PNG / JPG / GIF / WebP，单张不超过 {formatBytes(maxBytes)}）</span>
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
      {packs.length > 0 && (
        <label className="sbx-field">
          <span className="sbx-field-label">这批直接放进合集（可不选）</span>
          <Select allowClear value={pack} onChange={(value: string | undefined) => setPack(value)} options={packs.map((item) => ({ value: item.id, label: item.name }))} placeholder="不放进合集" aria-label="上传到合集" />
        </label>
      )}
      {items.length > 0 && (
        <ul className="stk-upload-list" aria-label="待上传">
          {items.map((item) => (
            <li key={item.key} className={`is-${item.status}`}>
              <img src={item.preview} alt="" />
              <div className="stk-upload-fields">
                <Input
                  size="small"
                  value={item.name}
                  maxLength={16}
                  placeholder={canLabel ? "名字（留空让 AI 起）" : "名字（必填）"}
                  disabled={item.status !== "waiting"}
                  status={item.status === "waiting" && !canLabel && !cleanName(item.name) ? "error" : undefined}
                  onChange={(event) => patch(item.key, { name: event.target.value, touched: true })}
                  aria-label="名字"
                />
                <Input
                  size="small"
                  value={item.tags}
                  placeholder={canLabel ? "标签（留空让 AI 打）" : "标签，逗号分隔"}
                  disabled={item.status !== "waiting"}
                  onChange={(event) => patch(item.key, { tags: event.target.value, touched: true })}
                  aria-label="标签"
                />
                <LabelHint item={item} />
              </div>
              <span className="stk-upload-state">
                {item.status === "done" && <Tag color="green">已上传</Tag>}
                {item.status === "duplicate" && <Tag>库里已有</Tag>}
                {item.status === "uploading" && <Tag color="blue">上传中</Tag>}
                {item.status === "failed" && <Tooltip title={item.error}><Tag color="red">失败</Tag></Tooltip>}
                {item.status === "done" && item.error && <Tooltip title={item.error}><Tag color="gold">待审核</Tag></Tooltip>}
                {item.status === "waiting" && (
                  <Button
                    size="small"
                    type="text"
                    onClick={() =>
                      setItems((current) => {
                        URL.revokeObjectURL(item.preview);
                        return current.filter((row) => row.key !== item.key);
                      })
                    }
                  >
                    移除
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function LabelHint({ item }: { item: UploadItem }): React.JSX.Element | null {
  if (item.duplicateOf && item.status === "waiting") return <span className="stk-label-hint is-warn">库里已有这张（{item.duplicateOf}），上传会跳过</span>;
  if (item.labelState === "labeling") return <span className="stk-label-hint"><RobotOutlined spin /> AI 识别中…</span>;
  if (item.labelState === "failed") return <span className="stk-label-hint is-warn">AI 没认出来：{item.labelError}</span>;
  if (item.labelState === "ready" && item.label) {
    const label = item.label;
    return (
      <span className="stk-label-hint">
        <RobotOutlined /> AI：{label.name}
        {label.tags.length ? ` · ${label.tags.join("、")}` : ""}
        {label.text ? ` · 图上写着“${label.text.slice(0, 20)}”` : ""}
        {` · ${confidenceText(label.confidence)}`}
        {!label.safe && <Tag color="red" className="stk-inline-tag">可能不合适</Tag>}
      </span>
    );
  }
  return null;
}
