// 代码沙箱管理页的接口类型；字段与后端 webui/sandbox.py（camelCase 化后的 runner 返回）一致。

export type SandboxState = "ready" | "paused" | "unsafe" | "offline" | "disabled";

export type SandboxRunStatus =
  | "ok"
  | "error"
  | "timeout"
  | "cpu_limit"
  | "memory_limit"
  | "disk_limit"
  | "output_limit"
  | "killed"
  | "cancelled"
  | "rejected"
  | "busy";

export type SandboxFileKind =
  | "image"
  | "vector"
  | "audio"
  | "midi"
  | "video"
  | "table"
  | "text"
  | "document"
  | "archive"
  | "other";

export interface SandboxIsolation {
  isolated: boolean;
  checks: {
    networkDisabled: boolean;
    privilegeDrop: boolean;
    readonlyRoot: boolean | null;
    noNewPrivileges: boolean | null;
    childReaper?: boolean;
  };
  networkInterfaces: string[];
  capabilities?: string | null;
  seccomp?: string | null;
  transport?: string;
}

export interface SandboxHealth {
  ok: boolean;
  version: string;
  api: number;
  python: string;
  packages: Array<{ name: string; version: string }>;
  packageCount: number;
  isolation: SandboxIsolation;
  acceptingRuns: boolean;
  ceilings: Record<string, number>;
  concurrency: number;
  queueLimit: number;
  running: number;
  waiting: number;
  retentionDays: number;
  maxUploadMb: number;
}

export interface SandboxSettings {
  enabled: boolean;
  groupMode: "all" | "allowlist";
  allowedGroups: string[];
  blockedGroups: string[];
  adminOnly: boolean;
  memberDailyRuns: number;
  groupDailyRuns: number;
  memberCooldownSeconds: number;
  timeoutSeconds: number;
  memoryMb: number;
  diskQuotaMb: number;
  maxOutputChars: number;
  maxShareMb: number;
  allowUnisolated: boolean;
}

export interface SandboxUsageRow {
  groupId: string;
  groupName?: string | null;
  userId?: string;
  runs: number;
}

export interface SandboxUsage {
  day: string | null;
  totalRuns: number;
  groups: SandboxUsageRow[];
  members: SandboxUsageRow[];
  groupLimit: number;
  memberLimit: number;
}

export interface SandboxStatus {
  state: SandboxState;
  online: boolean;
  configured: boolean;
  transport: "unix" | "tcp" | "disabled";
  error: { code: string; message: string } | null;
  health: SandboxHealth | null;
  settings: SandboxSettings;
  usage: SandboxUsage;
}

export interface SandboxRunMeta {
  source?: "agent" | "webui" | string;
  groupId?: number | string;
  userId?: number | string;
  purpose?: string;
}

export interface SandboxActiveRun {
  runId: string;
  workspace: string;
  startedAt: number;
  elapsedMs: number;
  meta: SandboxRunMeta;
  limits: Record<string, number>;
  phase: "queued" | "running" | string;
  groupId?: string | null;
  groupName?: string | null;
}

export interface SandboxRunSummary {
  runId: string;
  workspace: string;
  status: SandboxRunStatus;
  exitCode: number | null;
  signal: string | null;
  startedAt: number;
  durationMs: number;
  cpuMs: number;
  maxRssKb: number;
  message: string;
  meta: SandboxRunMeta;
  codeLines: number;
  preview: string;
  fileCount: number;
  groupId?: string | null;
  groupName?: string | null;
}

export interface SandboxRunFile {
  path: string;
  size: number;
  kind: SandboxFileKind;
  created?: boolean;
  mime?: string;
}

export interface SandboxRunDetail extends SandboxRunSummary {
  finishedAt?: number;
  queueMs: number;
  limits: Record<string, number>;
  files: SandboxRunFile[];
  code: string;
  codeTruncated: boolean;
  stdout: string;
  stderr: string;
}

/** 控制台直接执行的结果（与运行详情相似，但输出未经历史截断）。 */
export interface SandboxRunResult {
  runId: string;
  workspace: string;
  status: SandboxRunStatus;
  exitCode: number | null;
  signal: string | null;
  stdout: string;
  stderr: string;
  stdoutTruncated: boolean;
  stderrTruncated: boolean;
  startedAt: number;
  durationMs: number;
  queueMs: number;
  cpuMs: number;
  maxRssKb: number;
  files: SandboxRunFile[];
  filesTruncated: boolean;
  message: string;
  limits: Record<string, number>;
  usage: { bytes?: number; files?: number };
}

export interface SandboxRunsPage {
  items: SandboxRunSummary[];
  active: SandboxActiveRun[];
  nextBefore: number | null;
}

export interface SandboxStats {
  resources: {
    memory: { current: number | null; max: number | null; peak: number | null };
    pids: { current: number | null; max: number | null };
    cpu: { usageUsec: number | null; quotaCores: number | null; count: number | null; load: number[] | null };
    disk: { total: number; used: number; free: number } | null;
    uptimeSeconds: number;
  };
  workspaces: { count: number; bytes: number; files: number };
  runs: {
    windowSeconds: number;
    total: number;
    byStatus: Record<string, number>;
    byWorkspace: Record<string, number>;
    durationP50Ms: number;
    durationP95Ms: number;
    hourly: number[];
    hourlyErrors: number[];
  };
  active: SandboxActiveRun[];
  lastPrune: { at: number; removed: number; retentionDays: number } | null;
  topWorkspaces: Array<{ workspace: string; runs: number; groupId?: string | null; groupName?: string | null }>;
}

export interface SandboxWorkspace {
  id: string;
  bytes: number;
  files: number;
  lastModified: number | null;
  running: boolean;
  groupId?: string | null;
  groupName?: string | null;
}

export interface SandboxFileEntry {
  path: string;
  size: number;
  mtime: number;
  kind: SandboxFileKind;
  mime: string;
}

export interface SandboxFileListing {
  items: SandboxFileEntry[];
  total: number;
  usage: { bytes: number; files: number };
}

export interface SandboxPackages {
  python: string;
  implementation: string;
  highlights: Array<{ name: string; version: string }>;
  total: number;
  all: Record<string, string>;
}
