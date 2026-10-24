import {
  CodeOutlined,
  DashboardOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  InboxOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Alert, Button, Skeleton, Tabs, Tag, Tooltip } from "antd";
import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { ConsoleTab } from "./ConsoleTab";
import { FilesTab } from "./FilesTab";
import { InboxTab } from "./InboxTab";
import { OverviewTab } from "./OverviewTab";
import { PolicyTab } from "./PolicyTab";
import { RunsTab } from "./RunsTab";
import { sandboxApi } from "./sandbox-api";
import { describeAccess, formatBytes, INBOX_MODE_META, parseSandboxTab, SANDBOX_STATE_META, updateSearchParams } from "./sandbox-meta";
import type { SandboxTab } from "./sandbox-meta";
import type { SandboxStatus } from "./types";

const STATUS_POLL_MS = 15_000;

export function SandboxStatusBar({
  status,
  checking,
  onRecheck,
  onOpenConsole,
  onOpenInbox,
}: {
  status: SandboxStatus;
  checking: boolean;
  onRecheck: () => void;
  onOpenConsole: () => void;
  onOpenInbox?: () => void;
}): React.JSX.Element {
  const inbox = status.inbox;
  const meta = SANDBOX_STATE_META[status.state];
  const health = status.health;
  const checks = health?.isolation.checks;
  const detail = status.error?.message ?? (status.state === "ready" ? describeAccess(status.settings) : meta.hint);
  return (
    <section className={`sbx-status is-${meta.tone}`} aria-label="代码沙箱状态">
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${meta.tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>代码沙箱 · {meta.label}</strong>
          <span title={detail}>{detail}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        {health ? (
          <>
            <Tag>Python {health.python}</Tag>
            <Tag>runner {health.version}</Tag>
            <Tooltip title="正在执行 / 排队中 / 并发上限">
              <Tag color={health.running > 0 ? "processing" : undefined}>
                执行 {health.running} · 排队 {health.waiting} / {health.concurrency}
              </Tag>
            </Tooltip>
            <Tooltip title={checks?.networkDisabled ? "容器里没有网卡，代码无法联网" : `检测到网卡：${health.isolation.networkInterfaces.join(", ")}`}>
              <Tag color={checks?.networkDisabled ? "green" : "red"}>{checks?.networkDisabled ? "无网络" : "有网络"}</Tag>
            </Tooltip>
            <Tooltip title={checks?.privilegeDrop ? "每个群用独立 uid 运行，互相看不到文件" : "无法降权，所有群共享同一个用户"}>
              <Tag color={checks?.privilegeDrop ? "green" : "red"}>{checks?.privilegeDrop ? "独立用户" : "未降权"}</Tag>
            </Tooltip>
          </>
        ) : (
          <Tag>{status.transport === "disabled" ? "未配置地址" : status.transport === "unix" ? "Unix socket" : "TCP"}</Tag>
        )}
        {inbox && (
          <Tooltip title={inbox.enabled ? `群聊附件入箱：${INBOX_MODE_META[inbox.autoImport]?.hint ?? ""}` : "群聊附件入箱已关闭"}>
            <Tag
              color={!inbox.enabled ? undefined : inbox.today.failed ? "orange" : "magenta"}
              className={onOpenInbox ? "sbx-clickable" : undefined}
              onClick={onOpenInbox}
            >
              {inbox.enabled
                ? `附件 今日 ${inbox.today.imported ?? 0} 个${inbox.today.bytes ? ` · ${formatBytes(inbox.today.bytes)}` : ""}${inbox.today.failed ? ` · ${inbox.today.failed} 失败` : ""}`
                : "附件入箱关闭"}
            </Tag>
          </Tooltip>
        )}
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={checking} onClick={onRecheck}>重新检测</Button>
        <Button size="small" type="primary" icon={<CodeOutlined />} disabled={!status.online} onClick={onOpenConsole}>控制台</Button>
      </div>
    </section>
  );
}

export function SandboxPage(): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseSandboxTab(searchParams.get("sandbox.tab"));
  // “重新检测”只让下一次请求绕过后端 15 秒健康缓存，轮询仍走缓存。
  const bypassCache = useRef(false);
  const statusQuery = useApiQuery<SandboxStatus>({
    queryKey: ["sandbox-status"],
    fetcher: (signal) => {
      const refresh = bypassCache.current;
      bypassCache.current = false;
      return sandboxApi.status(refresh, signal);
    },
    invalidation: { resources: ["sandbox"] },
  });
  const { reload } = statusQuery;

  // 状态条轻量轮询：只在标签页可见时刷新，后台标签页不打扰沙箱。
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);

  const setTab = (next: SandboxTab, extra: Record<string, string | null> = {}) => {
    setSearchParams(
      updateSearchParams(searchParams, { "sandbox.tab": next === "overview" ? null : next, ...extra }),
      { replace: true },
    );
  };

  const status = statusQuery.data;
  if (!status) {
    return (
      <div className="sbx-page">
        <h2 className="sbx-title">代码沙箱</h2>
        {statusQuery.error ? <QueryErrorAlert error={statusQuery.error} onRetry={reload} /> : <Skeleton active />}
      </div>
    );
  }

  return (
    <div className="sbx-page">
      <div className="sbx-heading">
        <div>
          <h2 className="sbx-title">代码沙箱</h2>
          <p>Agent 的内置电脑：群里发的图片、文件、语音会放进来，数据分析、画图、生成文件与音乐都在隔离容器里完成</p>
        </div>
      </div>
      <SandboxStatusBar
        status={status}
        checking={statusQuery.refreshing}
        onRecheck={() => {
          bypassCache.current = true;
          reload();
        }}
        onOpenConsole={() => setTab("console")}
        onOpenInbox={() => setTab("inbox")}
      />
      {status.state === "disabled" && (
        <Alert
          className="section-alert"
          type="info"
          showIcon
          message="代码沙箱没有启用"
          description="在「环境配置 → Agent 代码沙箱」里设置 AGENT_SANDBOX_URL，并在部署里启动 sandbox 服务后，这里会自动连上。"
        />
      )}
      {status.state === "unsafe" && (
        <Alert
          className="section-alert"
          type="error"
          showIcon
          message="隔离自检没有通过，已拒绝执行代码"
          description="沙箱容器仍然能联网或不能切换到独立用户。请确认 compose 里 sandbox 服务是 network_mode: none 且以 root 启动（runner 会为每个群降权）。"
        />
      )}
      <Tabs
        className="sbx-tabs"
        activeKey={tab}
        onChange={(key) => setTab(key as SandboxTab)}
        destroyOnHidden
        items={[
          { key: "overview", label: <span><DashboardOutlined /> 概览</span>, children: <OverviewTab status={status} onOpenRuns={(extra) => setTab("runs", extra)} onOpenInbox={(extra) => setTab("inbox", extra)} /> },
          {
            key: "inbox",
            label: <span><InboxOutlined /> 附件收件箱</span>,
            children: <InboxTab status={status} onOpenFile={(ws, dir, path) => setTab("files", { "sandbox.ws": ws, "sandbox.dir": dir || null, "sandbox.file": path })} />,
          },
          { key: "runs", label: <span><HistoryOutlined /> 运行历史</span>, children: <RunsTab online={status.online} /> },
          { key: "files", label: <span><FolderOpenOutlined /> 工作区文件</span>, children: <FilesTab status={status} /> },
          { key: "console", label: <span><CodeOutlined /> 控制台</span>, children: <ConsoleTab status={status} /> },
          { key: "policy", label: <span><SafetyCertificateOutlined /> 策略与限额</span>, children: <PolicyTab status={status} /> },
        ]}
      />
    </div>
  );
}
