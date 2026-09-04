import { Alert } from "antd";
import { useState } from "react";
import { PanelStack, ScrollRegion, SplitWorkspace } from "../layout";
import type { AgentDebugResponse } from "../types";
import { SimulationWorkbench } from "./SimulationWorkbench";
import { TraceSidebar } from "./TraceSidebar";
import { TraceWorkspace } from "./TraceWorkspace";
import { useExecutionTraces } from "./useExecutionTraces";

export function AgentDebugger({ groupId }: { groupId: string }): React.JSX.Element {
  const traces = useExecutionTraces(groupId);
  const [result, setResult] = useState<AgentDebugResponse | null>(null);
  const [baseline, setBaseline] = useState<AgentDebugResponse | null>(null);

  return <PanelStack className="agent-debug-page">
    <Alert
      type="info"
      showIcon
      className="section-alert agent-debug-intro"
      message="Agent 调试工作台"
      description="桌面端左侧 Trace Navigator 与右侧 Inspector 独立滚动；切到平板或手机后自动恢复普通纵向页面。下方模拟运行仍是 dry-run，可固定一次结果进行 Context、Prompt、Tools、Speech、Token 和 Model 对比。"
    />
    <SplitWorkspace
      className="agent-debug-workbench"
      primaryClassName="agent-debug-navigator-pane"
      secondaryClassName="agent-debug-inspector-pane"
      primary={<TraceSidebar traces={traces} />}
      secondary={
        <ScrollRegion className="agent-debug-inspector-scroll">
          <TraceWorkspace
            runtimeTrace={traces.selectedTrace}
            runtimeLoading={traces.detailLoading}
            runtimeError={traces.detailError}
            onReloadRuntime={traces.reloadSelected}
            result={result}
            baseline={baseline}
            onPinBaseline={() => { if (result) setBaseline(result); }}
            onClearBaseline={() => setBaseline(null)}
          />
        </ScrollRegion>
      }
    />
    <div className="agent-debug-simulation-dock">
      <SimulationWorkbench groupId={groupId} onResult={setResult} />
    </div>
  </PanelStack>;
}
