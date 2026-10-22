import { Card, Descriptions, List, Progress, Segmented, Space, Table, Tabs, Tag, Typography } from "antd";
import { useState } from "react";
import { AdminEmpty } from "../shared";
import type { AgentDebugResponse } from "../types";
import {
  debugDisplay,
  debugRecord,
  SEGMENT_NAME_LABELS,
  TRACE_MEDIA_SOURCE_LABELS,
} from "./debug-utils";

const { Text, Paragraph } = Typography;

export function ContextBudgetInspector({ stats }: { stats: Record<string, unknown> }): React.JSX.Element {
  const rows = [
    { key: "history", label: "历史消息" },
    { key: "memory", label: "记忆" },
    { key: "relation", label: "关系" },
  ].map(({ key, label }) => {
    const data = debugRecord(stats[key]);
    const count = Number(data.count ?? 0);
    const limit = Math.max(1, Number(data.limit ?? 1));
    return {
      key,
      label,
      count,
      limit,
      reached: Boolean(data.limitReached),
      characters: Number(data.characters ?? 0),
    };
  });
  return <Card size="small" title="上下文预算">
    <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
      {rows.map((row) => <div key={row.key} className="agent-debug-budget-row">
        <div className="agent-debug-budget-head">
          <Text>{row.label}</Text>
          <Text type={row.reached ? "warning" : "secondary"}>{row.count} / {row.limit} · {row.characters} 字符</Text>
        </div>
        <Progress percent={Math.min(100, Math.round((row.count / row.limit) * 100))} showInfo={false} status={row.reached ? "exception" : "normal"} />
      </div>)}
      <Space wrap>
        <Tag>成员 {Number(stats.memberCount ?? 0)}</Tag>
        <Tag>媒体摘要 {Number(stats.mediaSummaryCount ?? 0)}</Tag>
      </Space>
    </Space>
  </Card>;
}

export function CurrentTurnInspector({ value }: { value: Record<string, unknown> }): React.JSX.Element {
  const mentions = Array.isArray(value.mentions) ? value.mentions : [];
  const replyTo = debugRecord(value.reply_to);
  const media = Array.isArray(value.media) ? value.media.map(debugRecord) : [];
  return <Card size="small" title="当前消息">
    <Descriptions size="small" column={{ xs: 1, sm: 2, xxl: 3 }} items={[
      { key: "actor", label: "发言人", children: debugDisplay(value.name || value.user_id) },
      { key: "message", label: "消息 ID", children: debugDisplay(value.message_id) },
      { key: "trigger", label: "触发方式", children: <Tag>{debugDisplay(value.trigger)}</Tag> },
      { key: "role", label: "角色", children: debugDisplay(value.role) },
      { key: "mentions", label: "@ 成员", children: mentions.length ? mentions.map(String).join("、") : "—" },
      { key: "reply", label: "回复对象", children: debugDisplay(replyTo.name || replyTo.user_id) },
      { key: "forward", label: "转发节点", children: debugDisplay(value.forward_nodes, "0") },
    ]} />
    <Paragraph className="agent-debug-message-content">{debugDisplay(value.content, "[空消息]")}</Paragraph>
    {media.length > 0 && <Space wrap>
      {media.map((item, index) => <Tag key={`${debugDisplay(item.type)}-${index}`} color={item.source === "reply" ? "blue" : item.source === "forward" ? "purple" : "green"}>
        {TRACE_MEDIA_SOURCE_LABELS[String(item.source || "current")] ?? debugDisplay(item.source, "current")} · {SEGMENT_NAME_LABELS[String(item.type)] ?? debugDisplay(item.type, "media")}{item.name ? ` · ${debugDisplay(item.name)}` : ""}
      </Tag>)}
    </Space>}
  </Card>;
}

const CONTEXT_SELECTION_REASON: Record<string, string> = {
  recent_cluster: "近期话题簇",
  focus_relation: "直接涉及当前成员",
  query_overlap: "与当前消息相关",
  relevant_neighbor: "相关消息的相邻上下文",
  proactive_recent_cluster: "主动会话近期话题簇",
  low_information: "低信息消息",
  stale: "超过相关性时间窗",
  not_relevant: "与当前回合无明显关联",
  context_budget: "超过上下文预算",
};

const BUDGET_COMPONENT_LABELS: Record<string, string> = {
  history: "历史消息",
  memory: "记忆",
  members: "成员画像",
  relations: "关系",
};

/** 展示后端 context_budget 的 Token 装箱结果：第一行是总预算，其余是分项。 */
export function ContextPackingInspector({ budget }: { budget: Array<Record<string, unknown>> }): React.JSX.Element {
  if (budget.length === 0) return <AdminEmpty description="本轮没有 Token 装箱记录" />;
  const [head, ...components] = budget;
  const total = debugRecord(head);
  const used = Number(total.usedTokens ?? 0);
  const limit = Number(total.contextLimit ?? 0);
  return <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
    <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
      { key: "model", label: "装箱模型", children: debugDisplay(total.model) },
      { key: "window", label: "上下文窗口", children: debugDisplay(total.contextWindow) },
      { key: "prompt", label: "Prompt 上限", children: debugDisplay(total.promptLimit) },
      { key: "reserve", label: "预留输出", children: debugDisplay(total.completionReserve) },
      { key: "limit", label: "上下文预算", children: debugDisplay(total.contextLimit) },
      { key: "used", label: "已用", children: limit > 0 ? `${used} Token（${Math.round((used / limit) * 100)}%）` : debugDisplay(total.usedTokens) },
    ]} />
    {limit > 0 && <Progress percent={Math.min(100, Math.round((used / limit) * 100))} status={used >= limit ? "exception" : "normal"} />}
    <Table
      size="small"
      pagination={false}
      rowKey={(row) => String(row.component)}
      dataSource={components.map(debugRecord)}
      scroll={{ x: 520 }}
      columns={[
        { title: "分项", dataIndex: "component", render: (value: string) => BUDGET_COMPONENT_LABELS[value] ?? value },
        { title: "预算", dataIndex: "budgetTokens", width: 90 },
        {
          title: "已用",
          width: 160,
          render: (_, row: Record<string, unknown>) => {
            const share = Number(row.budgetTokens) > 0 ? Math.min(100, Math.round((Number(row.usedTokens) / Number(row.budgetTokens)) * 100)) : 0;
            return <div className="dbg-mini-progress"><Progress percent={share} size="small" showInfo={false} /><span>{debugDisplay(row.usedTokens, "0")}</span></div>;
          },
        },
        { title: "保留", dataIndex: "kept", width: 70 },
        { title: "丢弃", dataIndex: "dropped", width: 70, render: (value: number) => value > 0 ? <Text type="warning">{value}</Text> : value },
      ]}
    />
  </Space>;
}

function SelectionList({ selection }: { selection: AgentDebugResponse["contextSelection"] }): React.JSX.Element {
  const [filter, setFilter] = useState<"all" | "kept" | "dropped">("all");
  if (selection.length === 0) return <AdminEmpty description="本轮没有历史筛选轨迹" />;
  const reasons = selection.reduce<Record<string, { kept: number; dropped: number }>>((acc, item) => {
    const row = acc[item.reason] ?? { kept: 0, dropped: 0 };
    if (item.selected) row.kept += 1; else row.dropped += 1;
    acc[item.reason] = row;
    return acc;
  }, {});
  const rows = selection.filter((item) => filter === "all" || (filter === "kept") === item.selected);
  const kept = selection.filter((item) => item.selected).length;
  return <Space orientation="vertical" size="small" style={{ width: "100%" }}>
    <div className="dbg-toolbar">
      <Segmented size="small" value={filter} onChange={(value) => setFilter(value as typeof filter)} options={[
        { value: "all", label: `全部 ${selection.length}` },
        { value: "kept", label: `保留 ${kept}` },
        { value: "dropped", label: `丢弃 ${selection.length - kept}` },
      ]} />
    </div>
    <div className="dbg-reason-tags">
      {Object.entries(reasons).map(([reason, count]) => <Tag key={reason} color={count.kept > 0 ? "green" : undefined}>
        {CONTEXT_SELECTION_REASON[reason] ?? reason}：{count.kept > 0 ? `保留 ${count.kept}` : ""}{count.kept > 0 && count.dropped > 0 ? " / " : ""}{count.dropped > 0 ? `丢弃 ${count.dropped}` : ""}
      </Tag>)}
    </div>
    <List
      className="agent-debug-list"
      dataSource={rows}
      renderItem={(item, index) => <List.Item key={`${String(item.message_id ?? "none")}-${index}`}>
        <div className="agent-debug-list-item">
          <Space wrap size={6}>
            <Tag color={item.selected ? "green" : undefined}>{item.selected ? "保留" : "丢弃"}</Tag>
            <Text strong>{item.name?.trim() || String(item.user_id ?? "未知成员")}</Text>
            <Tag>{item.role || "member"}</Tag>
            {item.title ? <Tag>{item.title}</Tag> : null}
            <Text type="secondary">{item.minutes_ago} 分钟前</Text>
          </Space>
          <Paragraph className="agent-debug-message-content" type={item.selected ? undefined : "secondary"}>
            {item.text || "[媒体消息/空文本]"}{item.text_truncated ? " …（调试预览已截断）" : ""}
          </Paragraph>
          <Space wrap size={8}>
            <Text type={item.selected ? "success" : "secondary"}>
              筛选原因：{CONTEXT_SELECTION_REASON[item.reason] ?? item.reason}
            </Text>
            <Text type="secondary">消息 ID {String(item.message_id ?? "—")}</Text>
            {typeof item.score === "number" && <Text type="secondary">相关分 {item.score.toFixed(2)}</Text>}
          </Space>
        </div>
      </List.Item>}
    />
  </Space>;
}

export function ContextInspector({
  context,
  selection,
  budget = [],
}: {
  context: AgentDebugResponse["context"];
  selection: AgentDebugResponse["contextSelection"];
  budget?: AgentDebugResponse["contextBudget"];
}): React.JSX.Element {
  const messages = context.messages ?? [];
  const members = context.members ?? [];
  const memories = context.memories ?? [];
  const relations = context.relations ?? [];
  return <Tabs size="small" items={[
    {
      key: "messages",
      label: `历史消息 ${messages.length}`,
      children: messages.length === 0 ? <AdminEmpty description="没有进入本轮 Prompt 的历史消息" /> : <List
        className="agent-debug-list"
        dataSource={messages}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.message_id ?? index)}>
            <div className="agent-debug-list-item">
              <Space wrap size={6}>
                <Text strong>{debugDisplay(row.name || row.user_id, "未知成员")}</Text>
                {row.minutes_ago !== undefined && <Text type="secondary">{debugDisplay(row.minutes_ago)} 分钟前</Text>}
                {Boolean(row.topic_break_before) && <Tag>话题分界</Tag>}
              </Space>
              <Text>{debugDisplay(row.text, "[媒体消息]")}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "selection",
      label: `筛选轨迹 ${selection.length}`,
      children: <SelectionList selection={selection} />,
    },
    {
      key: "packing",
      label: "Token 装箱",
      children: <ContextPackingInspector budget={budget} />,
    },
    {
      key: "members",
      label: `成员 ${members.length}`,
      children: members.length === 0 ? <AdminEmpty description="本轮没有成员画像上下文" /> : <List
        className="agent-debug-list"
        dataSource={members}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.user_id ?? index)}>
            <div className="agent-debug-list-item">
              <Text strong>{debugDisplay(row.name || row.nickname || row.user_id, "未知成员")}</Text>
              <Text type="secondary">{debugDisplay(row.role)}{row.title ? ` · ${debugDisplay(row.title)}` : ""}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "memories",
      label: `记忆 ${memories.length}`,
      children: memories.length === 0 ? <AdminEmpty description="本轮没有命中的记忆" /> : <List
        className="agent-debug-list"
        dataSource={memories}
        renderItem={(item, index) => {
          const row = debugRecord(item);
          return <List.Item key={String(row.id ?? index)}>
            <div className="agent-debug-list-item">
              <Space wrap size={6}><Text strong>{debugDisplay(row.subject_name || row.subject || row.memory_type, "记忆")}</Text>{row.memory_type ? <Tag>{debugDisplay(row.memory_type)}</Tag> : null}</Space>
              <Text>{debugDisplay(row.content)}</Text>
            </div>
          </List.Item>;
        }}
      />,
    },
    {
      key: "relations",
      label: `关系 ${relations.length}`,
      children: relations.length === 0 ? <AdminEmpty description="本轮没有关系上下文" /> : <List size="small" dataSource={relations} renderItem={(item) => <List.Item><Text>{debugDisplay(item)}</Text></List.Item>} />,
    },
  ]} />;
}
