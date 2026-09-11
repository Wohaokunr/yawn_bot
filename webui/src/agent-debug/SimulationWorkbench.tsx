import { App as AntApp, Button, Card, Col, Input, Row, Segmented, Select, Space, Switch, Tag, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { AdminEmpty, formatTime, QueryErrorAlert, useApiQuery } from "../shared";
import type { AgentDebugMode, AgentDebugResponse, AgentMessageItem } from "../types";
import { AGENT_DEBUG_MODES, debugMessageLabel } from "./debug-utils";

const { Text, Paragraph } = Typography;

export function SimulationWorkbench({
  groupId,
  onResult,
}: {
  groupId: string;
  onResult: (result: AgentDebugResponse) => void;
}): React.JSX.Element {
  const { message } = AntApp.useApp();
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedMessageId = searchParams.get("debug.messageId") ?? "";
  const [mode, setMode] = useState<AgentDebugMode>("dialogue");
  const [source, setSource] = useState<"history" | "simulation">(linkedMessageId ? "history" : "simulation");
  const [messageId, setMessageId] = useState(linkedMessageId);
  const [actorUserId, setActorUserId] = useState("");
  const [text, setText] = useState("");
  const [runModel, setRunModel] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const messagesQuery = useApiQuery({
    queryKey: ["agent-debug-messages", groupId],
    fetcher: (signal) => api<AgentMessageItem[]>(`/agent/groups/${groupId}/messages?page=1&pageSize=100`, { signal }).then((response) => response.data),
    invalidation: {
      resources: ["agent_group_data", "agent_privacy"],
      scope: { groupId },
    },
  });

  useEffect(() => {
    if (linkedMessageId) {
      setSource("history");
      setMessageId(linkedMessageId);
    }
  }, [linkedMessageId]);

  const selectMessage = (value: string): void => {
    setMessageId(value);
    const next = new URLSearchParams(searchParams);
    if (value) next.set("debug.messageId", value); else next.delete("debug.messageId");
    setSearchParams(next, { replace: true });
  };

  const messageOptions = useMemo(
    () => (messagesQuery.data ?? []).filter((row) => row.role !== "bot").map((row) => ({ value: row.messageId, label: debugMessageLabel(row) })),
    [messagesQuery.data],
  );
  const selectedMessage = useMemo(
    () => (messagesQuery.data ?? []).find((row) => row.messageId === messageId) ?? null,
    [messageId, messagesQuery.data],
  );

  const run = async () => {
    if (source === "history" && !messageId) {
      message.warning("请先选择一条历史消息");
      return;
    }
    if (source === "simulation" && (!actorUserId.trim() || !text.trim())) {
      message.warning("请填写模拟发言人和消息正文");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const body = source === "history"
        ? { mode, messageId: Number(messageId), runModel }
        : { mode, actorUserId: Number(actorUserId), text: text.trim(), runModel };
      const response = await api<AgentDebugResponse>(`/agent/groups/${groupId}/debug/run`, { method: "POST", body: JSON.stringify(body) });
      onResult(response.data);
      message.success(runModel ? "真实模型试跑完成，未执行任何动作" : "提示词快照已生成");
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return <Card
    title="模拟工作台"
    extra={<Text type="secondary">每次运行都只生成 dry-run 快照，不执行工具副作用</Text>}
  >
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      <div className="agent-debug-control-row">
        <Text strong>调试场景</Text>
        <Segmented block value={mode} onChange={(value) => setMode(value as AgentDebugMode)} options={AGENT_DEBUG_MODES} />
      </div>
      <div className="agent-debug-control-row">
        <Text strong>消息来源</Text>
        <Segmented value={source} onChange={(value) => setSource(value as "history" | "simulation")} options={[{ value: "history", label: "历史消息回放" }, { value: "simulation", label: "模拟消息" }]} />
      </div>

      {source === "history" ? <Space orientation="vertical" size="small" style={{ width: "100%" }}>
        <Select
          showSearch
          loading={messagesQuery.loading}
          value={messageId || undefined}
          onChange={selectMessage}
          options={messageOptions}
          optionFilterProp="label"
          placeholder="选择保留期内的一条成员消息"
          style={{ width: "100%" }}
          notFoundContent={messagesQuery.error ? "消息记录加载失败" : undefined}
        />
        {selectedMessage && <Card size="small" className="agent-debug-source-preview">
          <Space orientation="vertical" size={4} style={{ width: "100%" }}>
            <Space wrap size={8}>
              <Text strong>{selectedMessage.senderName || selectedMessage.userId}</Text>
              <Tag>{selectedMessage.role}</Tag>
              <Text type="secondary">{formatTime(selectedMessage.receivedAt)}</Text>
            </Space>
            <Paragraph style={{ marginBottom: 0 }}>{selectedMessage.text || "[媒体消息]"}</Paragraph>
          </Space>
        </Card>}
      </Space> : <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <Space orientation="vertical" size={6} style={{ width: "100%" }}>
            <Text strong>模拟发言人</Text>
            <Input value={actorUserId} onChange={(event) => setActorUserId(event.target.value)} placeholder="成员 QQ 号" />
          </Space>
        </Col>
        <Col xs={24} md={16}>
          <Space orientation="vertical" size={6} style={{ width: "100%" }}>
            <Text strong>消息正文</Text>
            <Input.TextArea value={text} onChange={(event) => setText(event.target.value)} autoSize={{ minRows: 3, maxRows: 7 }} maxLength={4000} showCount placeholder="输入要模拟的当前群消息" />
          </Space>
        </Col>
      </Row>}

      <div className="agent-debug-run-row">
        <Space wrap>
          <Switch checked={runModel} onChange={setRunModel} />
          <div>
            <Text strong>{runModel ? "调用真实模型" : "仅构建提示词"}</Text><br />
            <Text type="secondary">{runModel ? "30 秒超时，并发上限 2；仍不会执行任何副作用" : "用于检查上下文、Prompt 和可见工具，不产生模型调用"}</Text>
          </div>
        </Space>
        <Button type="primary" onClick={run} loading={running}>{runModel ? "开始真实试跑" : "生成调试快照"}</Button>
      </div>
      {error && <QueryErrorAlert error={error} onRetry={run} />}
      {messagesQuery.error && !messagesQuery.data && <QueryErrorAlert error={messagesQuery.error} onRetry={messagesQuery.reload} />}
      {messagesQuery.data && messagesQuery.data.length === 0 && source === "history" && <AdminEmpty description="当前没有可回放的成员消息" />}
    </Space>
  </Card>;
}
