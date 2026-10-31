import { CheckOutlined, CloseOutlined, ReloadOutlined, RollbackOutlined } from "@ant-design/icons";
import {
  App as AntApp,
  Button,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { formatTime } from "../shared";
import type { FeatureState } from "../types";
import {
  ACTIVITY_TONE_LABEL,
  activityTone,
  BATCH_LIMIT,
  batchSummary,
  FEATURE_LABELS,
  featureMode,
  inheritedValue,
  modeToOverride,
  overrideCount,
  relativeTime,
  roleMeta,
  sourceLabel,
} from "./directory-meta";
import type { FeatureMode } from "./directory-meta";

const { Text } = Typography;

export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

// 相对时间 + 悬停看绝对时间。
export function RelativeTime({ value, empty = "从未" }: { value?: string | null; empty?: string }): React.JSX.Element {
  if (!value) return <Text type="secondary">{empty}</Text>;
  return (
    <Tooltip title={formatTime(value)}>
      <time className="dir-time" dateTime={value}>{relativeTime(value)}</time>
    </Tooltip>
  );
}

export function ActivityDot({ value }: { value?: string | null }): React.JSX.Element {
  const tone = activityTone(value);
  return (
    <Tooltip title={ACTIVITY_TONE_LABEL[tone]}>
      <span className={`dir-dot is-${tone}`} role="img" aria-label={ACTIVITY_TONE_LABEL[tone]} />
    </Tooltip>
  );
}

export function RoleTag({ role }: { role: string }): React.JSX.Element {
  const meta = roleMeta(role);
  return <Tag color={meta.color} className="dir-role">{meta.label}</Tag>;
}

export function CopyId({ value, label = "号码" }: { value: string; label?: string }): React.JSX.Element {
  return (
    <span className="dir-id" onClick={(event) => event.stopPropagation()}>
      <Text type="secondary" copyable={{ text: value, tooltips: [`复制${label}`, "已复制"] }}>{value}</Text>
    </span>
  );
}

export interface StatusChip {
  key: string;
  label: React.ReactNode;
  color?: string;
  hint?: string;
  onClick?: () => void;
  active?: boolean;
}

// 页面顶部状态条：沿用代码沙箱的 sbx-status 版式；chip 可点击直接套用筛选。
export function DirectoryStatusBar({
  label,
  title,
  detail,
  tone,
  chips,
  refreshing,
  onRefresh,
}: {
  label: string;
  title: string;
  detail: string;
  tone: "ok" | "warn" | "error";
  chips: StatusChip[];
  refreshing: boolean;
  onRefresh: () => void;
}): React.JSX.Element {
  return (
    <section className={`sbx-status is-${tone} dir-status`} aria-label={label}>
      <div className="sbx-status-main">
        <span className={`sbx-dot is-${tone}`} aria-hidden="true" />
        <div className="sbx-status-copy">
          <strong>{title}</strong>
          <span title={detail}>{detail}</span>
        </div>
      </div>
      <div className="sbx-status-chips">
        {chips.map((chip) => {
          const tag = (
            <Tag
              key={chip.key}
              color={chip.active ? "magenta" : chip.color}
              className={chip.onClick ? "dir-chip is-clickable" : "dir-chip"}
              onClick={chip.onClick}
              role={chip.onClick ? "button" : undefined}
              tabIndex={chip.onClick ? 0 : undefined}
              aria-pressed={chip.onClick ? Boolean(chip.active) : undefined}
              onKeyDown={(event) => {
                if (chip.onClick && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  chip.onClick();
                }
              }}
            >
              {chip.label}
            </Tag>
          );
          return chip.hint ? <Tooltip key={chip.key} title={chip.hint}>{tag}</Tooltip> : tag;
        })}
      </div>
      <div className="sbx-status-actions">
        <Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={onRefresh}>刷新</Button>
      </div>
    </section>
  );
}

const MODE_OPTIONS = (inherited: boolean) => [
  { value: "inherit", label: <Tooltip title={`不单独设置，跟随上一级（现在是${inherited ? "开" : "关"}）`}><span>继承·{inherited ? "开" : "关"}</span></Tooltip> },
  { value: "on", label: <span><CheckOutlined /> 开</span> },
  { value: "off", label: <span><CloseOutlined /> 关</span> },
];

/**
 * 功能覆盖表：群功能、成员覆盖、私聊功能三处共用。
 * 每行一个“继承 / 开 / 关”分段开关，继承选项直接写出继承后是开是关。
 */
export function FeatureTable({
  rows,
  parent,
  onChange,
  onReset,
  resetLabel = "全部恢复继承",
  resetHint,
  confirmOff,
  compact = false,
  emptyHint,
}: {
  rows: FeatureState[];
  parent?: Pick<FeatureState, "key" | "effective">[];
  onChange: (key: string, override: boolean | null) => Promise<void>;
  onReset?: () => Promise<void>;
  resetLabel?: string;
  resetHint?: string;
  confirmOff?: Record<string, string>;
  compact?: boolean;
  emptyHint?: string;
}): React.JSX.Element {
  const { message, modal } = AntApp.useApp();
  const [saving, setSaving] = useState("");
  const [resetting, setResetting] = useState(false);
  const overrides = overrideCount(rows);

  const change = async (row: FeatureState, mode: FeatureMode) => {
    const next = modeToOverride(mode);
    if (next === row.override) return;
    const warning = confirmOff?.[row.key];
    if (warning && next === false) {
      const ok = await modal.confirm({ title: `关闭「${row.name}」？`, content: warning, okText: "关闭", okButtonProps: { danger: true } });
      if (!ok) return;
    }
    setSaving(row.key);
    try {
      await onChange(row.key, next);
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setSaving("");
    }
  };

  return (
    <div className="dir-features">
      <div className="dir-features-head">
        <Text type="secondary">
          {overrides > 0 ? `${overrides} 项单独设置，其余跟随上一级` : emptyHint ?? "全部跟随上一级，没有单独设置"}
        </Text>
        {onReset && (
          <Popconfirm
            title={`${resetLabel}？`}
            description={resetHint}
            disabled={overrides === 0}
            onConfirm={async () => {
              setResetting(true);
              try {
                await onReset();
              } catch (error) {
                message.error((error as Error).message);
              } finally {
                setResetting(false);
              }
            }}
          >
            <Button size="small" icon={<RollbackOutlined />} loading={resetting} disabled={overrides === 0}>{resetLabel}</Button>
          </Popconfirm>
        )}
      </div>
      <Table
        rowKey="key"
        size={compact ? "small" : "middle"}
        pagination={false}
        dataSource={rows}
        rowClassName={(row) => (row.override !== null ? "dir-row-overridden" : "")}
        columns={[
          {
            title: "功能",
            render: (_, row) => (
              <div className="dir-cell-stack">
                <Text strong>{row.name}</Text>
                {!compact && <Text type="secondary" className="dir-mono">{row.key}</Text>}
              </div>
            ),
          },
          {
            title: "现在",
            width: 84,
            render: (_, row) => <Tag color={row.effective ? "green" : "red"}>{row.effective ? "开启" : "关闭"}</Tag>,
          },
          {
            title: "来自",
            width: 110,
            responsive: ["sm"],
            render: (_, row) => <Text type={row.source === "default" ? "secondary" : undefined}>{sourceLabel(row.source)}</Text>,
          },
          {
            title: "设置",
            width: compact ? 210 : 230,
            render: (_, row) => (
              <Segmented
                size="small"
                aria-label={`${row.name} 的设置`}
                disabled={saving === row.key}
                value={featureMode(row.override)}
                options={MODE_OPTIONS(inheritedValue(row.key, parent))}
                onChange={(value) => void change(row, value as FeatureMode)}
              />
            ),
          },
        ]}
      />
    </div>
  );
}

const FEATURE_OPTIONS = Object.entries(FEATURE_LABELS).map(([value, label]) => ({ value, label }));

/**
 * 多选后的批量操作条：选一个功能 + 继承/开/关，确认后一次提交。
 */
export function BatchBar({
  count,
  noun,
  onApply,
  onClear,
  excludeFeatures = [],
}: {
  count: number;
  noun: string;
  onApply: (feature: string, override: boolean | null) => Promise<void>;
  onClear: () => void;
  excludeFeatures?: string[];
}): React.JSX.Element | null {
  const { message } = AntApp.useApp();
  const [feature, setFeature] = useState<string>();
  const [mode, setMode] = useState<FeatureMode>("off");
  const [busy, setBusy] = useState(false);
  const options = useMemo(() => FEATURE_OPTIONS.filter((item) => !excludeFeatures.includes(item.value)), [excludeFeatures]);
  if (count === 0) return null;
  const tooMany = count > BATCH_LIMIT;
  return (
    <div className="dir-batch" role="region" aria-label="批量操作">
      <strong>已选 {count} 个{noun}</strong>
      <Space wrap size={8}>
        <Select
          size="small"
          placeholder="选择功能"
          style={{ minWidth: 130 }}
          value={feature}
          options={options}
          onChange={setFeature}
          aria-label="批量设置的功能"
        />
        <Segmented
          size="small"
          value={mode}
          onChange={(value) => setMode(value as FeatureMode)}
          options={[
            { value: "on", label: "开" },
            { value: "off", label: "关" },
            { value: "inherit", label: "恢复继承" },
          ]}
        />
        <Popconfirm
          title="批量修改"
          description={feature ? `${batchSummary(count, feature, modeToOverride(mode))}？` : undefined}
          disabled={!feature || tooMany}
          onConfirm={async () => {
            if (!feature) return;
            setBusy(true);
            try {
              await onApply(feature, modeToOverride(mode));
              message.success(`已更新 ${count} 个${noun}`);
              onClear();
            } catch (error) {
              message.error((error as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Button size="small" type="primary" loading={busy} disabled={!feature || tooMany}>应用</Button>
        </Popconfirm>
        <Button size="small" type="text" onClick={onClear}>取消选择</Button>
      </Space>
      {tooMany && <Text type="danger">一次最多 {BATCH_LIMIT} 个，请减少选择</Text>}
    </div>
  );
}

export function FilterResetButton({ count, onReset }: { count: number; onReset: () => void }): React.JSX.Element | null {
  if (count === 0) return null;
  return <Button size="small" type="link" onClick={onReset}>清除 {count} 个筛选</Button>;
}

export function Metric({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }): React.JSX.Element {
  return (
    <div className="sbx-metric">
      <span className="sbx-metric-label">{label}</span>
      <span className="sbx-metric-value">{value}</span>
      {hint && <span className="sbx-metric-hint">{hint}</span>}
    </div>
  );
}

// 支持键盘：表格行 Tab 可聚焦、回车打开。
export function clickableRow(onOpen: () => void, label: string): React.HTMLAttributes<HTMLElement> {
  return {
    onClick: (event) => {
      const target = event.target as HTMLElement;
      if (target.closest("a,button,input,label,.ant-checkbox-wrapper,.ant-typography-copy,.ant-dropdown-trigger")) return;
      onOpen();
    },
    onKeyDown: (event) => {
      if (event.key === "Enter" && event.target === event.currentTarget) onOpen();
    },
    tabIndex: 0,
    "aria-label": label,
  };
}
