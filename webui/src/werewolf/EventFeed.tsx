// 观战室时间线：类型筛选（带计数）、座位/天数筛选、全文搜索、按天分组、
// 最新在上/在下切换；AI 决策默认折叠，上下文按需展开。
import { Button, Collapse, Empty, Input, Segmented, Select, Tag, Tooltip, Typography } from "antd";
import { useMemo, useState } from "react";
import type { WwEvent, WwPlayer } from "./types";
import { clockTime, deathLabel, EVENT_META, FEED_FILTERS, filterCounts, filterEvents, groupByRound, phaseInfo, publicPhaseInfo, roundLabel } from "./ww-meta";
import type { FeedFilter } from "./ww-meta";
import { RoleTag, SeatNo } from "./WwBits";

const { Text } = Typography;
const PAGE = 120;

export function EventFeed({ events, seat, onSeat, players, showRole }: {
  events: WwEvent[];
  seat: number | null;
  onSeat: (seat: number | null) => void;
  players: WwPlayer[];
  showRole: boolean;
}): React.JSX.Element {
  const [filter, setFilter] = useState<FeedFilter>("all");
  const [text, setText] = useState("");
  const [round, setRound] = useState<number | null>(null);
  const [newestFirst, setNewestFirst] = useState(true);
  const [limit, setLimit] = useState(PAGE);
  // 没开上帝视角时不展示 AI 思考（里面有身份和刀口），时间线只剩公开信息
  const visible = useMemo(() => (showRole ? events : events.filter((event) => event.type !== "ai_decision" && event.type !== "ai_speech")), [events, showRole]);
  const hiddenAi = events.length - visible.length;
  const counts = useMemo(() => filterCounts(visible), [visible]);
  const rounds = useMemo(() => [...new Set(visible.map((event) => event.roundNo).filter((value): value is number => value !== null))].sort((a, b) => a - b), [visible]);
  const shown = useMemo(() => {
    const list = filterEvents(visible, { filter, seat, round, text });
    return newestFirst ? list.slice().reverse() : list;
  }, [visible, filter, seat, round, text, newestFirst]);
  const groups = groupByRound(shown.slice(0, limit));
  const anyFilter = filter !== "all" || seat !== null || round !== null || text.trim() !== "";
  return (
    <div className="ww2-feed">
      <div className="ww2-feed-toolbar">
        <Segmented
          size="small"
          value={filter}
          onChange={(value) => { setFilter(value as FeedFilter); setLimit(PAGE); }}
          options={FEED_FILTERS.filter((item) => showRole || item.value !== "ai").map((item) => ({ value: item.value, label: `${item.label} ${counts[item.value]}` }))}
        />
        <div className="ww2-feed-filters">
          <Input.Search size="small" allowClear placeholder="搜索发言、播报、AI 指令" value={text} onChange={(event) => setText(event.target.value)} style={{ maxWidth: 240 }} />
          <Select size="small" allowClear placeholder="座位" value={seat ?? undefined} onChange={(value) => onSeat(value ?? null)} style={{ width: 110 }}
            options={players.map((player) => ({ value: player.seat, label: `${player.seat}号 ${player.name}` }))} />
          <Select size="small" allowClear placeholder="天数" value={round ?? undefined} onChange={(value) => setRound(value ?? null)} style={{ width: 96 }}
            options={rounds.map((value) => ({ value, label: roundLabel(value) }))} />
          <Tooltip title="切换排序">
            <Button size="small" onClick={() => setNewestFirst((value) => !value)}>{newestFirst ? "最新在上" : "最早在上"}</Button>
          </Tooltip>
          {anyFilter && <Button size="small" type="link" onClick={() => { setFilter("all"); setText(""); setRound(null); onSeat(null); }}>清除筛选</Button>}
        </div>
      </div>
      {hiddenAi > 0 && <div className="ww2-muted">已隐藏 {hiddenAi} 条 AI 思考记录（含身份与夜间行动），打开“上帝视角”后显示。</div>}
      {shown.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={events.length === 0 ? "还没有事件，开局后这里实时滚动" : "没有符合筛选条件的事件"} />
      ) : (
        <div className="ww2-feed-list">
          {groups.map((group, index) => (
            <section key={`${group.round}-${index}`} className="ww2-feed-day">
              <h5 className="ww2-feed-dayhead">{roundLabel(group.round)}</h5>
              {group.events.map((event) => <FeedItem key={event.seq} event={event} players={players} showRole={showRole} onSeat={onSeat} />)}
            </section>
          ))}
          {shown.length > limit && <Button block onClick={() => setLimit((value) => value + PAGE)}>再显示 {Math.min(PAGE, shown.length - limit)} 条（共 {shown.length} 条）</Button>}
        </div>
      )}
    </div>
  );
}

function FeedItem({ event, players, showRole, onSeat }: { event: WwEvent; players: WwPlayer[]; showRole: boolean; onSeat: (seat: number) => void }): React.JSX.Element {
  const meta = EVENT_META[event.type] ?? { label: event.type, tone: "gray" };
  if (event.type === "phase") {
    return (
      <div className="ww2-feed-phase">
        <span>{publicPhaseInfo(event.phase ?? event.text, showRole).label}</span>
        <time>{clockTime(event.ts)}</time>
      </div>
    );
  }
  const player = event.seat !== null ? players.find((item) => item.seat === event.seat) : undefined;
  return (
    <article className={`ww2-feed-item is-${event.type}`}>
      <header>
        <span className={`ww2-dot is-${meta.tone}`} aria-hidden="true" />
        <Tag bordered={false} className={`ww2-type is-${meta.tone}`}>{meta.label}</Tag>
        {event.seat !== null && (
          <button type="button" className="ww2-feed-seat" onClick={() => onSeat(event.seat as number)} title="只看这个座位">
            <SeatNo seat={event.seat} dead={player ? !player.alive : false} /> {event.name ?? player?.name ?? ""}
          </button>
        )}
        {(event.type === "speech" && event.extra.isAi) && <Tag color="purple" bordered={false}>AI</Tag>}
        {showRole && player && event.type !== "death" && <RoleTag role={player.role} faction={player.faction} />}
        <span className="ww2-grow" />
        <time title={event.ts}>{clockTime(event.ts)}</time>
      </header>
      <FeedBody event={event} showRole={showRole} />
    </article>
  );
}

function FeedBody({ event, showRole }: { event: WwEvent; showRole: boolean }): React.JSX.Element {
  if (event.type === "vote_tally") {
    const votes = event.extra.votes ?? [];
    if (votes.length === 0) return <Text type="secondary">无人投票</Text>;
    return (
      <div className="ww2-chip-row">
        <Text type="secondary">{phaseInfo(event.phase).label}：</Text>
        {votes.map((vote) => (
          <Tag key={vote.voterSeat} color={vote.targetSeat === null ? undefined : "gold"} bordered={false}>
            {vote.voterSeat} → {vote.targetSeat === null ? "弃票" : `${vote.targetSeat}`}{vote.isSheriff ? " ×1.5" : ""}
          </Tag>
        ))}
      </div>
    );
  }
  if (event.type === "death") {
    return (
      <div className="ww2-chip-row">
        <span>{event.text}</span>
        <Tag color="red" bordered={false}>{deathLabel(event.extra.deathCause)}</Tag>
        {event.extra.role && <RoleTag role={event.extra.role} hidden={!showRole} />}
      </div>
    );
  }
  if (event.type === "ai_decision" || event.type === "ai_speech") {
    const action = event.extra.action;
    const empty = !(event.text ?? "").trim();
    return (
      <div className="ww2-ai">
        {event.extra.instruction && <div className="ww2-ai-instruction">指令：{event.extra.instruction}</div>}
        {event.extra.scene && <div className="ww2-ai-instruction">场景：{String(event.extra.scene)}</div>}
        <div className={`ww2-ai-reply${empty ? " is-empty" : ""}`}>{empty ? "（空回复，已走降级默认）" : event.text}</div>
        <div className="ww2-chip-row">
          {event.type === "ai_decision" && (action
            ? <Tag color={action.value === null ? undefined : "green"} bordered={false}>解析为 {action.kind}{action.value !== null ? ` → ${action.value} 号` : ""}</Tag>
            : <Tag color="orange" bordered={false}>未能解析成行动</Tag>)}
          {event.extra.attempt !== undefined && event.extra.attempt > 1 && <Tag color="orange" bordered={false}>第 {event.extra.attempt} 次纠正</Tag>}
        </div>
        {event.extra.context && (
          <Collapse size="small" ghost items={[{ key: "ctx", label: <Text type="secondary">喂给 AI 的局势快照（{event.extra.context.length} 字）</Text>, children: <pre className="ww2-pre">{event.extra.context}</pre> }]} />
        )}
      </div>
    );
  }
  if (event.type === "speech") return <p className="ww2-speech">{event.text ?? "—"}</p>;
  return <p className="ww2-feed-text">{event.text ?? "—"}</p>;
}
