import { Card, Col, Row, Space, Tabs, Typography } from "antd";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { QueryErrorAlert } from "../shared";
import type { AgentDebugResponse } from "../types";
import { SimulationWorkbench } from "./SimulationWorkbench";
import { TraceSidebar } from "./TraceSidebar";
import { TraceWorkspace } from "./TraceWorkspace";
import { TracePipeline } from "./TracePipeline";
import { useExecutionTraces } from "./useExecutionTraces";

export function AgentDebugger({ groupId }: { groupId: string }): React.JSX.Element {
  const [params] = useSearchParams();
  const messageId = params.get("messageId");
  const [tab, setTab] = useState(messageId ? "simulation" : "runtime");
  const traces = useExecutionTraces(groupId, tab === "runtime");
  const [result, setResult] = useState<AgentDebugResponse | null>(null);
  const [baseline, setBaseline] = useState<AgentDebugResponse | null>(null);
  useEffect(() => { if (messageId) setTab("simulation"); }, [messageId]);

  return <Tabs className="agent-debugger" activeKey={tab} onChange={setTab} items={[
    { key: "runtime", label: "真实执行", children: <Row gutter={[16, 16]} align="top">
      <Col xs={24} xl={8}><TraceSidebar traces={traces} /></Col>
      <Col xs={24} xl={16}><Card title="执行详情" extra={traces.detailLoading && traces.selectedTrace ? "正在更新…" : null}>
        {traces.detailError && <QueryErrorAlert error={traces.selectedTrace ? `更新失败，当前展示上次快照：${traces.detailError}` : traces.detailError} onRetry={traces.reloadSelected} />}
        {traces.selectedTrace ? <TracePipeline key={traces.selectedTrace.traceId} trace={traces.selectedTrace} />
          : <Typography.Text type="secondary">{traces.detailLoading ? "正在加载 Trace 详情…" : "选择一条真实执行查看结果与事件。"}</Typography.Text>}
      </Card></Col>
    </Row> },
    { key: "simulation", label: "模拟调试", children: <Space orientation="vertical" size="large" style={{ width: "100%" }}>
      <SimulationWorkbench groupId={groupId} onResult={setResult} />
      {result ? <TraceWorkspace result={result} baseline={baseline}
        onPinBaseline={() => setBaseline(result)} onClearBaseline={() => setBaseline(null)} />
        : <Typography.Text type="secondary">生成快照后，在这里检查结果、上下文和 Prompt，并固定基准进行比较。</Typography.Text>}
    </Space> },
  ]} />;
}
