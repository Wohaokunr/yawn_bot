import { CaretRightOutlined, ClearOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty, Input, InputNumber, Select, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { useApiQuery } from "../shared";
import { OutputBlock, RunFiles } from "./RunsTab";
import { sandboxApi } from "./sandbox-api";
import {
  CONSOLE_SNIPPETS,
  formatBytes,
  formatDuration,
  runStatusMeta,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxRunResult, SandboxStatus, SandboxWorkspace } from "./types";

// 本机便利：记住上次写的代码（不同浏览器互不影响；存储不可用时静默降级）。
const DRAFT_KEY = "yawnbot.sandbox.console";

function loadDraft(): string {
  try {
    return window.localStorage.getItem(DRAFT_KEY) ?? CONSOLE_SNIPPETS[0].code;
  } catch {
    return CONSOLE_SNIPPETS[0].code;
  }
}

function saveDraft(value: string): void {
  try {
    window.localStorage.setItem(DRAFT_KEY, value);
  } catch {
    // 隐私模式等场景下忽略。
  }
}

export function ConsoleTab({ status }: { status: SandboxStatus }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [code, setCode] = useState(loadDraft);
  const [workspace, setWorkspace] = useState("admin");
  const [timeout, setTimeoutSeconds] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SandboxRunResult | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => saveDraft(code), 400);
    return () => window.clearTimeout(timer);
  }, [code]);

  const workspacesQuery = useApiQuery<SandboxWorkspace[]>({
    queryKey: ["sandbox-workspaces", status.online],
    fetcher: (signal) => (status.online ? sandboxApi.workspaces(signal) : Promise.resolve([])),
  });
  const options = [
    { value: "admin", label: "管理员控制台（独立工作区）" },
    ...(workspacesQuery.data ?? [])
      .filter((item) => item.id !== "admin")
      .map((item) => ({ value: item.id, label: workspaceLabel(item.id, item.groupName) })),
  ];
  const maxTimeout = status.health?.ceilings.timeoutSeconds ?? 90;
  const canRun = status.online && status.health?.acceptingRuns !== false && code.trim().length > 0;

  const run = async () => {
    if (!canRun || running) return;
    setRunning(true);
    setError("");
    try {
      setResult(await sandboxApi.consoleRun({ code, workspace, timeoutSeconds: timeout }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "执行失败");
    } finally {
      setRunning(false);
    }
  };

  const meta = result ? runStatusMeta(result.status) : null;
  return (
    <div className="sbx-console">
      <div className="sbx-console-editor">
        <div className="sbx-toolbar">
          <Select value={workspace} onChange={setWorkspace} options={options} style={{ minWidth: 240 }} showSearch optionFilterProp="label" />
          <Tooltip title={`留空用策略里的默认时限（${status.settings.timeoutSeconds}s）`}>
            <InputNumber
              min={1}
              max={maxTimeout}
              value={timeout}
              onChange={(value) => setTimeoutSeconds(typeof value === "number" ? value : null)}
              placeholder={`${status.settings.timeoutSeconds}`}
              addonAfter="秒"
              style={{ width: 120 }}
            />
          </Tooltip>
          <span className="sbx-toolbar-spacer" />
          <Select
            placeholder="插入示例"
            value={null}
            onChange={(key: string) => {
              const snippet = CONSOLE_SNIPPETS.find((item) => item.key === key);
              if (snippet) setCode(snippet.code);
            }}
            options={CONSOLE_SNIPPETS.map((item) => ({ value: item.key, label: item.label }))}
            style={{ width: 130 }}
          />
          <Button icon={<ClearOutlined />} onClick={() => setCode("")}>清空</Button>
          <Button type="primary" icon={<CaretRightOutlined />} loading={running} disabled={!canRun} onClick={() => void run()}>
            运行
          </Button>
        </div>
        {workspace !== "admin" && (
          <Alert
            type="warning"
            showIcon
            className="sbx-gap"
            message="你正在群工作区里运行"
            description="代码能读写这个群的全部沙箱文件；控制台执行不计入群配额。"
          />
        )}
        <Input.TextArea
          className="sbx-code"
          value={code}
          onChange={(event) => setCode(event.target.value)}
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
              event.preventDefault();
              void run();
            }
            if (event.key === "Tab" && !event.shiftKey) {
              // Tab 插入四个空格而不是跳出编辑框。
              event.preventDefault();
              const target = event.currentTarget;
              const { selectionStart, selectionEnd } = target;
              const next = `${code.slice(0, selectionStart)}    ${code.slice(selectionEnd)}`;
              setCode(next);
              window.requestAnimationFrame(() => target.setSelectionRange(selectionStart + 4, selectionStart + 4));
            }
          }}
          spellCheck={false}
          autoSize={{ minRows: 14, maxRows: 32 }}
          aria-label="Python 代码"
        />
        <p className="sbx-muted sbx-hint">Ctrl/⌘ + Enter 运行 · 最后一行表达式会自动显示 · plt.show() 会把图存成 PNG · 没有网络，不能 pip install</p>
      </div>
      <div className="sbx-console-result" aria-live="polite">
        {error && <Alert type="error" showIcon message="执行失败" description={error} />}
        {!result && !error && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={status.online ? "运行结果会显示在这里" : "沙箱离线，暂时不能运行"} />
        )}
        {result && meta && (
          <>
            <div className="sbx-result-head">
              <Tag color={meta.color}>{meta.label}</Tag>
              <span>{formatDuration(result.durationMs)}</span>
              <span className="sbx-muted">CPU {formatDuration(result.cpuMs)} · 内存峰值 {formatBytes(result.maxRssKb * 1024)}</span>
              {result.usage.bytes != null && <span className="sbx-muted">工作区 {formatBytes(result.usage.bytes)}</span>}
            </div>
            {result.message && <Alert className="sbx-gap" type={result.status === "ok" ? "info" : "warning"} showIcon message={result.message} />}
            <OutputBlock label={`输出${result.stdoutTruncated ? "（已截断）" : ""}`} text={result.stdout} />
            <OutputBlock label={`错误${result.stderrTruncated ? "（已截断）" : ""}`} text={result.stderr} tone="error" />
            {!result.stdout && !result.stderr && !result.files.length && <p className="sbx-muted">没有输出。</p>}
            <RunFiles workspace={result.workspace} files={result.files} />
            {result.filesTruncated && <p className="sbx-muted">产物太多，只列出了一部分。</p>}
            <Button
              type="link"
              size="small"
              onClick={() => {
                void navigator.clipboard?.writeText(result.runId).then(() => message.success("已复制运行 ID"));
              }}
            >运行 ID {result.runId}</Button>
          </>
        )}
      </div>
    </div>
  );
}
