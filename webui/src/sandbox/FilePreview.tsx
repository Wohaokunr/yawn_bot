// 工作区文件预览：图片（可放大）、音视频、文本（行号/JSON 美化/CSV 表格），←/→ 切换上一个/下一个。
import { CodeOutlined, CopyOutlined, DownloadOutlined, LeftOutlined, RightOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Drawer, Empty, Segmented, Skeleton, Space, Tag, Tooltip } from "antd";
import { useEffect, useMemo, useState } from "react";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { sandboxApi, sandboxFileUrl } from "./sandbox-api";
import {
  canPreviewText,
  fileKindMeta,
  formatBytes,
  formatEpoch,
  inlineMediaKind,
  isInboxPath,
  parseCsvPreview,
  prettyJson,
} from "./sandbox-meta";
import type { SandboxFileEntry } from "./types";

type TextMode = "raw" | "lines" | "table" | "json";

function availableModes(path: string): TextMode[] {
  const lower = path.toLowerCase();
  if (/\.(csv|tsv)$/.test(lower)) return ["table", "raw", "lines"];
  if (/\.(json|geojson|ipynb)$/.test(lower)) return ["json", "raw", "lines"];
  return ["lines", "raw"];
}

const MODE_LABELS: Record<TextMode, string> = { raw: "原文", lines: "行号", table: "表格", json: "美化" };

export function CsvTable({ text, delimiter }: { text: string; delimiter: string }): React.JSX.Element {
  const rows = useMemo(() => parseCsvPreview(text, 200, delimiter), [text, delimiter]);
  if (!rows.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空表格" />;
  const [head, ...body] = rows;
  return (
    <div className="sbx-csv">
      <table>
        <thead>
          <tr>
            <th aria-label="行号">#</th>
            {head.map((cell, index) => <th key={index}>{cell}</th>)}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={rowIndex}>
              <td className="sbx-csv-index">{rowIndex + 1}</td>
              {head.map((_, index) => <td key={index}>{row[index] ?? ""}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length >= 200 && <p className="sbx-muted">只显示前 200 行。</p>}
    </div>
  );
}

export function NumberedText({ text }: { text: string }): React.JSX.Element {
  const lines = text.split("\n");
  return (
    <pre className="sbx-preview-text sbx-numbered">
      {lines.map((line, index) => (
        <span key={index} className="sbx-line">
          <span className="sbx-line-no" aria-hidden="true">{index + 1}</span>
          {line || " "}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

export function FilePreviewDrawer({
  workspace,
  file,
  siblings,
  onNavigate,
  onClose,
  onOpenInConsole,
}: {
  workspace: string;
  file: SandboxFileEntry | null;
  siblings: SandboxFileEntry[];
  onNavigate: (file: SandboxFileEntry) => void;
  onClose: () => void;
  onOpenInConsole?: (file: SandboxFileEntry) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const textual = file ? canPreviewText(file.path, file.kind) : false;
  const media = file ? inlineMediaKind(file.path, file.kind) : null;
  const modes = file ? availableModes(file.path) : ["lines" as TextMode];
  const [mode, setMode] = useState<TextMode>(modes[0]);
  const [zoomed, setZoomed] = useState(false);
  useEffect(() => {
    setMode(modes[0]);
    setZoomed(false);
  }, [file?.path]);

  const query = useApiQuery<{ text: string; truncated: boolean } | null>({
    queryKey: ["sandbox-preview", workspace, file?.path, file?.mtime, textual],
    fetcher: (signal) => (file && textual ? sandboxApi.preview(workspace, file.path, signal) : Promise.resolve(null)),
    keepPreviousData: false,
  });

  const index = file ? siblings.findIndex((item) => item.path === file.path) : -1;
  const previous = index > 0 ? siblings[index - 1] : null;
  const next = index >= 0 && index < siblings.length - 1 ? siblings[index + 1] : null;

  useEffect(() => {
    if (!file) return undefined;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      if (event.key === "ArrowLeft" && previous) onNavigate(previous);
      if (event.key === "ArrowRight" && next) onNavigate(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [file, previous, next, onNavigate]);

  const text = query.data?.text ?? "";
  const shown = mode === "json" ? prettyJson(text) : text;
  const delimiter = file?.path.toLowerCase().endsWith(".tsv") ? "\t" : ",";

  return (
    <Drawer
      open={!!file}
      onClose={onClose}
      width="min(880px, 100%)"
      title={file ? (
        <span className="sbx-preview-title">
          <span aria-hidden="true">{fileKindMeta(file.kind).emoji}</span>
          <span className="sbx-preview-path">{file.path}</span>
          {isInboxPath(file.path) && <Tag color="magenta">来自群聊</Tag>}
        </span>
      ) : "预览"}
      extra={file && (
        <Space size={4} wrap>
          <Tooltip title="上一个（←）"><Button icon={<LeftOutlined />} disabled={!previous} onClick={() => previous && onNavigate(previous)} aria-label="上一个" /></Tooltip>
          <Tooltip title="下一个（→）"><Button icon={<RightOutlined />} disabled={!next} onClick={() => next && onNavigate(next)} aria-label="下一个" /></Tooltip>
          {onOpenInConsole && <Button icon={<CodeOutlined />} onClick={() => onOpenInConsole(file)}>在控制台打开</Button>}
          <Button icon={<DownloadOutlined />} href={sandboxFileUrl(workspace, file.path, true)}>下载</Button>
        </Space>
      )}
    >
      {file && (
        <div className="sbx-preview">
          <p className="sbx-muted">
            {formatBytes(file.size)} · {file.mime} · 修改于 {formatEpoch(file.mtime)}
            {index >= 0 && siblings.length > 1 ? ` · 第 ${index + 1} / ${siblings.length} 个` : ""}
          </p>
          {media === "image" && (
            <button type="button" className={`sbx-zoom ${zoomed ? "is-zoomed" : ""}`} onClick={() => setZoomed(!zoomed)} aria-label={zoomed ? "缩小到适合宽度" : "查看原始大小"}>
              <img src={sandboxFileUrl(workspace, file.path)} alt={file.path} />
            </button>
          )}
          {media === "audio" && <audio controls src={sandboxFileUrl(workspace, file.path)} />}
          {media === "video" && <video controls src={sandboxFileUrl(workspace, file.path)} />}
          {textual && (query.error
            ? <QueryErrorAlert error={query.error} onRetry={query.reload} />
            : query.data
              ? <>
                <div className="sbx-toolbar">
                  <Segmented size="small" value={mode} onChange={(value) => setMode(value as TextMode)} options={modes.map((value) => ({ value, label: MODE_LABELS[value] }))} />
                  <span className="sbx-muted">{text.length.toLocaleString()} 字 · {text.split("\n").length.toLocaleString()} 行</span>
                  <span className="sbx-toolbar-spacer" />
                  <Button
                    size="small"
                    icon={<CopyOutlined />}
                    onClick={() => void navigator.clipboard?.writeText(shown).then(() => message.success("已复制内容"))}
                  >复制</Button>
                </div>
                {mode === "table" && <CsvTable text={text} delimiter={delimiter} />}
                {mode === "lines" && <NumberedText text={shown} />}
                {(mode === "raw" || mode === "json") && <pre className="sbx-preview-text">{shown}</pre>}
                {query.data.truncated && <p className="sbx-muted">只显示前 200KB，完整内容请下载。</p>}
              </>
              : <Skeleton active />)}
          {!media && !textual && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={onOpenInConsole ? "这种文件不能在页面里预览：可以下载，或在控制台用 Python 打开" : "这种文件不能在页面里预览，请下载查看"}
            />
          )}
        </div>
      )}
    </Drawer>
  );
}
