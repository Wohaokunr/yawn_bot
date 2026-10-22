import { App as AntApp, Button, Popconfirm, Progress, Skeleton, Switch, Tooltip } from "antd";
import { useState } from "react";
import { api } from "../api";
import type { ApiQuery } from "../shared";
import type { AgentConfig, AgentDiagnostics } from "../types";
import { relativeTime } from "./polling";

type RuntimeState = { tone: "ok" | "paused" | "error" | "warning"; label: string; detail: string };

export function runtimeState(data: AgentDiagnostics): RuntimeState {
  if (!data.effective.enabled) {
    return { tone: "paused", label: "已暂停", detail: "总开关关闭，不响应也不整理记忆" };
  }
  const error = data.blockers.find((item) => item.severity === "error");
  if (error) return { tone: "error", label: "存在阻塞", detail: error.title };
  const warning = data.blockers.find((item) => item.severity === "warning");
  if (warning) return { tone: "warning", label: "运行中", detail: warning.title };
  return { tone: "ok", label: "运行中", detail: "触发、路由与记忆均正常" };
}

function QuotaMeter({ label, used, limit, hint }: { label: string; used: number; limit: number; hint: string }): React.JSX.Element {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 1;
  const status = ratio >= 1 ? "exception" : "normal";
  return (
    <Tooltip title={hint}>
      <div className="agent-status-meter">
        <div className="agent-status-meter-head">
          <span>{label}</span>
          <strong>{used}<em> / {limit}</em></strong>
        </div>
        <Progress
          percent={Math.round(ratio * 100)}
          showInfo={false}
          size="small"
          status={status}
          strokeColor={ratio >= 0.8 && ratio < 1 ? "#d48806" : "#e4558a"}
          aria-label={`${label} ${used}/${limit}`}
        />
      </div>
    </Tooltip>
  );
}

// 工作台顶部常驻状态条：任何子页面都能看到 Agent 是否在跑、额度还剩多少，并一键开关。
export function AgentStatusBar({
  groupId,
  diagnosticsQuery,
  onNavigate,
}: {
  groupId: string;
  diagnosticsQuery: ApiQuery<AgentDiagnostics>;
  onNavigate: (tab: string) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [toggling, setToggling] = useState(false);
  const [closing, setClosing] = useState(false);
  const data = diagnosticsQuery.data;

  if (!data) {
    return (
      <div className="agent-status-bar" aria-busy="true">
        {diagnosticsQuery.error
          ? <span className="agent-status-error">状态加载失败：{diagnosticsQuery.error} <Button size="small" type="link" onClick={diagnosticsQuery.reload}>重试</Button></span>
          : <Skeleton active paragraph={false} title={{ width: "60%" }} />}
      </div>
    );
  }

  const state = runtimeState(data);
  const effective = data.effective;
  const conversation = effective.shortConversation;
  const tools = data.tools;

  const setEnabled = async (enabled: boolean): Promise<void> => {
    setToggling(true);
    try {
      const current = await api<AgentConfig>(`/agent/groups/${groupId}/config`);
      await api<AgentConfig>(`/agent/groups/${groupId}/config`, {
        method: "PATCH",
        body: JSON.stringify({ enabled, version: current.data.version ?? null }),
      });
      message.success(enabled ? "Agent 已启用" : "Agent 已暂停，配置与记忆均保留");
      diagnosticsQuery.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setToggling(false);
    }
  };

  const closeConversation = async (): Promise<void> => {
    setClosing(true);
    try {
      await api(`/agent/groups/${groupId}/conversation/close`, { method: "POST" });
      message.success("已结束当前短会话");
      diagnosticsQuery.reload();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setClosing(false);
    }
  };

  return (
    <div className={`agent-status-bar is-${state.tone}`}>
      <div className="agent-status-main">
        <span className={`agent-status-dot is-${state.tone}`} aria-hidden="true" />
        <div className="agent-status-copy">
          <strong>{state.label}</strong>
          <button type="button" className="agent-status-detail" onClick={() => onNavigate("overview")}>
            {state.detail}
          </button>
        </div>
        {effective.enabled
          ? <Popconfirm
            title="暂停这个群的 Agent？"
            description="暂停后不再响应、主动发言或整理记忆；配置、人设和记忆都会保留。"
            okText="暂停"
            onConfirm={() => setEnabled(false)}
          >
            <Switch checked loading={toggling} aria-label="Agent 总开关" />
          </Popconfirm>
          : <Switch checked={false} loading={toggling} aria-label="Agent 总开关" onChange={(value) => void setEnabled(value)} />}
      </div>

      <div className="agent-status-meters">
        <QuotaMeter
          label="主动发言"
          used={effective.proactiveToday}
          limit={effective.dailyLimit}
          hint={effective.cooldownRemainingMinutes > 0 ? `冷却还剩约 ${effective.cooldownRemainingMinutes} 分钟` : "今日主动发言次数"}
        />
        {tools && <QuotaMeter label="特权工具" used={tools.adminToday} limit={tools.adminLimit} hint="群管理与群文件发送共用额度" />}
        {tools && <QuotaMeter label="高风险" used={tools.criticalToday} limit={tools.criticalLimit} hint="踢人、全员禁言、管理员变更等单独计数" />}
      </div>

      <div className="agent-status-session">
        {conversation.active
          ? <>
            <div className="agent-status-session-copy">
              <span className="agent-status-live">短会话进行中</span>
              <span className="agent-status-muted">
                {conversation.topic ? `「${conversation.topic}」· ` : ""}
                Bot {conversation.botTurns}{data.pipeline ? ` / ${data.pipeline.conversationMaxBotTurns}` : ""} 回合
              </span>
            </div>
            <Popconfirm title="立即结束当前短会话？" description="Bot 会停止续聊，下次需重新被呼叫或主动触发。" onConfirm={closeConversation}>
              <Button size="small" loading={closing}>结束</Button>
            </Popconfirm>
          </>
          : <div className="agent-status-session-copy">
            <span className="agent-status-muted">{conversation.enabled ? "短会话空闲" : "短会话已关闭"}</span>
            <span className="agent-status-muted">上次回复 {relativeTime(effective.lastAgentAt)}</span>
          </div>}
      </div>
    </div>
  );
}
