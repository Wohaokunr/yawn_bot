import { List, Tag } from "antd";
import { AdminEmpty } from "../shared";
import type { AgentDebugResponse } from "../types";
import { DebugRawBlock } from "./debug-utils";

export function PromptInspector({
  messages,
}: {
  messages: AgentDebugResponse["promptMessages"];
}): React.JSX.Element {
  return messages.length === 0 ? <AdminEmpty description="Prompt 为空" /> : <List
    className="agent-debug-prompt-list"
    dataSource={messages}
    renderItem={(item, index) => <List.Item key={`${item.role}-${index}`}>
      <div className="agent-debug-prompt-item">
        <Tag>{item.role}</Tag>
        {typeof item.content === "string" ? <pre className="agent-debug-prompt-content">{item.content}</pre> : <DebugRawBlock value={item.content} />}
      </div>
    </List.Item>}
  />;
}
