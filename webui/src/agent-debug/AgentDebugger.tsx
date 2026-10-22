import { App as AntApp, type InputRef } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { AgentExecutionTrace } from "../types";
import { DebugHeader, DEBUG_VIEWS, type DebugView, ShortcutHelp } from "./DebugHeader";
import { normalizeMode, type LabReplayRequest, SimulationLab } from "./SimulationLab";
import { TraceCompare } from "./TraceCompare";
import { TraceInspector } from "./TraceInspector";
import { TraceNavigator } from "./TraceNavigator";
import { useDebugRuns } from "./useDebugRuns";
import { useExecutionTraces } from "./useExecutionTraces";

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || Boolean(target.closest("[role='combobox'], .ant-select, .ant-modal"));
}

export function AgentDebugger({ groupId }: { groupId: string }): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const traces = useExecutionTraces(groupId);
  const runs = useDebugRuns();
  const [replay, setReplay] = useState<LabReplayRequest | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<InputRef>(null);

  const requestedView = searchParams.get("debug.view");
  const view: DebugView = requestedView === "lab" || requestedView === "compare"
    ? requestedView
    : requestedView === "trace" ? "trace" : searchParams.get("debug.messageId") ? "lab" : "trace";

  const setView = useCallback((next: DebugView, extra: Record<string, string> = {}) => {
    const params = new URLSearchParams(searchParams);
    if (next === "trace") params.delete("debug.view"); else params.set("debug.view", next);
    for (const [key, value] of Object.entries(extra)) {
      if (value) params.set(key, value); else params.delete(key);
    }
    setSearchParams(params, { replace: true });
  }, [searchParams, setSearchParams]);

  const replayTrace = (trace: AgentExecutionTrace): void => {
    const intake = trace.events.find((event) => event.phase === "intake" && typeof event.output?.text_preview === "string");
    setReplay({
      nonce: Date.now(),
      mode: normalizeMode(trace.mode),
      messageId: trace.messageId ?? undefined,
      actorUserId: trace.actorUserId ?? undefined,
      text: intake ? String(intake.output.text_preview) : undefined,
    });
    setView("lab", trace.messageId ? { "debug.messageId": trace.messageId } : {});
    message.info(trace.messageId ? "已带入触发消息，确认后点“生成调试快照”" : "已带入发言人和消息预览，确认后再运行");
  };

  const compareTrace = (traceId: string): void => {
    const other = traces.summaries.find((item) => item.traceId !== traceId)?.traceId ?? "";
    setView("compare", { "debug.compare": `${traceId},${other}` });
  };

  const clearBuffer = async (): Promise<void> => {
    setClearing(true);
    try {
      const cleared = await traces.clearBuffer();
      message.success(`已清空 ${cleared} 条 Trace`);
    } catch (caught) {
      message.error((caught as Error).message);
    } finally {
      setClearing(false);
    }
  };

  // 页面级快捷键：只在调试台可见、且没在输入框里打字时生效。
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.altKey || event.ctrlKey || event.metaKey || isTyping(event.target)) return;
      const root = rootRef.current;
      if (!root || root.closest("[hidden]")) return;
      const key = event.key;
      if (key === "?") {
        event.preventDefault();
        setHelpOpen(true);
        return;
      }
      const target = DEBUG_VIEWS.find((item) => item.key === key);
      if (target) {
        event.preventDefault();
        setView(target.value);
        return;
      }
      if (key === "r") {
        event.preventDefault();
        traces.reloadSelected();
        return;
      }
      if (key === "p") {
        event.preventDefault();
        traces.setAutoRefresh(!traces.autoRefresh);
        return;
      }
      if (view !== "trace") return;
      if (key === "j" || key === "ArrowDown") {
        event.preventDefault();
        traces.selectRelative(1);
      } else if (key === "k" || key === "ArrowUp") {
        event.preventDefault();
        traces.selectRelative(-1);
      } else if (key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setView, traces, view]);

  return <div className="dbg agent-debug-page" ref={rootRef}>
    <DebugHeader
      view={view}
      onViewChange={(next) => setView(next)}
      stats={traces.stats}
      statsError={traces.statsError}
      onShowShortcuts={() => setHelpOpen(true)}
      onClearBuffer={() => void clearBuffer()}
      clearing={clearing}
    />
    <div className="dbg-view" hidden={view !== "trace"}>
      <div className="dbg-trace-layout agent-debug-workbench">
        <TraceNavigator ref={searchRef} traces={traces} onOpenLab={() => setView("lab")} />
        <TraceInspector groupId={groupId} traces={traces} stats={traces.stats} onReplay={replayTrace} onCompare={compareTrace} />
      </div>
    </div>
    <div className="dbg-view" hidden={view !== "lab"}>
      <SimulationLab groupId={groupId} runs={runs} replay={replay} active={view === "lab"} />
    </div>
    <div className="dbg-view" hidden={view !== "compare"}>
      {view === "compare" && <TraceCompare groupId={groupId} summaries={traces.summaries} />}
    </div>
    <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
  </div>;
}
