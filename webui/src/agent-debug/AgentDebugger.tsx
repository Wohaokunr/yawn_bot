import { Alert, Col, Row, Space } from "antd";
import { useState } from "react";
import type { AgentDebugResponse } from "../types";
import { SimulationWorkbench } from "./SimulationWorkbench";
import { TraceSidebar } from "./TraceSidebar";
import { TraceWorkspace } from "./TraceWorkspace";
import { useExecutionTraces } from "./useExecutionTraces";

export function AgentDebugger({ groupId }: { groupId: string }): React.JSX.Element {
  const traces = useExecutionTraces(groupId);
  const [result, setResult] = useState<AgentDebugResponse | null>(null);
  const [baseline, setBaseline] = useState<AgentDebugResponse | null>(null);

  return <Space orientation="vertical" size="large" style={{ width: "100%" }}>
    <Alert
      type="info"
      showIcon
      className="section-alert"
      message="Agent 调试工作台 + 发言模拟器"
      description="左侧查看最近真实执行，右侧按需加载完整 Trace；下方可进行无副作用发言模拟，固定一次结果后再与下一次运行比较 Context、Prompt、Tools、Speech、Token 和 Model。"
    />
    <Row gutter={[16, 16]} align="top">
      <Col xs={24} xl={8}><TraceSidebar traces={traces} /></Col>
      <Col xs={24} xl={16}><TraceWorkspace
        runtimeTrace={traces.selectedTrace}
        runtimeLoading={traces.detailLoading}
        runtimeError={traces.detailError}
        onReloadRuntime={traces.reloadSelected}
        result={result}
        baseline={baseline}
        onPinBaseline={() => { if (result) setBaseline(result); }}
        onClearBaseline={() => setBaseline(null)}
      /></Col>
    </Row>
    <SimulationWorkbench groupId={groupId} onResult={setResult} />
  </Space>;
}
