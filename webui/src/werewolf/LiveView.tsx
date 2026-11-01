// 实时对局列表：每局一张卡片（阶段进度、座位缩略、存活、告警），点卡片进入观战室。
import { EyeOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Popconfirm, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import type { WwGame, WwMeta } from "./types";
import { liveAlerts, phaseInfo, roleInfo } from "./ww-meta";
import { PhasePill, StageProgress } from "./WwBits";

const { Text } = Typography;

export function groupTitle(name: string | null | undefined, groupId: number): string {
  return name || `群 ${groupId}`;
}

export function LiveView({ games, reveal, meta, now, stopping, onOpen, onStop }: {
  games: WwGame[];
  reveal: boolean;
  meta: WwMeta | null;
  now: number;
  stopping: number;
  onOpen: (groupId: number) => void;
  onStop: (groupId: number) => void;
}): React.JSX.Element {
  // 有故障的局排最前，其次按进行阶段（报名中的放后面）
  const sorted = useMemo(() => [...games].sort((left, right) => {
    const leftBad = left.workerAlive ? 0 : 1;
    const rightBad = right.workerAlive ? 0 : 1;
    if (leftBad !== rightBad) return rightBad - leftBad;
    const leftSignup = phaseInfo(left.phase).stage === "signup" ? 1 : 0;
    const rightSignup = phaseInfo(right.phase).stage === "signup" ? 1 : 0;
    return leftSignup - rightSignup || left.groupId - right.groupId;
  }), [games]);
  if (games.length === 0) {
    return (
      <div className="ww2-empty">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="现在没有进行中的狼人杀" />
        <p>在群里发送 <code>/狼人杀</code> 开房，玩家发送 <code>/报名</code> 上车，人齐后房主 <code>/开始游戏</code>。开局后这里会实时出现对局卡片。</p>
      </div>
    );
  }
  return (
    <div className="ww2-live-grid">
      {sorted.map((game) => (
        <LiveCard key={game.groupId} game={game} reveal={reveal} meta={meta} now={now}
          stopping={stopping === game.groupId} onOpen={() => onOpen(game.groupId)} onStop={() => onStop(game.groupId)} />
      ))}
    </div>
  );
}

function LiveCard({ game, reveal, meta, now, stopping, onOpen, onStop }: {
  game: WwGame;
  reveal: boolean;
  meta: WwMeta | null;
  now: number;
  stopping: boolean;
  onOpen: () => void;
  onStop: () => void;
}): React.JSX.Element {
  const info = phaseInfo(game.phase, game.phaseLabel);
  const alerts = liveAlerts(game, { now, timeouts: meta?.phaseTimeouts });
  const signup = info.stage === "signup" && game.players.length === 0;
  const minPlayers = Number(meta?.config.ww_min_players ?? 0) || null;
  const showRole = reveal || game.phase === "ENDED";
  return (
    <article className={`ww2-card${info.stage === "night" ? " is-night" : ""}${game.workerAlive ? "" : " is-broken"}`}>
      <header className="ww2-card-head">
        <button type="button" className="ww2-card-title" onClick={onOpen}>
          <strong>{groupTitle(game.groupName, game.groupId)}</strong>
          <span>{game.groupId} · {game.board ?? "未选板子"}</span>
        </button>
        <PhasePill phase={game.phase} label={game.phaseLabel} reveal={reveal} />
      </header>
      <StageProgress phase={game.phase} />
      {signup ? (
        <div className="ww2-card-signup">
          <Text>已报名 <strong>{game.signupCount}</strong>{minPlayers ? ` / 至少 ${minPlayers}` : ""} 人</Text>
          <div className="ww2-chip-row">
            {game.signup.map((item) => <Tag key={item.userId} color={item.isAi ? "purple" : undefined}>{item.name}{item.isAi ? " · AI" : ""}</Tag>)}
            {game.signup.length === 0 && <Text type="secondary">还没有人报名</Text>}
          </div>
        </div>
      ) : (
        <>
          <div className="ww2-card-seats" aria-label="座位">
            {game.players.map((player) => {
              const role = roleInfo(player.role, player.faction);
              const cls = [
                "ww2-mini-seat",
                player.alive ? "" : "is-dead",
                player.seat === game.currentSpeaker ? "is-speaking" : "",
                showRole ? `is-${role.faction}` : "",
              ].filter(Boolean).join(" ");
              return (
                <Tooltip key={player.userId} title={`${player.seat}号 ${player.name}${player.isAi ? "（AI）" : ""}${showRole ? ` · ${role.role}` : ""}${player.alive ? "" : " · 已出局"}`}>
                  <span className={cls}>{showRole ? role.icon : player.seat}</span>
                </Tooltip>
              );
            })}
          </div>
          <dl className="ww2-card-facts">
            <div><dt>天数</dt><dd>第 {game.roundNo} 天</dd></div>
            <div><dt>存活</dt><dd>{game.aliveCount} / {game.playerCount}</dd></div>
            <div><dt>AI</dt><dd>{game.aiCount}</dd></div>
            <div><dt>发言</dt><dd>{game.currentSpeaker ? `${game.currentSpeaker} 号` : "—"}</dd></div>
          </dl>
        </>
      )}
      {alerts.length > 0 && (
        <div className="ww2-card-alerts">
          {alerts.map((alert) => <Alert key={alert.text} type={alert.level} showIcon banner message={alert.text} />)}
        </div>
      )}
      <footer className="ww2-card-foot">
        <Text type="secondary">房主 {game.hostUserId}</Text>
        <span className="ww2-grow" />
        <Popconfirm
          title="强制结束这局狼人杀？"
          description="与群内 /结束游戏 同一路径，本局不计胜负。"
          okText="强制结束"
          okButtonProps={{ danger: true }}
          onConfirm={onStop}
        >
          <Button size="small" danger icon={<StopOutlined />} loading={stopping}>结束</Button>
        </Popconfirm>
        <Button size="small" type="primary" icon={<EyeOutlined />} onClick={onOpen}>观战</Button>
      </footer>
    </article>
  );
}
