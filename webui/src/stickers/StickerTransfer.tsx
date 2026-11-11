// 导入一个导出的表情包 zip：先看包里有什么，再决定怎么导。
import { ImportOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Checkbox, Modal } from "antd";
import { useEffect, useRef, useState } from "react";
import { importStickers } from "./stickers-api";
import { formatBytes } from "./stickers-meta";
import type { ArchiveInfo, ImportResult } from "./types";

export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ImportModal({ open, onClose, onImported }: { open: boolean; onClose: () => void; onImported: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [info, setInfo] = useState<ArchiveInfo | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [updateExisting, setUpdateExisting] = useState(false);
  const [asPending, setAsPending] = useState(false);
  const [keepPrivate, setKeepPrivate] = useState(false);
  useEffect(() => {
    if (!open) return;
    setFile(null);
    setInfo(null);
    setResult(null);
    setUpdateExisting(false);
    setAsPending(false);
    setKeepPrivate(false);
  }, [open]);
  const inspect = async (picked: File) => {
    setFile(picked);
    setInfo(null);
    setResult(null);
    setBusy(true);
    try {
      const response = await importStickers(picked, { apply: false });
      setInfo(response.info ?? null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "这个包读不了");
      setFile(null);
    } finally {
      setBusy(false);
    }
  };
  const apply = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const response = await importStickers(file, { apply: true, updateExisting, asPending, keepPrivate });
      setResult(response.result ?? null);
      onImported();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "导入失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      title="导入表情包"
      onCancel={onClose}
      width={520}
      footer={
        result
          ? <Button type="primary" onClick={onClose}>完成</Button>
          : [
              <Button key="cancel" onClick={onClose}>取消</Button>,
              <Button key="apply" type="primary" icon={<ImportOutlined />} disabled={!info || info.count === 0} loading={busy && Boolean(info)} onClick={() => void apply()}>导入</Button>,
            ]
      }
    >
      <p className="sbx-muted">选一个在这里（或另一个 YawnBot）“导出”得到的 zip。图片、名字、别名、标签、说明和合集都会带过来；同一张图库里已经有的不会重复。</p>
      <input
        ref={input}
        type="file"
        accept=".zip,application/zip"
        hidden
        aria-label="选择导出的包"
        onChange={(event) => {
          const picked = event.target.files?.[0];
          event.target.value = "";
          if (picked) void inspect(picked);
        }}
      />
      <Button onClick={() => input.current?.click()} loading={busy && !info}>{file ? `换一个包（现在是 ${file.name}，${formatBytes(file.size)}）` : "选择 zip 文件"}</Button>
      {info && !result && (
        <div className="stk-import-info" aria-label="包里的内容">
          <strong>包里有 {info.count} 张：{info.new} 张是新的，{info.existing} 张库里已经有</strong>
          {info.packs.length > 0 && <span className="sbx-muted">合集：{info.packs.join("、")}</span>}
          {info.exportedAt && <span className="sbx-muted">导出时间：{info.exportedAt}</span>}
          <Checkbox checked={asPending} onChange={(event) => setAsPending(event.target.checked)}>新的先进待审核，看过再用</Checkbox>
          <Checkbox checked={updateExisting} disabled={info.existing === 0} onChange={(event) => setUpdateExisting(event.target.checked)}>库里已有的，用包里的名字、标签、说明覆盖</Checkbox>
          {info.includePrivate && <Checkbox checked={keepPrivate} onChange={(event) => setKeepPrivate(event.target.checked)}>保留来源信息（来源群、谁收藏的、只在哪些群用）</Checkbox>}
        </div>
      )}
      {result && (
        <div className="stk-import-info" aria-label="导入结果">
          <Alert type={result.failedCount ? "warning" : "success"} showIcon message={`新加 ${result.added} 张，更新 ${result.updated} 张，跳过 ${result.skipped} 张${result.packsCreated ? `，新建 ${result.packsCreated} 个合集` : ""}`} />
          {result.failed.length > 0 && (
            <ul className="stk-mini-list">
              {result.failed.slice(0, 8).map((row, index) => <li key={`${row.name}-${index}`}>{row.name || "（没名字）"}：{row.reason}</li>)}
              {result.failedCount > 8 && <li>…还有 {result.failedCount - 8} 张没导进来</li>}
            </ul>
          )}
        </div>
      )}
    </Modal>
  );
}
