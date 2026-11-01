// 狼人杀管理台 2.0 入口：状态条 + 四个视图（实时对局 / 战绩 / 数据洞察 / 规则与配置），
// 点开一局进入观战室。所有可分享的状态都写在 URL（ww.*），上帝视角开关存在本机。
import { BarChartOutlined, BookOutlined, EyeInvisibleOutlined, EyeOutlined, HistoryOutlined, ReloadOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Badge, Button, Segmented, Switch, Tag, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { updateSearchParams } from "../sandbox/sandbox-meta";
import { useApiQuery } from "../shared";
import type { WwGame } from "./types";
import { GameRoom, parseRoomTab } from "./GameRoom";
import type { RoomTab } from "./GameRoom";
import { HistoryView } from "./HistoryView";
import type { HistoryFilters } from "./HistoryView";
import { InsightsView } from "./InsightsView";
import { LiveView } from "./LiveView";
import { RulesView } from "./RulesView";
import { wwApi } from "./ww-api";
import { liveAlerts, parseIntParam, parseView, phaseInfo } from "./ww-meta";
import type { WwView } from "./ww-meta";

export const REVEAL_KEY = "yawnbot.ww.reveal";

function readReveal(): boolean {
  try { return window.localStorage.getItem(REVEAL_KEY) === "1"; } catch { return false; }
}

function writeReveal(value: boolean): void {
  try { window.localStorage.setItem(REVEAL_KEY, value ? "1" : "0"); } catch { /* 隐私模式下忽略 */ }
}

export function WerewolfWorkspace({ available, games, onChanged, refreshing }: {
  available: boolean;
  games: WwGame[];
  onChanged: () => void;
  refreshing: boolean;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [params, setParams] = useSearchParams();
  const [reveal, setRevealState] = useState(readReveal);
  const [stopping, setStopping] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, []);
  const metaQuery = useApiQuery({ queryKey: ["ww-meta"], fetcher: () => wwApi.meta() });
  const meta = metaQuery.data;
  const setReveal = (value: boolean) => { setRevealState(value); writeReveal(value); };
  const patch = useCallback((values: Record<string, string | null | undefined>, replace = false) => {
    setParams((current) => updateSearchParams(current, values), { replace });
  }, [setParams]);

  const view = parseView(params.get("ww.view"));
  const gameId = parseIntParam(params.get("ww.game"));
  const roomTab = parseRoomTab(params.get("ww.tab"));
  const roomSeat = parseIntParam(params.get("ww.seat"));
  const history: HistoryFilters = {
    page: Math.max(1, parseIntParam(params.get("ww.page")) ?? 1),
    search: params.get("ww.q") ?? "",
    status: params.get("ww.status") ?? "all",
    board: params.get("ww.board") ?? "",
    winner: params.get("ww.winner") ?? "",
    groupId: parseIntParam(params.get("ww.group")),
    userId: parseIntParam(params.get("ww.user")),
    game: parseIntParam(params.get("ww.detail")),
  };
  const days = parseIntParam(params.get("ww.days")) ?? 30;
  const statsBoard = params.get("ww.sboard") ?? "";

  const counts = useMemo(() => {
    let night = 0;
    let day = 0;
    let signup = 0;
    let broken = 0;
    for (const game of games) {
      const stage = phaseInfo(game.phase).stage;
      if (stage === "night") night += 1;
      else if (stage === "signup") signup += 1;
      else day += 1;
      if (liveAlerts(game, { now, timeouts: meta?.phaseTimeouts }).some((alert) => alert.level === "error")) broken += 1;
    }
    return { night, day, signup, broken };
  }, [games, now, meta?.phaseTimeouts]);

  const stop = async (groupId: number) => {
    setStopping(groupId);
    try {
      await wwApi.stop(groupId);
      message.success("已提交强制结束，引擎收尾后对局会从列表消失");
      onChanged();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setStopping(0);
    }
  };

  const setHistory = (next: Partial<HistoryFilters>) => {
    const merged = { ...history, ...next };
    patch({
      "ww.page": merged.page > 1 ? String(merged.page) : null,
      "ww.q": merged.search || null,
      "ww.status": merged.status !== "all" ? merged.status : null,
      "ww.board": merged.board || null,
      "ww.winner": merged.winner || null,
      "ww.group": merged.groupId !== null ? String(merged.groupId) : null,
      "ww.user": merged.userId !== null ? String(merged.userId) : null,
      "ww.detail": merged.game !== null ? String(merged.game) : null,
    });
  };
  const openHistory = (next: Partial<HistoryFilters>) => {
    patch({
      "ww.view": "history",
      "ww.game": null,
      "ww.page": null,
      "ww.q": null,
      "ww.status": null,
      "ww.winner": null,
      "ww.detail": null,
      "ww.board": next.board || null,
      "ww.group": next.groupId != null ? String(next.groupId) : null,
      "ww.user": next.userId != null ? String(next.userId) : null,
    });
  };

  if (!available) {
    return <Alert className="section-alert" type="warning" showIcon message="狼人杀子插件未加载" description="实时对局与战绩暂不可用；子插件随 Bot 启动加载后这里会自动恢复。" />;
  }

  const switchView = (next: WwView) => patch({ "ww.view": next === "live" ? null : next, "ww.game": null, "ww.tab": null, "ww.seat": null });

  return (
    <div className="ww2-page">
      <section className={`ww2-status${counts.broken ? " is-error" : ""}`} aria-label="狼人杀状态">
        <div className="ww2-status-main">
          <span className={`ww2-status-dot${games.length ? " is-live" : ""}`} aria-hidden="true" />
          <div>
            <strong>{games.length ? `${games.length} 局进行中` : "当前没有对局"}</strong>
            <span>{games.length ? `黑夜 ${counts.night} · 白天 ${counts.day} · 报名 ${counts.signup}` : "开局后实时出现在这里，每 5 秒自动刷新"}</span>
          </div>
        </div>
        <div className="ww2-status-chips">
          {counts.broken > 0 && <Tag color="red">{counts.broken} 局引擎异常</Tag>}
          {meta && <Tooltip title="可用板子"><Tag>{meta.boards.length} 个板子</Tag></Tooltip>}
          {meta && <Tooltip title="AI 玩家开关（WW_AI_ENABLED）"><Tag color={meta.config.ww_ai_enabled === false ? undefined : "purple"}>AI 玩家{meta.config.ww_ai_enabled === false ? "已关闭" : "已启用"}</Tag></Tooltip>}
        </div>
        <div className="ww2-status-actions">
          <Tooltip title={reveal ? "正在显示所有玩家身份（仅本机记住）" : "默认隐藏身份，避免投屏或截图时泄露"}>
            <label className={`ww2-reveal${reveal ? " is-on" : ""}`}>
              {reveal ? <EyeOutlined /> : <EyeInvisibleOutlined />}
              <span>上帝视角</span>
              <Switch size="small" checked={reveal} onChange={setReveal} aria-label="上帝视角：显示玩家身份" />
            </label>
          </Tooltip>
          <Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={onChanged}>刷新</Button>
        </div>
      </section>

      {gameId === null && (
        <Segmented
          className="ww2-views"
          value={view}
          onChange={(value) => switchView(value as WwView)}
          options={[
            { value: "live", label: <span><ThunderboltOutlined /> 实时对局 <Badge count={games.length} size="small" color="#e4558a" showZero={false} /></span> },
            { value: "history", label: <span><HistoryOutlined /> 战绩</span> },
            { value: "insights", label: <span><BarChartOutlined /> 数据洞察</span> },
            { value: "rules", label: <span><BookOutlined /> 规则与配置</span> },
          ]}
        />
      )}

      {gameId !== null ? (
        <GameRoom
          groupId={gameId}
          reveal={reveal}
          meta={meta}
          tab={roomTab}
          seat={roomSeat}
          onTab={(next: RoomTab) => patch({ "ww.tab": next === "feed" ? null : next }, true)}
          onSeat={(seat) => patch({ "ww.seat": seat !== null ? String(seat) : null }, true)}
          onBack={() => patch({ "ww.game": null, "ww.tab": null, "ww.seat": null })}
          onStopped={onChanged}
          onOpenHistory={() => openHistory({ groupId: gameId })}
        />
      ) : view === "live" ? (
        <LiveView games={games} reveal={reveal} meta={meta} now={now} stopping={stopping}
          onOpen={(groupId) => patch({ "ww.game": String(groupId) })} onStop={(groupId) => { void stop(groupId); }} />
      ) : view === "history" ? (
        <HistoryView filters={history} meta={meta} onChange={setHistory} />
      ) : view === "insights" ? (
        <InsightsView days={days} board={statsBoard} meta={meta}
          onChange={(next) => patch({
            "ww.days": next.days !== undefined ? (next.days === 30 ? null : String(next.days)) : params.get("ww.days"),
            "ww.sboard": next.board !== undefined ? next.board || null : params.get("ww.sboard"),
          }, true)}
          onOpenHistory={openHistory} />
      ) : (
        <RulesView meta={meta} error={metaQuery.error} onRetry={metaQuery.reload} onOpenBoard={(board) => openHistory({ board })} />
      )}
    </div>
  );
}
