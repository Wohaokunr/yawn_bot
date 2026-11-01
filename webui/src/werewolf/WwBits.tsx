// 狼人杀管理台共用的小组件：阶段徽标、角色标签、座位徽章、阶段进度、比例条、指标卡。
import { Tooltip } from "antd";
import type { ReactNode } from "react";
import { factionLabel, phaseInfo, publicPhaseInfo, roleInfo, STAGE_FLOW, STAGE_META } from "./ww-meta";

export function PhasePill({ phase, label, reveal = true }: { phase: string | null | undefined; label?: string; reveal?: boolean }): React.JSX.Element {
  const info = publicPhaseInfo(phase, reveal, label);
  const stage = STAGE_META[info.stage];
  return (
    <Tooltip title={info.hint || undefined}>
      <span className={`ww2-pill is-${stage.tone}`} data-stage={info.stage}>
        <span aria-hidden="true">{stage.icon}</span>
        {info.label}
      </span>
    </Tooltip>
  );
}

export function RoleTag({ role, faction, hidden = false }: { role: string | null | undefined; faction?: string | null; hidden?: boolean }): React.JSX.Element {
  if (hidden) return <span className="ww2-role is-hidden" title="身份已隐藏，打开“上帝视角”查看">🔒 隐藏</span>;
  const info = roleInfo(role, faction);
  return (
    <span className={`ww2-role is-${info.faction}`} title={factionLabel(info.faction)}>
      <span aria-hidden="true">{info.icon}</span>{info.role}
    </span>
  );
}

export function SeatNo({ seat, dead = false }: { seat: number; dead?: boolean }): React.JSX.Element {
  return <span className={`ww2-seatno${dead ? " is-dead" : ""}`}>{seat}</span>;
}

/** 一局的主流程进度：报名 → 黑夜 → 天亮 → 警长 → 发言 → 投票 → 结束 */
export function StageProgress({ phase }: { phase: string | null | undefined }): React.JSX.Element {
  const stage = phaseInfo(phase).stage;
  const index = STAGE_FLOW.indexOf(stage);
  return (
    <ol className="ww2-stages" aria-label="对局流程">
      {STAGE_FLOW.map((item, position) => {
        const meta = STAGE_META[item];
        const state = position < index ? "done" : position === index ? "current" : "todo";
        return (
          <li key={item} className={`is-${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="ww2-stage-dot" aria-hidden="true">{meta.icon}</span>
            <span className="ww2-stage-label">{meta.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

export interface BarSegment { value: number; tone: string; label: string }

/** 水平比例条（阵营存活、胜负占比等），无数据时显示灰条 */
export function RatioBar({ segments, height = 10 }: { segments: BarSegment[]; height?: number }): React.JSX.Element {
  const total = segments.reduce((sum, item) => sum + Math.max(0, item.value), 0);
  return (
    <div className="ww2-ratio" style={{ height }} role="img" aria-label={segments.map((item) => `${item.label} ${item.value}`).join("，")}>
      {total === 0 ? <span className="ww2-ratio-empty" /> : segments.filter((item) => item.value > 0).map((item) => (
        <Tooltip key={item.label} title={`${item.label}：${item.value}`}>
          <span className={`ww2-ratio-seg is-${item.tone}`} style={{ flexGrow: item.value }} />
        </Tooltip>
      ))}
    </div>
  );
}

export function Metric({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }): React.JSX.Element {
  return (
    <div className={`ww2-metric${tone ? ` is-${tone}` : ""}`}>
      <span className="ww2-metric-label">{label}</span>
      <strong className="ww2-metric-value">{value}</strong>
      {hint && <span className="ww2-metric-hint">{hint}</span>}
    </div>
  );
}

/** 横向数值条（统计排行用）：value / max 的宽度 + 右侧文字 */
export function MeterRow({ label, value, max, text, tone = "accent", onClick }: { label: ReactNode; value: number; max: number; text: ReactNode; tone?: string; onClick?: () => void }): React.JSX.Element {
  const width = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  const content = (
    <>
      <span className="ww2-meter-label">{label}</span>
      <span className="ww2-meter-track"><span className={`ww2-meter-fill is-${tone}`} style={{ width: `${width}%` }} /></span>
      <span className="ww2-meter-text">{text}</span>
    </>
  );
  return onClick
    ? <button type="button" className="ww2-meter is-link" onClick={onClick}>{content}</button>
    : <div className="ww2-meter">{content}</div>;
}
