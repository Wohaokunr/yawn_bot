import { BarChartOutlined, ClearOutlined, DiffOutlined, ExperimentOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import { Button, Modal, Popconfirm, Segmented, Tooltip, Typography } from "antd";
import type { AgentExecutionTraceStats } from "../types";
import { formatDuration } from "./trace-analysis";

const { Text } = Typography;

export type DebugView = "trace" | "lab" | "compare";

export const DEBUG_VIEWS: Array<{ value: DebugView; label: string; icon: React.ReactNode; key: string }> = [
  { value: "trace", label: "Trace 分析", icon: <BarChartOutlined />, key: "t" },
  { value: "lab", label: "模拟实验室", icon: <ExperimentOutlined />, key: "l" },
  { value: "compare", label: "Trace 对比", icon: <DiffOutlined />, key: "c" },
];

export function DebugStatsStrip({ stats, error }: { stats: AgentExecutionTraceStats | null; error: string }): React.JSX.Element {
  if (!stats) {
    return <div className="dbg-stats is-empty">{error ? <Text type="secondary">统计暂不可用：{error}</Text> : <Text type="secondary">正在汇总 Trace 缓冲…</Text>}</div>;
  }
  const failRate = stats.total > 0 ? Math.round((stats.failed / stats.total) * 100) : 0;
  const toolRate = stats.total > 0 ? Math.round((stats.withTools / stats.total) * 100) : 0;
  const topPhase = stats.phases[0];
  const items: Array<{ key: string; label: string; value: string; hint: string; tone?: string }> = [
    { key: "buffer", label: "缓冲", value: `${stats.total} / ${stats.capacity}`, hint: stats.running > 0 ? `${stats.running} 条执行中` : "最近执行" },
    { key: "failed", label: "失败", value: String(stats.failed), hint: stats.total > 0 ? `失败率 ${failRate}%` : "—", tone: stats.failed > 0 ? "error" : undefined },
    { key: "warn", label: "降级 / 发送异常", value: `${stats.degraded} / ${stats.outboundProblems}`, hint: stats.withMedia > 0 ? `${stats.withMedia} 条带媒体` : "无媒体", tone: stats.degraded + stats.outboundProblems > 0 ? "warning" : undefined },
    { key: "avg", label: "平均耗时", value: formatDuration(stats.durationMs.avg), hint: `中位 ${formatDuration(stats.durationMs.p50)}` },
    { key: "p95", label: "P95 耗时", value: formatDuration(stats.durationMs.p95), hint: `最长 ${formatDuration(stats.durationMs.max)}` },
    { key: "tools", label: "带工具", value: `${toolRate}%`, hint: stats.tools[0] ? `最常用 ${stats.tools[0].name}` : "没有工具调用" },
    { key: "phase", label: "最耗时阶段", value: topPhase ? (PHASE_SHORT[topPhase.phase] ?? topPhase.phase) : "—", hint: topPhase?.avgMs != null ? `平均 ${formatDuration(topPhase.avgMs)}` : "—" },
  ];
  return <div className="dbg-stats" aria-label="Trace 缓冲统计">
    {items.map((item) => <div key={item.key} className={`dbg-stat${item.tone ? ` is-${item.tone}` : ""}`}>
      <span className="dbg-kpi-label">{item.label}</span>
      <strong className="dbg-stat-value">{item.value}</strong>
      <span className="dbg-kpi-hint" title={item.hint}>{item.hint}</span>
    </div>)}
  </div>;
}

const PHASE_SHORT: Record<string, string> = {
  parse: "解析",
  intake: "输入",
  policy: "策略",
  context: "上下文",
  capability: "能力",
  media: "媒体",
  prompt: "Prompt",
  llm: "模型",
  tool: "工具",
  outbound: "发送",
  state: "状态",
  turn: "回合",
};

export function DebugHeader({
  view,
  onViewChange,
  stats,
  statsError,
  onShowShortcuts,
  onClearBuffer,
  clearing,
}: {
  view: DebugView;
  onViewChange: (view: DebugView) => void;
  stats: AgentExecutionTraceStats | null;
  statsError: string;
  onShowShortcuts: () => void;
  onClearBuffer: () => void;
  clearing: boolean;
}): React.JSX.Element {
  return <div className="dbg-header">
    <div className="dbg-header-bar">
      <Segmented
        className="dbg-view-switch"
        value={view}
        onChange={(value) => onViewChange(value as DebugView)}
        options={DEBUG_VIEWS.map((item) => ({ value: item.value, label: <span className="dbg-view-option">{item.icon}<span>{item.label}</span></span> }))}
        aria-label="调试台视图"
      />
      <div className="dbg-header-actions">
        <Text type="secondary" className="dbg-safety">全部只读 · 模拟不执行副作用</Text>
        <Popconfirm
          title="清空本群的 Trace 缓冲？"
          description="只清理 Bot 进程内存里的诊断记录，不影响数据库、审计和 Agent 状态。"
          okText="清空"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={onClearBuffer}
          disabled={!stats || stats.total === 0}
        >
          <Button icon={<ClearOutlined />} loading={clearing} disabled={!stats || stats.total === 0}>清空缓冲</Button>
        </Popconfirm>
        <Tooltip title="快捷键（?）">
          <Button aria-label="查看快捷键" icon={<QuestionCircleOutlined />} onClick={onShowShortcuts} />
        </Tooltip>
      </div>
    </div>
    <DebugStatsStrip stats={stats} error={statsError} />
  </div>;
}

export const SHORTCUTS: Array<{ keys: string[]; label: string }> = [
  { keys: ["j", "↓"], label: "下一条 Trace" },
  { keys: ["k", "↑"], label: "上一条 Trace" },
  { keys: ["/"], label: "搜索 Trace" },
  { keys: ["r"], label: "刷新列表与统计" },
  { keys: ["p"], label: "暂停 / 继续自动刷新" },
  { keys: ["t"], label: "切到 Trace 分析" },
  { keys: ["l"], label: "切到模拟实验室" },
  { keys: ["c"], label: "切到 Trace 对比" },
  { keys: ["Ctrl", "Enter"], label: "在实验室运行一次" },
  { keys: ["?"], label: "打开这个帮助" },
];

export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }): React.JSX.Element {
  return <Modal open={open} onCancel={onClose} footer={null} title="调试台快捷键" width={420}>
    <ul className="dbg-shortcuts">
      {SHORTCUTS.map((item) => <li key={item.label}>
        <span>{item.label}</span>
        <span className="dbg-keys">{item.keys.map((key) => <Text key={key} keyboard>{key}</Text>)}</span>
      </li>)}
    </ul>
    <Text type="secondary" className="dbg-small">在输入框里打字时快捷键不会触发；Alt + 数字仍然用来切换 Agent 工作台的页签。</Text>
  </Modal>;
}
