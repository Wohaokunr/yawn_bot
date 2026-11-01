import { CheckCircleFilled, CloseCircleFilled, MinusCircleFilled, StopOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Card, Empty, Input, Modal, Popconfirm, Progress, Skeleton, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useApiQuery } from "../shared";
import { sandboxApi } from "./sandbox-api";
import {
  formatBytes,
  formatDuration,
  formatEpoch,
  formatUptime,
  INBOX_MODE_META,
  runSourceLabel,
  runStatusMeta,
  workspaceLabel,
} from "./sandbox-meta";
import type { SandboxActiveRun, SandboxPackages, SandboxStats, SandboxStatus } from "./types";

export function Metric({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }): React.JSX.Element {
  return (
    <div className="sbx-metric">
      <span className="sbx-metric-label">{label}</span>
      <strong className="sbx-metric-value">{value}</strong>
      {hint && <span className="sbx-metric-hint">{hint}</span>}
    </div>
  );
}

/** 最近 24 小时每小时执行量；红色叠加是非成功的部分。 */
export function HourlyRunsChart({ hourly, errors }: { hourly: number[]; errors: number[] }): React.JSX.Element {
  const max = Math.max(1, ...hourly);
  const width = 24 * 14;
  const height = 96;
  const now = new Date();
  return (
    <svg className="sbx-hourly" viewBox={`0 0 ${width} ${height + 16}`} role="img" aria-label="最近 24 小时每小时运行次数">
      {hourly.map((value, index) => {
        const barHeight = (value / max) * height;
        const errorHeight = ((errors[index] ?? 0) / max) * height;
        const hour = (now.getHours() - (23 - index) + 24) % 24;
        return (
          <g key={index}>
            <title>{`${hour}:00 · ${value} 次${errors[index] ? `，其中 ${errors[index]} 次未成功` : ""}`}</title>
            <rect x={index * 14 + 2} y={height - barHeight} width={10} height={Math.max(barHeight, value ? 2 : 0)} rx={2} className="sbx-bar" />
            {errorHeight > 0 && <rect x={index * 14 + 2} y={height - errorHeight} width={10} height={errorHeight} rx={2} className="sbx-bar-error" />}
            {index % 6 === 0 && <text x={index * 14 + 7} y={height + 13} textAnchor="middle" className="sbx-axis">{hour}时</text>}
          </g>
        );
      })}
    </svg>
  );
}

export function CheckRow({ ok, label, detail }: { ok: boolean | null | undefined; label: string; detail: string }): React.JSX.Element {
  const icon = ok == null ? <MinusCircleFilled className="sbx-check-na" /> : ok ? <CheckCircleFilled className="sbx-check-ok" /> : <CloseCircleFilled className="sbx-check-bad" />;
  return (
    <li className="sbx-check">
      {icon}
      <div>
        <strong>{label}</strong>
        <span>{detail}</span>
      </div>
    </li>
  );
}

function PackagesModal({ open, onClose }: { open: boolean; onClose: () => void }): React.JSX.Element {
  const [filter, setFilter] = useState("");
  const query = useApiQuery<SandboxPackages | null>({
    queryKey: ["sandbox-packages", open],
    fetcher: (signal) => (open ? sandboxApi.packages(signal) : Promise.resolve(null)),
  });
  const rows = useMemo(
    () => Object.entries(query.data?.all ?? {})
      .filter(([name]) => name.toLowerCase().includes(filter.trim().toLowerCase()))
      .map(([name, version]) => ({ name, version })),
    [query.data, filter],
  );
  return (
    <Modal open={open} onCancel={onClose} footer={null} title={`沙箱里安装的 Python 包${query.data ? `（${query.data.total} 个）` : ""}`} width={560}>
      <Input.Search allowClear placeholder="搜索包名" value={filter} onChange={(event) => setFilter(event.target.value)} style={{ marginBottom: 12 }} />
      <Table
        size="small"
        rowKey="name"
        loading={query.loading}
        dataSource={rows}
        pagination={{ pageSize: 12, showSizeChanger: false, hideOnSinglePage: true }}
        columns={[{ title: "包", dataIndex: "name" }, { title: "版本", dataIndex: "version", width: 140 }]}
      />
    </Modal>
  );
}

export function ActiveRunsTable({ runs, onCancelled }: { runs: SandboxActiveRun[]; onCancelled: () => void }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const columns: ColumnsType<SandboxActiveRun> = [
    { title: "工作区", dataIndex: "workspace", render: (value: string, row) => workspaceLabel(value, row.groupName) },
    { title: "来源", render: (_, row) => `${runSourceLabel(row.meta.source)}${row.meta.purpose ? ` · ${row.meta.purpose}` : ""}` },
    { title: "阶段", dataIndex: "phase", width: 90, render: (value: string) => <Tag color={value === "running" ? "processing" : "default"}>{value === "running" ? "执行中" : "排队"}</Tag> },
    { title: "已用时", dataIndex: "elapsedMs", width: 100, render: (value: number) => formatDuration(value) },
    {
      title: "",
      width: 80,
      render: (_, row) => (
        <Popconfirm
          title="终止这次执行？"
          description="进程会被立即杀掉，已写出的文件保留。"
          onConfirm={async () => {
            try {
              const result = await sandboxApi.cancel(row.runId);
              message[result.cancelled ? "success" : "info"](result.cancelled ? "已发送终止信号" : "这次执行已经结束");
              onCancelled();
            } catch (error) {
              message.error(error instanceof Error ? error.message : "终止失败");
            }
          }}
        >
          <Button size="small" danger icon={<StopOutlined />}>终止</Button>
        </Popconfirm>
      ),
    },
  ];
  return <Table size="small" rowKey="runId" columns={columns} dataSource={runs} pagination={false} />;
}

export function OverviewTab({
  status,
  onOpenRuns,
  onOpenInbox,
}: {
  status: SandboxStatus;
  onOpenRuns: (extra: Record<string, string | null>) => void;
  onOpenInbox?: (extra?: Record<string, string | null>) => void;
}): React.JSX.Element {
  const [packagesOpen, setPackagesOpen] = useState(false);
  const statsQuery = useApiQuery<SandboxStats | null>({
    queryKey: ["sandbox-stats", status.online],
    fetcher: (signal) => (status.online ? sandboxApi.stats(signal) : Promise.resolve(null)),
    invalidation: { resources: ["sandbox"] },
  });
  const stats = statsQuery.data;
  const health = status.health;
  const isolation = health?.isolation;

  const runs = stats?.runs;
  const failures = runs ? runs.total - (runs.byStatus.ok ?? 0) : 0;
  const successRate = runs && runs.total ? Math.round(((runs.byStatus.ok ?? 0) / runs.total) * 100) : null;
  const memory = stats?.resources.memory;
  const memoryPercent = memory?.current && memory.max ? Math.round((memory.current / memory.max) * 100) : null;
  const statusRows = Object.entries(runs?.byStatus ?? {}).sort((a, b) => b[1] - a[1]);

  return (
    <div className="sbx-overview">
      <div className="sbx-metrics">
        <Metric label="今日执行（北京时间）" value={status.usage.totalRuns} hint={`${status.usage.groups.length} 个群在用`} />
        <Metric label="24 小时成功率" value={successRate == null ? "—" : `${successRate}%`} hint={runs ? `${runs.total} 次，${failures} 次未成功` : "沙箱离线"} />
        <Metric label="耗时 P50 / P95" value={runs ? `${formatDuration(runs.durationP50Ms)}` : "—"} hint={runs ? `P95 ${formatDuration(runs.durationP95Ms)}` : undefined} />
        <Metric label="工作区" value={stats ? stats.workspaces.count : "—"} hint={stats ? `${formatBytes(stats.workspaces.bytes)} · ${stats.workspaces.files} 个文件` : undefined} />
      </div>

      <div className="sbx-grid">
        <Card size="small" title="最近 24 小时" className="sbx-card-wide">
          {statsQuery.initialLoading ? <Skeleton active /> : runs && runs.total ? (
            <>
              <HourlyRunsChart hourly={runs.hourly} errors={runs.hourlyErrors} />
              <div className="sbx-status-breakdown">
                {statusRows.map(([key, value]) => {
                  const meta = runStatusMeta(key);
                  return (
                    <button type="button" key={key} className="sbx-breakdown-item" onClick={() => onOpenRuns({ "sandbox.status": key })}>
                      <Tag color={meta.color}>{meta.label}</Tag>
                      <span>{value}</span>
                    </button>
                  );
                })}
              </div>
            </>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={status.online ? "最近 24 小时还没有执行记录" : "沙箱离线，暂无统计"} />}
        </Card>

        <Card size="small" title="隔离自检">
          {isolation ? (
            <ul className="sbx-checks">
              {isolation.checks.networkGuarded === undefined ? (
                <CheckRow ok={isolation.checks.networkDisabled} label="无网络" detail={isolation.checks.networkDisabled ? "容器内没有网卡" : `发现网卡 ${isolation.networkInterfaces.join(", ")}`} />
              ) : (
                <CheckRow
                  ok={isolation.checks.networkGuarded}
                  label={isolation.checks.networkDisabled ? "无网络" : "出网受控"}
                  detail={
                    isolation.checks.networkDisabled
                      ? "容器内没有网卡"
                      : isolation.checks.networkGuarded
                        ? "可以上外网；内网地址被防火墙挡住，按设置给每个工作区开关"
                        : `有网卡 ${isolation.networkInterfaces.join(", ")} 但 runner 管不住防火墙${health?.network?.error ? `：${health.network.error}` : ""}`
                  }
                />
              )}
              <CheckRow ok={isolation.checks.privilegeDrop} label="按群降权" detail="每个群一个独立 uid，文件互不可见" />
              <CheckRow ok={isolation.checks.readonlyRoot} label="只读根文件系统" detail="镜像文件不可被代码改写" />
              <CheckRow ok={isolation.checks.noNewPrivileges} label="no-new-privileges" detail="setuid 程序无法提权" />
              <CheckRow ok={isolation.checks.childReaper} label="孤儿进程回收" detail={isolation.checks.childReaper === false ? "runner 是 PID 1，给 sandbox 服务加 init: true" : "后台进程退出后会被回收"} />
              <CheckRow ok={status.transport === "unix" ? true : null} label="Unix socket 通信" detail={status.transport === "unix" ? "不监听任何网络端口" : "TCP 模式，仅建议开发时使用"} />
            </ul>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="沙箱离线，无法自检" />}
        </Card>

        <Card size="small" title="容器资源">
          {stats ? (
            <div className="sbx-resources">
              <div>
                <span>内存</span>
                {memoryPercent != null ? <Progress percent={memoryPercent} size="small" status={memoryPercent > 85 ? "exception" : "normal"} /> : <Tag>不限</Tag>}
                <small>{formatBytes(memory?.current)} / {memory?.max ? formatBytes(memory.max) : "不限"}，峰值 {formatBytes(memory?.peak)}</small>
              </div>
              <div>
                <span>进程</span>
                <small>{stats.resources.pids.current ?? "—"} / {stats.resources.pids.max ?? "不限"}</small>
              </div>
              <div>
                <span>CPU</span>
                <small>{stats.resources.cpu.quotaCores ? `${stats.resources.cpu.quotaCores} 核配额` : `${stats.resources.cpu.count ?? "?"} 核`}{stats.resources.cpu.load ? ` · 负载 ${stats.resources.cpu.load.map((v) => v.toFixed(2)).join(" / ")}` : ""}</small>
              </div>
              <div>
                <span>工作区磁盘</span>
                <small>{stats.resources.disk ? `已用 ${formatBytes(stats.resources.disk.used)}，剩余 ${formatBytes(stats.resources.disk.free)}` : "—"}</small>
              </div>
              <div>
                <span>运行时长</span>
                <small>{formatUptime(stats.resources.uptimeSeconds)}{stats.lastPrune ? ` · 上次清理 ${formatEpoch(stats.lastPrune.at)}` : ""}</small>
              </div>
            </div>
          ) : <Skeleton active paragraph={{ rows: 3 }} loading={statsQuery.initialLoading}><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="沙箱离线" /></Skeleton>}
        </Card>

        <Card
          size="small"
          title="群聊附件（今天）"
          extra={onOpenInbox && <Button size="small" type="link" onClick={() => onOpenInbox()}>收件箱</Button>}
        >
          {status.inbox ? (
            <div className="sbx-resources">
              <div>
                <span>入箱</span>
                <small>{status.inbox.enabled ? INBOX_MODE_META[status.inbox.autoImport]?.label ?? status.inbox.autoImport : "已关闭"}</small>
              </div>
              <div>
                <span>放进电脑</span>
                <small>{status.inbox.today.imported ?? 0} 个 · {formatBytes(status.inbox.today.bytes ?? 0)}{status.inbox.today.reused ? ` · 复用 ${status.inbox.today.reused} 次` : ""}</small>
              </div>
              <div>
                <span>失败</span>
                {status.inbox.today.failed && onOpenInbox ? (
                  <Button size="small" type="link" danger onClick={() => onOpenInbox({ "sandbox.inbox.status": "failed" })}>{status.inbox.today.failed} 次，看原因</Button>
                ) : <small>{status.inbox.today.failed ?? 0} 次</small>}
              </div>
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />}
        </Card>

        <Card size="small" title="最活跃的工作区">
          {stats?.topWorkspaces.length ? (
            <ol className="sbx-rank">
              {stats.topWorkspaces.map((row) => (
                <li key={row.workspace}>
                  <button type="button" onClick={() => onOpenRuns({ "sandbox.ws": row.workspace })}>{workspaceLabel(row.workspace, row.groupName)}</button>
                  <span>{row.runs} 次</span>
                </li>
              ))}
            </ol>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />}
        </Card>

        <Card
          size="small"
          title="预装库"
          extra={health && <Button size="small" type="link" onClick={() => setPackagesOpen(true)}>全部 {health.packageCount} 个</Button>}
        >
          {health ? (
            <div className="sbx-packages">
              {health.packages.map((item) => (
                <Tooltip key={item.name} title={item.version}>
                  <Tag>{item.name}</Tag>
                </Tooltip>
              ))}
            </div>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="沙箱离线" />}
        </Card>

        <Card size="small" title="正在执行" className="sbx-card-wide">
          {stats?.active.length ? (
            <ActiveRunsTable runs={stats.active} onCancelled={statsQuery.reload} />
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有执行中的代码" />}
        </Card>
      </div>
      <PackagesModal open={packagesOpen} onClose={() => setPackagesOpen(false)} />
    </div>
  );
}
