// 人物关系用的小型 SVG 图表：分布条、柱状图、健康分环、迷你走势。
// 不引入图表库，配色走 CSS 变量与关系类型色，深浅两套主题下都清晰。
import { Tooltip } from "antd";
import { relationTypeColor } from "../relation-meta";

export interface DistributionItem {
  key: string;
  label: string;
  count: number;
  color?: string;
}

// 横向堆叠分布条：每段可点击（例如按类型筛选），宽度按占比，过窄的段只在 tooltip 里给数。
export function DistributionBar({
  items,
  active,
  onSelect,
  height = 10,
  ariaLabel,
}: {
  items: DistributionItem[];
  active?: string;
  onSelect?: (key: string) => void;
  height?: number;
  ariaLabel: string;
}): React.JSX.Element {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  if (total === 0) return <div className="rel-dist-bar is-empty" style={{ height }} aria-label={`${ariaLabel}：暂无数据`} />;
  return (
    <div className="rel-dist-bar" style={{ height }} role="list" aria-label={ariaLabel}>
      {items.filter((item) => item.count > 0).map((item) => {
        const percent = (item.count / total) * 100;
        const color = item.color ?? relationTypeColor(item.key);
        const dimmed = active && active !== item.key;
        return (
          <Tooltip key={item.key} title={`${item.label} · ${item.count} 条 · ${percent.toFixed(0)}%`}>
            <button
              type="button"
              role="listitem"
              className={`rel-dist-seg${dimmed ? " is-dimmed" : ""}${onSelect ? " is-clickable" : ""}`}
              style={{ width: `${percent}%`, background: color }}
              aria-label={`${item.label} ${item.count} 条`}
              onClick={onSelect ? () => onSelect(item.key) : undefined}
              tabIndex={onSelect ? 0 : -1}
            />
          </Tooltip>
        );
      })}
    </div>
  );
}

// 竖向柱状图（置信度直方图、每周活跃等）。
export function MiniBars({
  values,
  labels,
  color = "var(--brand)",
  height = 72,
  highlightLast = false,
  formatTip,
  ariaLabel,
}: {
  values: number[];
  labels: string[];
  color?: string | ((index: number) => string);
  height?: number;
  highlightLast?: boolean;
  formatTip?: (value: number, index: number) => string;
  ariaLabel: string;
}): React.JSX.Element {
  const max = Math.max(1, ...values);
  return (
    <div className="rel-minibars" style={{ height }} role="img" aria-label={ariaLabel}>
      {values.map((value, index) => {
        const fill = typeof color === "function" ? color(index) : color;
        return (
          <Tooltip key={labels[index] ?? index} title={formatTip ? formatTip(value, index) : `${labels[index]}：${value}`}>
            <div className="rel-minibar-col">
              <div
                className={`rel-minibar${highlightLast && index === values.length - 1 ? " is-current" : ""}`}
                style={{ height: `${Math.max(value > 0 ? 6 : 2, (value / max) * 100)}%`, background: value > 0 ? fill : "var(--border)" }}
              />
            </div>
          </Tooltip>
        );
      })}
    </div>
  );
}

// 环形分数：0–100，按分段着色。
export function ScoreRing({ score, size = 112, label }: { score: number; size?: number; label: string }): React.JSX.Element {
  const radius = size / 2 - 9;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.min(Math.max(score, 0), 100);
  const color = clamped >= 75 ? "var(--rel-good)" : clamped >= 50 ? "var(--rel-warn)" : "var(--rel-bad)";
  return (
    <svg className="rel-score-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`${label} ${clamped} 分`}>
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--border-soft)" strokeWidth={9} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={color}
        strokeWidth={9}
        strokeLinecap="round"
        strokeDasharray={`${(clamped / 100) * circumference} ${circumference}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        className="rel-score-ring-arc"
      />
      <text x="50%" y="47%" textAnchor="middle" dominantBaseline="central" className="rel-score-ring-value">{clamped}</text>
      <text x="50%" y="66%" textAnchor="middle" dominantBaseline="central" className="rel-score-ring-label">{label}</text>
    </svg>
  );
}

// 单值进度条（因子评分、类型平均置信度等）。
export function Meter({ value, color, label }: { value: number; color?: string; label: string }): React.JSX.Element {
  const percent = Math.round(Math.min(Math.max(value, 0), 1) * 100);
  return (
    <div className="rel-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={label}>
      <div className="rel-meter-fill" style={{ width: `${percent}%`, background: color ?? "var(--brand)" }} />
    </div>
  );
}
