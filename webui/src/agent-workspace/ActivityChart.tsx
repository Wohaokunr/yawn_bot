import type { AgentActivity } from "../types";

const WIDTH = 560;
const HEIGHT = 150;
const PAD_BOTTOM = 20;
const PAD_TOP = 8;

function hourLabel(value: string): string {
  const date = new Date(value);
  return `${String(date.getHours()).padStart(2, "0")}:00`;
}

// 24 小时堆叠柱：成员消息在下、Bot 回复叠在上；纯 SVG，避免为一张小图引入图表库。
export function ActivityChart({ activity }: { activity: AgentActivity }): React.JSX.Element {
  const buckets = activity.buckets;
  const max = Math.max(1, ...buckets.map((bucket) => bucket.members + bucket.bot));
  const slot = WIDTH / Math.max(buckets.length, 1);
  const barWidth = Math.max(2, slot * 0.62);
  const plotHeight = HEIGHT - PAD_BOTTOM - PAD_TOP;
  const scale = (value: number) => (value / max) * plotHeight;
  const labelEvery = buckets.length > 72 ? 24 : buckets.length > 12 ? 6 : 2;

  return (
    <svg
      className="agent-activity-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`最近 ${activity.hours} 小时：成员消息 ${activity.totals.members} 条，Bot 回复 ${activity.totals.bot} 条`}
    >
      <line x1={0} x2={WIDTH} y1={HEIGHT - PAD_BOTTOM} y2={HEIGHT - PAD_BOTTOM} className="agent-activity-axis" />
      {buckets.map((bucket, index) => {
        const x = index * slot + (slot - barWidth) / 2;
        const memberHeight = scale(bucket.members);
        const botHeight = scale(bucket.bot);
        const base = HEIGHT - PAD_BOTTOM;
        return (
          <g key={bucket.hour}>
            <title>{`${hourLabel(bucket.hour)} · 成员 ${bucket.members} · Bot ${bucket.bot}`}</title>
            <rect x={index * slot} y={PAD_TOP} width={slot} height={plotHeight} fill="transparent" />
            {bucket.members > 0 && <rect className="agent-activity-members" x={x} y={base - memberHeight} width={barWidth} height={memberHeight} rx={2} />}
            {bucket.bot > 0 && <rect className="agent-activity-bot" x={x} y={base - memberHeight - botHeight} width={barWidth} height={botHeight} rx={2} />}
            {index % labelEvery === 0 && (
              <text x={index * slot + slot / 2} y={HEIGHT - 6} textAnchor="middle" className="agent-activity-label">
                {hourLabel(bucket.hour)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
