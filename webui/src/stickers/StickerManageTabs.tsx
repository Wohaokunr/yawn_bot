// 体检、合集、回收站三个标签页。
import { DeleteOutlined, EditOutlined, MedicineBoxOutlined, PlusOutlined, ReloadOutlined, RollbackOutlined, ToolOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Input, Modal, Popconfirm, Progress, Skeleton, Switch, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { parseGroupIds } from "../web-search/web-search-meta";
import { saveBlob } from "./StickerTransfer";
import { exportStickers, stickerImageUrl, stickersApi } from "./stickers-api";
import { cleanName, formatBytes, healthTone, SEVERITY_COLORS, SEVERITY_LABELS, SOURCE_LABELS, trashDaysLeft } from "./stickers-meta";
import type { HealthIssue, Sticker, StickerHealth, StickerOverview, StickerPack, StickerTrash } from "./types";

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

// ── 体检 ──

/** 这类问题点“去看看”时，库里该怎么筛。 */
const ISSUE_FILTERS: Record<string, Record<string, string | null>> = {
  missing_file: { "stk.flag": "missing" },
  unsafe_active: { "stk.ai": "unsafe" },
  never_used: { "stk.flag": "never", "stk.sort": "oldest" },
  heavy: { "stk.sort": "size" },
  weight_zero_unnamed: { "stk.flag": "silent" },
  placeholder_name: { "stk.ai": "unlabeled" },
  no_tags: { "stk.ai": "unlabeled" },
};

function IssueRow({ issue, overview, onFix, fixing }: { issue: HealthIssue; overview: StickerOverview; onFix: () => void; fixing: boolean }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const byId = new Map(overview.stickers.map((item) => [item.id, item]));
  const open = (id: string) => setSearchParams(updateSearchParams(searchParams, { "stk.tab": null, "stk.edit": id, "stk.f": "all" }), { replace: true });
  const jump = ISSUE_FILTERS[issue.kind];
  return (
    <li className={`stk-issue is-${issue.severity}`}>
      <div className="stk-issue-head">
        <Tag color={SEVERITY_COLORS[issue.severity]}>{SEVERITY_LABELS[issue.severity]}</Tag>
        <strong>{issue.title}</strong>
        <span className="sbx-muted">{issue.count} {issue.kind === "orphan_file" ? "个文件" : issue.kind === "index_corrupt" ? "次" : "张"}</span>
        <span className="sbx-toolbar-spacer" />
        {jump && <Button size="small" type="link" onClick={() => setSearchParams(updateSearchParams(searchParams, { "stk.tab": null, "stk.f": "all", ...jump }), { replace: true })}>在库里筛出来</Button>}
        {issue.fixable && (
          <Popconfirm title={issue.kind === "orphan_file" ? "删掉这些没人引用的文件？" : issue.kind === "missing_file" ? "把这些放进回收站？" : "一键修好？"} onConfirm={onFix}>
            <Button size="small" icon={<ToolOutlined />} loading={fixing}>一键修</Button>
          </Popconfirm>
        )}
      </div>
      <span className="sbx-muted">{issue.advice}</span>
      {issue.ids.length > 0 && (
        <div className="stk-chiprow">
          {issue.ids.slice(0, 16).map((id) => <Tag key={id} className="stk-chip" onClick={() => open(id)}>{byId.get(id)?.name ?? id}</Tag>)}
          {issue.count > 16 && <span className="sbx-muted">…等 {issue.count} 张</span>}
        </div>
      )}
      {issue.files && issue.files.length > 0 && <span className="sbx-muted">{issue.files.slice(0, 6).join("、")}{issue.files.length > 6 ? " …" : ""}</span>}
    </li>
  );
}

export function HealthTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<StickerHealth>({ queryKey: ["stickers-health"], fetcher: (signal) => stickersApi.health(signal), invalidation: { resources: ["stickers"] } });
  const [fixing, setFixing] = useState<string | null>(null);
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />;
  const tone = healthTone(data.score);
  const fix = async (kind: string) => {
    setFixing(kind);
    try {
      const result = await stickersApi.fixHealth(kind);
      message.success(result.fixed > 0 ? `修好了 ${result.fixed} 处` : "没有需要修的");
      query.reload();
      onChanged();
    } catch (error) {
      message.error(errorText(error, "没修成"));
    } finally {
      setFixing(null);
    }
  };
  const counts = { error: 0, warn: 0, info: 0 };
  for (const issue of data.issues) counts[issue.severity] += 1;
  return (
    <div className="stk-health">
      <Card size="small">
        <div className="stk-health-head">
          <Progress type="circle" size={84} percent={data.score} status={tone === "bad" ? "exception" : tone === "ok" ? "success" : "normal"} format={(value) => `${value ?? 0}`} aria-label={`体检得分 ${data.score}`} />
          <div>
            <strong>{tone === "ok" ? "库的状态不错" : tone === "warn" ? "有些地方可以收拾一下" : "有需要马上处理的问题"}</strong>
            <span className="sbx-muted">
              查了 {data.total} 张（{formatBytes(data.bytes)}），回收站里 {data.trash.count} 张（{formatBytes(data.trash.bytes)}）。
              {counts.error ? ` ${counts.error} 类要处理` : ""}{counts.warn ? ` ${counts.warn} 类建议处理` : ""}{counts.info ? ` ${counts.info} 类提示` : ""}
            </span>
            <span className="sbx-muted">体检只看库本身：文件在不在、名字标签全不全、有没有用过。不看聊天内容。{data.checkedAt} 查的。</span>
          </div>
          <span className="sbx-toolbar-spacer" />
          <Button icon={<ReloadOutlined />} loading={query.refreshing} onClick={query.reload}>重新体检</Button>
        </div>
      </Card>
      {data.issues.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没发现问题" />
      ) : (
        <ul className="stk-issue-list" aria-label="体检结果">
          {data.issues.map((issue) => <IssueRow key={issue.kind} issue={issue} overview={overview} fixing={fixing === issue.kind} onFix={() => void fix(issue.kind)} />)}
        </ul>
      )}
    </div>
  );
}

// ── 合集 ──

function PackEditor({ pack, open, onClose, onSaved }: { pack: StickerPack | null; open: boolean; onClose: () => void; onSaved: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [groups, setGroups] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setName(pack?.name ?? "");
    setDescription(pack?.description ?? "");
    setGroups(pack?.groups.join("\n") ?? "");
  }, [open, pack]);
  const parsed = parseGroupIds(groups);
  const save = async () => {
    setBusy(true);
    try {
      const body = { name: cleanName(name), description, groups: parsed.ids };
      if (pack) await stickersApi.updatePack(pack.id, body);
      else await stickersApi.createPack(body);
      message.success(pack ? "合集已保存" : "合集建好了，到表情包库里选图放进来");
      onSaved();
      onClose();
    } catch (error) {
      message.error(errorText(error, "保存失败"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} title={pack ? `编辑合集 · ${pack.name}` : "新建合集"} onCancel={onClose} okText="保存" okButtonProps={{ disabled: !cleanName(name) || parsed.invalid.length > 0, loading: busy }} onOk={() => void save()} width={460}>
      <label className="sbx-field">
        <span className="sbx-field-label">合集名字</span>
        <Input value={name} maxLength={16} showCount onChange={(event) => setName(event.target.value)} placeholder="如 猫猫、本群梗图" aria-label="合集名字" />
      </label>
      <label className="sbx-field">
        <span className="sbx-field-label">说明</span>
        <Input value={description} maxLength={60} onChange={(event) => setDescription(event.target.value)} placeholder="给管理员自己看的" aria-label="合集说明" />
      </label>
      <label className="sbx-field">
        <span className="sbx-field-label">只在这些群用（留空 = 所有群）</span>
        <Input.TextArea value={groups} autoSize={{ minRows: 2, maxRows: 5 }} onChange={(event) => setGroups(event.target.value)} placeholder="每行一个群号" status={parsed.invalid.length ? "error" : undefined} aria-label="合集适用群" />
        <span className="sbx-muted">合集里的图只在这些群出现；一张图自己也限定了群时，两边都要满足。</span>
      </label>
    </Modal>
  );
}

export function PacksTab({ overview, onChanged }: { overview: StickerOverview; onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const [editing, setEditing] = useState<StickerPack | null>(null);
  const [creating, setCreating] = useState(false);
  const byId = new Map(overview.stickers.map((item) => [item.id, item]));
  const run = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      message.success(done);
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  const exportPack = async (pack: StickerPack) => {
    try {
      const result = await exportStickers({ pack: pack.id });
      saveBlob(result.blob, `yawnbot-stickers-${pack.name}.zip`);
      message.success(`导出了 ${result.count} 张`);
    } catch (error) {
      message.error(errorText(error, "导出失败"));
    }
  };
  const unpacked = overview.stickers.filter((item) => item.packs.length === 0).length;
  return (
    <div className="stk-packs">
      <div className="web-toolbar">
        <span className="sbx-muted">合集把表情包分成几套：可以整套只给某些群用、整套停用、整套导出。一张图可以在好几个合集里；{unpacked} 张不在任何合集。</span>
        <span className="sbx-toolbar-spacer" />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>新建合集</Button>
      </div>
      {overview.packs.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有合集。建一个，再到表情包库里多选几张“放进合集”" />
      ) : (
        <div className="stk-pack-grid">
          {overview.packs.map((pack) => (
            <Card key={pack.id} size="small" className={pack.enabled ? "" : "is-off"}>
              <div className="stk-pack-cover" aria-hidden="true">
                {pack.cover.length === 0 ? <span className="sbx-muted">空的</span> : pack.cover.map((id) => (byId.get(id) && !byId.get(id)?.fileMissing ? <img key={id} src={stickerImageUrl(byId.get(id)!)} alt="" loading="lazy" /> : <span key={id} className="stk-rank-blank" />))}
              </div>
              <div className="stk-pack-meta">
                <strong>{pack.name}</strong>
                {!pack.enabled && <Tag>已停用</Tag>}
                <span className="sbx-muted">{pack.count} 张 · 发过 {pack.uses} 次</span>
              </div>
              {pack.description && <span className="sbx-muted">{pack.description}</span>}
              <span className="sbx-muted">{pack.groups.length === 0 ? "所有群都能用" : `只在 ${pack.groups.map((group) => overview.groupNames[group] ?? group).slice(0, 3).join("、")}${pack.groups.length > 3 ? ` 等 ${pack.groups.length} 个群` : ""}用`}</span>
              <div className="stk-actions">
                <Tooltip title={pack.enabled ? "停用后，只属于这个合集的图整套不发" : "启用这一套"}>
                  <Switch size="small" checked={pack.enabled} onChange={(value) => void run(() => stickersApi.updatePack(pack.id, { enabled: value }), value ? "已启用" : "已停用")} aria-label={`启用合集 ${pack.name}`} />
                </Tooltip>
                <Button size="small" onClick={() => setSearchParams(updateSearchParams(searchParams, { "stk.tab": null, "stk.pack": pack.id, "stk.f": "all" }), { replace: true })}>看里面的图</Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => setEditing(pack)} aria-label={`编辑合集 ${pack.name}`} />
                <Button size="small" disabled={pack.count === 0} onClick={() => void exportPack(pack)}>导出</Button>
                <Popconfirm title={`删除合集“${pack.name}”？`} description="里面的表情包留在库里，只是不再属于它。" onConfirm={() => void run(() => stickersApi.deletePack(pack.id), "合集已删除")}>
                  <Button size="small" danger icon={<DeleteOutlined />} aria-label={`删除合集 ${pack.name}`} />
                </Popconfirm>
              </div>
            </Card>
          ))}
        </div>
      )}
      <PackEditor pack={editing} open={creating || editing !== null} onClose={() => { setCreating(false); setEditing(null); }} onSaved={onChanged} />
    </div>
  );
}

// ── 回收站 ──

function TrashCard({ sticker, trashDays, onRestore, onPurge }: { sticker: Sticker; trashDays: number; onRestore: () => void; onPurge: (block: boolean) => void }): React.JSX.Element {
  const left = trashDaysLeft(sticker.deletedAt, trashDays);
  return (
    <div className="stk-card is-disabled">
      <div className="stk-thumb">{sticker.fileMissing ? <span className="stk-missing">文件丢失</span> : <img src={stickerImageUrl(sticker)} alt={sticker.name} loading="lazy" />}</div>
      <div className="stk-meta">
        <strong title={sticker.name}>{sticker.name}</strong>
        <span className="sbx-muted">{SOURCE_LABELS[sticker.source]} · 发过 {sticker.uses} 次</span>
        <span className="sbx-muted">{sticker.deletedAt?.slice(0, 16)} 删的{left !== null ? ` · ${left === 0 ? "今天清掉" : `${left} 天后清掉`}` : ""}</span>
        <div className="stk-actions">
          <Button size="small" icon={<RollbackOutlined />} onClick={onRestore} aria-label={`恢复 ${sticker.name}`}>恢复</Button>
          <Popconfirm title="彻底删除？" description="图片文件也会删掉，不能恢复。" onConfirm={() => onPurge(false)}>
            <Button size="small" danger aria-label={`彻底删除 ${sticker.name}`}>删掉</Button>
          </Popconfirm>
          <Popconfirm title="删除并拉黑？" description="以后收藏、偷图都不再收这张。" onConfirm={() => onPurge(true)}>
            <Button size="small" type="text" danger aria-label={`拉黑 ${sticker.name}`}>拉黑</Button>
          </Popconfirm>
        </div>
      </div>
    </div>
  );
}

export function TrashTab({ onChanged }: { onChanged: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const query = useApiQuery<StickerTrash>({ queryKey: ["stickers-trash"], fetcher: (signal) => stickersApi.trash(signal), invalidation: { resources: ["stickers"] } });
  const data = query.data;
  if (!data) return query.error ? <QueryErrorAlert error={query.error} onRetry={query.reload} /> : <Skeleton active />;
  const run = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      message.success(done);
      query.reload();
      onChanged();
    } catch (error) {
      message.error(errorText(error, "操作失败"));
    }
  };
  return (
    <div className="stk-trash">
      <div className="web-toolbar">
        <span className="sbx-muted">
          {data.trashDays > 0 ? `删掉的表情包在这里留 ${data.trashDays} 天，过期自动清掉（天数在“策略与频率”里改）。` : "现在删除是直接删、不进回收站（在“策略与频率”里可以打开）。"}
          回收站里的图不会发，群友收藏、Bot 偷图也不会把它们悄悄加回来。
        </span>
        <span className="sbx-toolbar-spacer" />
        <Popconfirm title={`清空回收站（${data.stickers.length} 张）？`} description="图片文件也会删掉，不能恢复。" onConfirm={() => void run(() => stickersApi.emptyTrash(), "回收站已清空")} disabled={data.stickers.length === 0}>
          <Button danger icon={<DeleteOutlined />} disabled={data.stickers.length === 0}>清空回收站</Button>
        </Popconfirm>
      </div>
      {data.autoPurged > 0 && <Alert type="info" showIcon closable message={`刚刚自动清掉了 ${data.autoPurged} 张放够天数的`} />}
      {data.stickers.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="回收站是空的" />
      ) : (
        <div className="stk-grid is-cozy">
          {data.stickers.map((sticker) => (
            <TrashCard
              key={sticker.id}
              sticker={sticker}
              trashDays={data.trashDays}
              onRestore={() => void run(() => stickersApi.restore(sticker.id), "已恢复到库里")}
              onPurge={(block) => void run(() => stickersApi.remove(sticker.id, block ? "block" : "purge"), block ? "已删除并拉黑" : "已彻底删除")}
            />
          ))}
        </div>
      )}
      <Card size="small" title={<span><MedicineBoxOutlined /> 拉黑的图（{data.blocked.length}）</span>}>
        {data.blocked.length === 0 ? (
          <span className="sbx-muted">没有拉黑的图。删除时选“删除并拉黑”，以后就不会再收到同一张。</span>
        ) : (
          <>
            <p className="sbx-muted">这里只存图片的指纹（内容哈希），不存图片本身。解除后，同一张图又可以被收藏或偷回来；管理员在后台上传同一张也会自动解除。</p>
            <ul className="stk-mini-list">
              {data.blocked.map((digest) => (
                <li key={digest}>
                  <code>{digest.slice(0, 16)}…</code>
                  <Button size="small" type="link" onClick={() => void run(() => stickersApi.unblock(digest), "已解除拉黑")} aria-label={`解除拉黑 ${digest.slice(0, 8)}`}>解除</Button>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </div>
  );
}
