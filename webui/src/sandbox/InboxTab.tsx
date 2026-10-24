// 「附件收件箱」：群聊里发的图片/文件/语音/视频有没有放进内置电脑、为什么没放进去。
import { FolderOpenOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Empty, Select, Skeleton, Switch, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert, useApiQuery } from "../shared";
import { CheckRow, Metric } from "./OverviewTab";
import { sandboxApi } from "./sandbox-api";
import {
  ADMIN_FIXABLE_REASONS,
  dirName,
  formatBytes,
  formatDuration,
  formatEpoch,
  INBOX_MODE_META,
  INBOX_STATUS_META,
  INBOX_TYPE_META,
  inboxSourceLabel,
  inboxStatusMeta,
  inboxTriggerLabel,
  inboxViaLabel,
  relativeEpoch,
  updateSearchParams,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxInboxEvent, SandboxInboxPage, SandboxInboxStatus, SandboxInboxType, SandboxStatus } from "./types";

const AUTO_REFRESH_MS = 10_000;

/** 失败原因汇总：每个原因给出说明和“谁来修”。 */
export function InboxReasons({
  byReason,
  reasons,
  onPick,
}: {
  byReason: Record<string, number>;
  reasons: SandboxInboxPage["reasons"];
  onPick: (reason: string) => void;
}): React.JSX.Element {
  const rows = Object.entries(byReason).sort((a, b) => b[1] - a[1]);
  if (!rows.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天没有失败的入箱 🎉" />;
  return (
    <ul className="sbx-inbox-reasons">
      {rows.map(([code, count]) => {
        const info = reasons[code];
        const admin = ADMIN_FIXABLE_REASONS.has(code);
        return (
          <li key={code}>
            <button type="button" onClick={() => onPick(code)}>
              <strong>{info?.message ?? code}</strong>
              <Tag color={admin ? "orange" : "blue"}>{admin ? "需要管理员" : "成员可解决"}</Tag>
              <span className="sbx-inbox-count">{count} 次</span>
            </button>
            {info?.hint && <small>{info.hint}</small>}
          </li>
        );
      })}
    </ul>
  );
}

/** 取文件链路的自检：能不能拿到 NapCat 那边的字节。 */
export function InboxSetupChecklist({ page, runnerOnline }: { page: SandboxInboxPage; runnerOnline: boolean }): React.JSX.Element {
  const vias = page.summary.byVia ?? {};
  const base64Hits = Object.entries(vias).filter(([via]) => via.endsWith(":base64")).reduce((sum, [, n]) => sum + n, 0);
  const unavailable = page.summary.byReason?.unavailable ?? 0;
  const hostBlocked = page.summary.byReason?.host_blocked ?? 0;
  return (
    <ul className="sbx-checks">
      <CheckRow ok={page.settings.enabled} label="入箱总开关" detail={page.settings.enabled ? "已开启" : "在「策略与限额 → 群聊附件入箱」打开"} />
      <CheckRow ok={runnerOnline} label="沙箱在线" detail={runnerOnline ? "附件能写进各群工作区的 inbox/" : "沙箱离线时附件只会记为失败"} />
      <CheckRow
        ok={unavailable ? false : base64Hits ? true : null}
        label="NapCat 直接回传文件内容"
        detail={unavailable
          ? `今天有 ${unavailable} 次拿不到内容：在 NapCat 的 OneBot 配置里打开 enableLocalFile2Url`
          : base64Hits
            ? `今天 ${base64Hits} 次通过 base64 直接拿到`
            : "还没有数据；建议在 NapCat 的 OneBot 配置里打开 enableLocalFile2Url"}
      />
      <CheckRow
        ok={hostBlocked ? false : true}
        label="下载域名白名单"
        detail={hostBlocked ? `今天有 ${hostBlocked} 次链接不在白名单：${page.hosts.join("、")}` : `只从 ${page.hosts.slice(0, 3).join("、")} 等 QQ 域名下载`}
      />
      <CheckRow
        ok={page.localRoots ? true : null}
        label="共享目录（可选）"
        detail={page.localRoots ? "已配置 AGENT_SANDBOX_INBOX_LOCAL_ROOTS，NapCat 给本机路径时也能读" : "没配 AGENT_SANDBOX_INBOX_LOCAL_ROOTS；NapCat 与 Bot 共享目录时可以配上"}
      />
    </ul>
  );
}

export function InboxTab({ status, onOpenFile }: { status: SandboxStatus; onOpenFile: (workspace: string, dir: string, path: string) => void }): React.JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const group = searchParams.get("sandbox.inbox.group") ?? "";
  const statusFilter = searchParams.get("sandbox.inbox.status") ?? "";
  const typeFilter = searchParams.get("sandbox.inbox.type") ?? "";
  const reasonFilter = searchParams.get("sandbox.inbox.reason") ?? "";
  const live = searchParams.get("sandbox.inbox.live") === "1";
  const setFilter = (patch: Record<string, string | null>) => setSearchParams(updateSearchParams(searchParams, patch), { replace: true });

  const query = useApiQuery<SandboxInboxPage>({
    queryKey: ["sandbox-inbox", group, statusFilter, typeFilter],
    fetcher: (signal) => sandboxApi.inbox({ group, status: statusFilter, type: typeFilter, limit: 300 }, signal),
    invalidation: { resources: ["sandbox"] },
  });
  const { reload } = query;

  useEffect(() => {
    if (!live) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [live, reload]);

  const page = query.data;
  const rows = useMemo(
    () => (page?.items ?? []).filter((item) => !reasonFilter || item.reason === reasonFilter),
    [page, reasonFilter],
  );
  const groupOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const item of page?.items ?? []) {
      if (item.groupId && !seen.has(item.groupId)) seen.set(item.groupId, workspaceLabel(`g${item.groupId}`, item.groupName));
    }
    if (group && !seen.has(group)) seen.set(group, `群 ${group}`);
    return [...seen.entries()].map(([value, label]) => ({ value, label }));
  }, [page, group]);

  if (!page) {
    return query.error ? <QueryErrorAlert error={query.error} onRetry={reload} /> : <Skeleton active />;
  }

  const summary = page.summary;
  const attempts = summary.total || 0;
  const okCount = (summary.imported ?? 0) + (summary.reused ?? 0);
  const successRate = attempts ? Math.round((okCount / attempts) * 100) : null;
  const modeMeta = INBOX_MODE_META[page.settings.autoImport];

  const columns: ColumnsType<SandboxInboxEvent> = [
    {
      title: "时间",
      dataIndex: "at",
      width: 96,
      render: (value: number) => <Tooltip title={formatEpoch(value)}>{relativeEpoch(value)}</Tooltip>,
    },
    {
      title: "群",
      dataIndex: "groupId",
      width: 150,
      ellipsis: true,
      render: (_: unknown, row) => row.workspace ? (
        <button type="button" className="sbx-link" onClick={() => setFilter({ "sandbox.inbox.group": row.groupId })}>
          {workspaceLabel(row.workspace, row.groupName)}
        </button>
      ) : "—",
    },
    {
      title: "附件",
      dataIndex: "name",
      ellipsis: true,
      render: (_: unknown, row) => {
        const type = INBOX_TYPE_META[row.mediaType] ?? { label: row.mediaType, emoji: "📎" };
        return (
          <span className="sbx-inbox-name">
            <span aria-hidden="true">{type.emoji}</span>
            <span>{row.name || type.label}</span>
            {row.size ? <small>{formatBytes(row.size)}</small> : null}
          </span>
        );
      },
    },
    {
      title: "来源",
      width: 120,
      render: (_: unknown, row) => (
        <Tooltip title={row.senderId ? `发送者 ${row.senderId}${row.messageId ? ` · 消息 ${row.messageId}` : ""}` : undefined}>
          <span>{inboxSourceLabel(row.source)}</span>
          <small className="sbx-muted"> · {inboxTriggerLabel(row.trigger)}</small>
        </Tooltip>
      ),
    },
    {
      title: "结果",
      dataIndex: "status",
      width: 220,
      render: (value: string, row) => {
        const meta = inboxStatusMeta(value);
        const reason = row.reason ? page.reasons[row.reason] : undefined;
        return (
          <span className="sbx-inbox-result">
            <Tag color={meta.color}>{meta.label}</Tag>
            {reason && <Tooltip title={reason.hint}><span className="sbx-inbox-reason">{reason.message}</span></Tooltip>}
            {!reason && row.via && <small className="sbx-muted">{inboxViaLabel(row.via)}{row.durationMs ? ` · ${formatDuration(row.durationMs)}` : ""}</small>}
          </span>
        );
      },
    },
    {
      title: "位置",
      dataIndex: "path",
      width: 190,
      ellipsis: true,
      render: (value: string | undefined, row) => value && row.workspace && (row.status === "imported" || row.status === "reused") ? (
        <Button size="small" type="link" icon={<FolderOpenOutlined />} onClick={() => onOpenFile(row.workspace as string, dirName(value), value)}>
          {value}
        </Button>
      ) : <span className="sbx-muted">{value ?? "—"}</span>,
    },
  ];

  return (
    <div className="sbx-inbox">
      {!page.settings.enabled && (
        <Alert
          className="section-alert"
          type="warning"
          showIcon
          message="群聊附件入箱已关闭"
          description="群友发的图片、文件、语音不会再放进内置电脑。到「策略与限额 → 群聊附件入箱」打开。"
        />
      )}
      <div className="sbx-metrics">
        <Metric label="今日放入" value={summary.imported ?? 0} hint={`${formatBytes(summary.bytes)} · 复用 ${summary.reused ?? 0} 次`} />
        <Metric label="今日成功率" value={successRate == null ? "—" : `${successRate}%`} hint={`${attempts} 次尝试，${summary.failed ?? 0} 次失败`} />
        <Metric
          label="自动放入"
          value={page.settings.enabled ? modeMeta.label : "已关闭"}
          hint={<Tooltip title={modeMeta.hint}><span>每轮最多 {page.settings.maxPerTurn} 个 · 单个 ≤ {page.settings.maxFileMb}MB</span></Tooltip>}
        />
        <Metric
          label="允许的类型"
          value={page.settings.types.map((type) => INBOX_TYPE_META[type]?.emoji ?? type).join(" ") || "—"}
          hint={page.settings.groupDailyMb ? `每群每天 ${page.settings.groupDailyMb}MB · 回看 ${page.settings.historyMinutes} 分钟` : `不限每日总量 · 回看 ${page.settings.historyMinutes} 分钟`}
        />
      </div>

      <div className="sbx-grid">
        <Card size="small" title="失败原因（今天）">
          <InboxReasons byReason={summary.byReason ?? {}} reasons={page.reasons} onPick={(reason) => setFilter({ "sandbox.inbox.status": "failed", "sandbox.inbox.reason": reason })} />
        </Card>
        <Card size="small" title="取文件链路自检">
          <InboxSetupChecklist page={page} runnerOnline={status.online} />
        </Card>
        <Card size="small" title="按类型（今天）">
          {Object.keys(summary.byType ?? {}).length ? (
            <div className="sbx-status-breakdown">
              {Object.entries(summary.byType).map(([type, count]) => {
                const meta = INBOX_TYPE_META[type as SandboxInboxType];
                return (
                  <button type="button" key={type} className="sbx-breakdown-item" onClick={() => setFilter({ "sandbox.inbox.type": type })}>
                    <span aria-hidden="true">{meta?.emoji ?? "📎"}</span> {meta?.label ?? type}
                    <span>{count}</span>
                  </button>
                );
              })}
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天还没人发附件给 Agent" />}
          {Object.keys(summary.byVia ?? {}).length > 0 && (
            <p className="sbx-muted sbx-inbox-vias">
              取到方式：{Object.entries(summary.byVia).map(([via, count]) => `${inboxViaLabel(via)} ${count}`).join("，")}
            </p>
          )}
        </Card>
      </div>

      <div className="sbx-toolbar">
        <strong>入箱记录</strong>
        <span className="sbx-muted">最近 {page.items.length} 条{reasonFilter ? `，筛选原因：${page.reasons[reasonFilter]?.message ?? reasonFilter}` : ""}</span>
        <span className="sbx-toolbar-spacer" />
        <Select
          size="small"
          allowClear
          showSearch
          placeholder="全部群"
          value={group || undefined}
          options={groupOptions}
          onChange={(value?: string) => setFilter({ "sandbox.inbox.group": value ?? null })}
          style={{ width: 180 }}
          aria-label="按群筛选"
        />
        <Select
          size="small"
          allowClear
          placeholder="全部结果"
          value={statusFilter || undefined}
          options={(Object.keys(INBOX_STATUS_META) as SandboxInboxStatus[]).map((value) => ({ value, label: INBOX_STATUS_META[value].label }))}
          onChange={(value?: string) => setFilter({ "sandbox.inbox.status": value ?? null, "sandbox.inbox.reason": null })}
          style={{ width: 120 }}
          aria-label="按结果筛选"
        />
        <Select
          size="small"
          allowClear
          placeholder="全部类型"
          value={typeFilter || undefined}
          options={(Object.keys(INBOX_TYPE_META) as SandboxInboxType[]).map((value) => ({ value, label: `${INBOX_TYPE_META[value].emoji} ${INBOX_TYPE_META[value].label}` }))}
          onChange={(value?: string) => setFilter({ "sandbox.inbox.type": value ?? null })}
          style={{ width: 120 }}
          aria-label="按类型筛选"
        />
        <Tooltip title="每 10 秒自动刷新（标签页可见时）">
          <span className="sbx-inline-switch">
            <Switch size="small" checked={live} onChange={(checked) => setFilter({ "sandbox.inbox.live": checked ? "1" : null })} aria-label="自动刷新" /> 实时
          </span>
        </Tooltip>
        <Button size="small" icon={<ReloadOutlined />} loading={query.refreshing} onClick={reload}>刷新</Button>
        {(group || statusFilter || typeFilter || reasonFilter) && (
          <Button size="small" type="link" onClick={() => setFilter({ "sandbox.inbox.group": null, "sandbox.inbox.status": null, "sandbox.inbox.type": null, "sandbox.inbox.reason": null })}>
            清除筛选
          </Button>
        )}
      </div>
      {query.error && <QueryErrorAlert error={query.error} onRetry={reload} />}
      <Table
        size="small"
        rowKey={(row) => `${row.at}-${row.groupId}-${row.messageId}-${row.index}-${row.status}-${row.path ?? row.name ?? ""}`}
        columns={columns}
        dataSource={rows}
        rowClassName={(row) => (row.status === "failed" ? "sbx-row-failed" : "")}
        pagination={{ pageSize: 30, showSizeChanger: false, hideOnSinglePage: true }}
        scroll={{ x: 900 }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={group || statusFilter || typeFilter || reasonFilter ? "没有符合筛选的记录" : "还没有入箱记录。在群里发一张图片并 @ Agent 让它处理试试"}
            />
          ),
        }}
      />
    </div>
  );
}
