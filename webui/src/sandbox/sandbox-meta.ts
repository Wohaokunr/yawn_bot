// 代码沙箱页的纯逻辑：状态文案、格式化、URL 参数与设置校验。不依赖 React，便于单测。
import type {
  SandboxFileKind,
  SandboxRunStatus,
  SandboxSettings,
  SandboxState,
} from "./types";

export const SANDBOX_STATE_META: Record<SandboxState, { label: string; tone: "ok" | "warn" | "error" | "idle"; hint: string }> = {
  ready: { label: "运行中", tone: "ok", hint: "隔离自检通过，群成员可以让 Agent 运行代码" },
  paused: { label: "已暂停", tone: "warn", hint: "沙箱在线，但策略里关闭了总开关" },
  unsafe: { label: "隔离未通过", tone: "error", hint: "沙箱容器仍有网络或无法降权，已拒绝执行" },
  offline: { label: "离线", tone: "error", hint: "Bot 连不上沙箱容器，检查 sandbox 服务是否启动" },
  disabled: { label: "未启用", tone: "idle", hint: "AGENT_SANDBOX_URL 为空，沙箱功能关闭" },
};

export const RUN_STATUS_META: Record<SandboxRunStatus, { label: string; color: string }> = {
  ok: { label: "成功", color: "green" },
  error: { label: "代码报错", color: "red" },
  timeout: { label: "超时", color: "orange" },
  cpu_limit: { label: "CPU 超限", color: "orange" },
  memory_limit: { label: "内存超限", color: "orange" },
  disk_limit: { label: "磁盘超限", color: "orange" },
  output_limit: { label: "输出过多", color: "gold" },
  killed: { label: "被终止", color: "volcano" },
  cancelled: { label: "已取消", color: "default" },
  rejected: { label: "被拒绝", color: "default" },
  busy: { label: "繁忙", color: "default" },
};

export const RUN_STATUS_OPTIONS = (Object.keys(RUN_STATUS_META) as SandboxRunStatus[])
  .filter((key) => key !== "busy")
  .map((value) => ({ value, label: RUN_STATUS_META[value].label }));

export function runStatusMeta(status: string): { label: string; color: string } {
  return RUN_STATUS_META[status as SandboxRunStatus] ?? { label: status || "未知", color: "default" };
}

export const FILE_KIND_META: Record<SandboxFileKind, { label: string; emoji: string }> = {
  image: { label: "图片", emoji: "🖼️" },
  vector: { label: "矢量图", emoji: "📐" },
  audio: { label: "音频", emoji: "🎵" },
  midi: { label: "MIDI", emoji: "🎹" },
  video: { label: "视频", emoji: "🎬" },
  table: { label: "表格", emoji: "📊" },
  text: { label: "文本", emoji: "📄" },
  document: { label: "文档", emoji: "📑" },
  archive: { label: "压缩包", emoji: "📦" },
  other: { label: "其他", emoji: "📎" },
};

export function fileKindMeta(kind: string): { label: string; emoji: string } {
  return FILE_KIND_META[kind as SandboxFileKind] ?? FILE_KIND_META.other;
}

const BINARY_TABLE_SUFFIXES = [".xlsx", ".xls", ".parquet"];

/** 能否在页面里以文本预览（与后端 preview=1 的判断一致）。 */
export function canPreviewText(path: string, kind: string): boolean {
  if (!["text", "table", "vector"].includes(kind)) return false;
  const lower = path.toLowerCase();
  return !BINARY_TABLE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

/** 浏览器能直接内联展示的媒体（其余只能下载）。 */
export function inlineMediaKind(path: string, kind: string): "image" | "audio" | "video" | null {
  const lower = path.toLowerCase();
  if (kind === "image" && /\.(png|jpe?g|gif|webp)$/.test(lower)) return "image";
  if (kind === "audio" && /\.(wav|mp3|ogg|flac)$/.test(lower)) return "audio";
  if (kind === "video" && /\.(mp4|webm)$/.test(lower)) return "video";
  return null;
}

export function formatBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

export function formatDuration(ms?: number | null): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} 分 ${seconds} 秒`;
}

export function formatUptime(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${Math.max(minutes, 0)} 分钟`;
}

/** runner 的时间戳是 Unix 秒。 */
export function formatEpoch(seconds?: number | null): string {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function relativeEpoch(seconds?: number | null, now: number = Date.now()): string {
  if (!seconds) return "—";
  const diff = Math.max(0, now / 1000 - seconds);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86_400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86_400)} 天前`;
}

export function workspaceLabel(workspace: string, groupName?: string | null): string {
  if (workspace === "admin") return "管理员控制台";
  const match = /^g(\d+)$/.exec(workspace);
  if (match) return groupName ? `${groupName}（${match[1]}）` : `群 ${match[1]}`;
  return workspace;
}

export function runSourceLabel(source?: string): string {
  if (source === "agent") return "Agent";
  if (source === "webui") return "控制台";
  return source || "未知";
}

// ── URL 状态：统一 sandbox.* 前缀，刷新或分享链接时保持上下文 ─────────────

export const SANDBOX_TABS = ["overview", "runs", "files", "console", "policy"] as const;
export type SandboxTab = (typeof SANDBOX_TABS)[number];

export function parseSandboxTab(value: string | null): SandboxTab {
  return (SANDBOX_TABS as readonly string[]).includes(value ?? "") ? (value as SandboxTab) : "overview";
}

export function updateSearchParams(
  current: URLSearchParams,
  patch: Record<string, string | null | undefined>,
): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const [key, value] of Object.entries(patch)) {
    if (value == null || value === "") next.delete(key);
    else next.set(key, value);
  }
  return next;
}

// ── 设置校验：与后端 SandboxSettings 的范围一致 ───────────────────────────

export const SETTING_RANGES: Record<
  "memberDailyRuns" | "groupDailyRuns" | "memberCooldownSeconds" | "timeoutSeconds" | "memoryMb" | "diskQuotaMb" | "maxOutputChars" | "maxShareMb",
  { min: number; max: number; unit: string; label: string }
> = {
  memberDailyRuns: { min: 0, max: 1000, unit: "次/天", label: "每位成员每日次数" },
  groupDailyRuns: { min: 0, max: 10000, unit: "次/天", label: "每个群每日次数" },
  memberCooldownSeconds: { min: 0, max: 600, unit: "秒", label: "成员冷却" },
  timeoutSeconds: { min: 5, max: 90, unit: "秒", label: "单次时限" },
  memoryMb: { min: 128, max: 4096, unit: "MB", label: "内存上限" },
  diskQuotaMb: { min: 10, max: 4096, unit: "MB", label: "每群工作区空间" },
  maxOutputChars: { min: 500, max: 12000, unit: "字", label: "回给模型的输出" },
  maxShareMb: { min: 1, max: 32, unit: "MB", label: "单个产物发送上限" },
};

const GROUP_ID_RE = /^\d{4,12}$/;

/** 把文本框里的群号（空格/逗号/换行分隔）解析成去重列表，并返回不合法的项。 */
export function parseGroupList(text: string): { ids: string[]; invalid: string[] } {
  const ids: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,，、;；]+/)) {
    const value = raw.trim();
    if (!value) continue;
    if (!GROUP_ID_RE.test(value)) invalid.push(value);
    else if (!ids.includes(value)) ids.push(value);
  }
  return { ids, invalid };
}

export function validateSettings(settings: SandboxSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, range] of Object.entries(SETTING_RANGES)) {
    const value = settings[key as keyof typeof SETTING_RANGES];
    if (!Number.isInteger(value)) errors[key] = `${range.label}必须是整数`;
    else if (value < range.min || value > range.max) errors[key] = `${range.label}应在 ${range.min}–${range.max} ${range.unit}`;
  }
  if (settings.groupMode === "allowlist" && settings.allowedGroups.length === 0) {
    errors.allowedGroups = "白名单模式下至少填一个群号，否则所有群都用不了";
  }
  if (settings.groupDailyRuns > 0 && settings.memberDailyRuns > settings.groupDailyRuns) {
    errors.memberDailyRuns = "成员每日次数不应超过群每日次数";
  }
  for (const key of ["allowedGroups", "blockedGroups"] as const) {
    const bad = settings[key].filter((item) => !GROUP_ID_RE.test(item));
    if (bad.length) errors[key] = `不合法的群号：${bad.slice(0, 3).join("、")}`;
  }
  return errors;
}

export function settingsEqual(left: SandboxSettings, right: SandboxSettings): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

/** 描述“谁能用”的一句话，策略页顶部和状态条共用。 */
export function describeAccess(settings: SandboxSettings): string {
  if (!settings.enabled) return "已关闭：所有群都不能运行代码";
  const who = settings.adminOnly ? "群主/管理员" : "所有群成员";
  const where = settings.groupMode === "allowlist"
    ? `${settings.allowedGroups.length} 个白名单群`
    : settings.blockedGroups.length
      ? `除 ${settings.blockedGroups.length} 个禁用群外的所有群`
      : "所有群";
  return `${where}的${who}可用，每人每天 ${settings.memberDailyRuns} 次`;
}

// ── 控制台示例 ────────────────────────────────────────────────────────────

export const CONSOLE_SNIPPETS: Array<{ key: string; label: string; code: string }> = [
  {
    key: "hello",
    label: "环境自检",
    code: [
      "import sys, platform",
      "import numpy as np, pandas as pd, matplotlib",
      "print(platform.python_version(), np.__version__, pd.__version__, matplotlib.__version__)",
      "sys.version_info[:3]",
    ].join("\n"),
  },
  {
    key: "chart",
    label: "画一张图",
    code: [
      "import numpy as np",
      "import matplotlib.pyplot as plt",
      "",
      "x = np.linspace(0, 4 * np.pi, 400)",
      "plt.figure(figsize=(7, 3.5))",
      "plt.plot(x, np.sin(x), label='sin')",
      "plt.plot(x, np.cos(x), label='cos')",
      "plt.title('三角函数')",
      "plt.legend()",
      "plt.show()  # 自动保存为 PNG",
    ].join("\n"),
  },
  {
    key: "table",
    label: "数据分析",
    code: [
      "import pandas as pd",
      "from yawnkit import files",
      "",
      "df = pd.DataFrame({'成员': ['甲', '乙', '丙', '丁'], '发言': [120, 87, 45, 230]})",
      "df['占比'] = (df['发言'] / df['发言'].sum()).round(3)",
      "files.save_table(df, '发言统计.xlsx')",
      "df.sort_values('发言', ascending=False)",
    ].join("\n"),
  },
  {
    key: "music",
    label: "作一段曲",
    code: [
      "from yawnkit import music",
      "",
      "song = music.Song(bpm=96)",
      "song.track('E4 G4 A4:2 G4 E4 D4:2 C4 D4 E4 G4 E4:4', instrument='piano')",
      "song.track(music.progression('C Am F G'), instrument='pad', volume=0.35)",
      "song.save('demo.wav')",
      "song.save_midi('demo.mid')",
    ].join("\n"),
  },
];

// ── 限额展示 ──────────────────────────────────────────────────────────────

const LIMIT_LABELS: Record<string, (value: number) => string> = {
  timeoutSeconds: (value) => `时限 ${value}s`,
  cpuSeconds: (value) => `CPU ${value}s`,
  memoryMb: (value) => `内存 ${value}MB`,
  outputBytes: (value) => `输出 ${formatBytes(value)}`,
  fileMb: (value) => `单文件 ${value}MB`,
  processes: (value) => `进程 ${value}`,
  openFiles: (value) => `句柄 ${value}`,
  diskQuotaMb: (value) => `磁盘 ${value}MB`,
  maxFiles: (value) => `文件数 ${value}`,
};

export function formatLimit(key: string, value: number): string {
  return LIMIT_LABELS[key]?.(value) ?? `${key} ${value}`;
}
