import { Input, List, Segmented, Select, Space, Tag, Typography } from "antd";
import { useMemo, useState } from "react";
import type { AgentDebugResponse } from "../types";
import { AdminEmpty } from "../shared";
import {
  debugDisplay,
  debugRecord,
  DebugRawBlock,
  SEGMENT_NAME_LABELS,
  TOOL_PERMISSION_LABELS,
  TOOL_PERMISSION_REASON_LABELS,
  toolDisplayName,
} from "./debug-utils";

const { Text } = Typography;

export function ToolArgumentSummary({ name, value }: { name: string; value: unknown }): React.JSX.Element | null {
  const args = debugRecord(value);
  if (Object.keys(args).length === 0) return null;

  if (name === "send_message") {
    const segments = Array.isArray(args.segments) ? args.segments.map((item) => debugRecord(item)) : [];
    const types = segments.map((item) => String(item.type ?? "")).filter(Boolean);
    return <Text type="secondary">
      准备发送 {segments.length} 个消息段{types.length > 0 ? `：${types.map((type) => SEGMENT_NAME_LABELS[type] ?? type).join(" + ")}` : ""}
    </Text>;
  }
  if (name === "send_forward") {
    const nodes = Array.isArray(args.nodes) ? args.nodes.length : 0;
    return <Text type="secondary">准备发送包含 {nodes} 个节点的合并转发</Text>;
  }
  if (name === "search_group_memory") {
    return <Text type="secondary">搜索内容：{String(args.query ?? args.keyword ?? "（未提供关键词）")}</Text>;
  }
  if (name === "get_group_member" || name === "get_person_profile") {
    return <Text type="secondary">目标成员：{String(args.user_id ?? args.userId ?? "未知")}</Text>;
  }
  if (name === "list_user_relations") {
    return <Text type="secondary">查看成员 {String(args.user_id ?? args.userId ?? "未知")} 的关系</Text>;
  }
  if (name === "record_user_relation") {
    return <Text type="secondary">
      记录 {String(args.subject_user_id ?? "?")} → {String(args.object_user_id ?? "?")} 的「{String(args.type ?? "关系")}」关系
    </Text>;
  }
  if (name === "mute_member") {
    return <Text type="secondary">目标成员：{String(args.user_id ?? "未知")}；时长：{String(args.duration ?? args.duration_seconds ?? "默认")} 秒</Text>;
  }
  if (name === "create_group_announcement") {
    const content = String(args.content ?? args.text ?? "");
    return <Text type="secondary">公告内容：{content ? `${content.slice(0, 80)}${content.length > 80 ? "…" : ""}` : "（空）"}</Text>;
  }
  if (name === "send_file") {
    return <Text type="secondary">发送一个经过 Agent 文件安全校验的群文件</Text>;
  }
  if (name === "search_reactions") {
    return <Text type="secondary">搜索表情：{String(args.query ?? args.keyword ?? args.tag ?? "未指定")}</Text>;
  }
  return null;
}

const PERMISSION_COLORS: Record<string, string> = {
  critical: "volcano",
  privileged: "red",
  message_send: "blue",
  state_write: "orange",
};

export function ToolInspector({
  tools,
  permissions,
}: {
  tools: AgentDebugResponse["tools"];
  permissions: AgentDebugResponse["toolPermissions"];
}): React.JSX.Element {
  const [exposure, setExposure] = useState<"all" | "exposed" | "blocked">("all");
  const [level, setLevel] = useState("");
  const [query, setQuery] = useState("");
  const schemas = useMemo(() => {
    const map = new Map<string, Record<string, unknown>>();
    tools.forEach((tool) => {
      const row = debugRecord(tool);
      const fn = debugRecord(row.function);
      const name = String(fn.name || row.name || "");
      if (name) map.set(name, tool);
    });
    return map;
  }, [tools]);
  if (permissions.length === 0) return <AdminEmpty description="当前调试场景不使用工具权限矩阵" />;
  const exposedCount = permissions.filter((item) => item.exposed).length;
  const levels = [...new Set(permissions.map((item) => item.permissionLevel))];
  const needle = query.trim().toLowerCase();
  const rows = permissions.filter((item) => (
    (exposure === "all" || (exposure === "exposed") === item.exposed)
    && (!level || item.permissionLevel === level)
    && (!needle || `${item.name} ${toolDisplayName(item.name)}`.toLowerCase().includes(needle))
  ));
  const reasonCounts = permissions.filter((item) => !item.exposed).reduce<Record<string, number>>((acc, item) => {
    acc[item.reason] = (acc[item.reason] ?? 0) + 1;
    return acc;
  }, {});
  return <div className="dbg-tools">
    <div className="dbg-prompt-stats">
      <span>开放 <strong>{exposedCount}</strong> / {permissions.length} 个工具</span>
      <span>本轮实际下发 Schema <strong>{tools.length}</strong> 个</span>
      {Object.entries(reasonCounts).map(([reason, count]) => <Tag key={reason}>{TOOL_PERMISSION_REASON_LABELS[reason] ?? reason} × {count}</Tag>)}
    </div>
    <div className="dbg-toolbar">
      <Input allowClear size="small" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工具名" aria-label="搜索工具" className="dbg-toolbar-search" />
      <Segmented size="small" value={exposure} onChange={(value) => setExposure(value as typeof exposure)} options={[{ value: "all", label: "全部" }, { value: "exposed", label: "已开放" }, { value: "blocked", label: "未开放" }]} />
      <Select size="small" value={level} onChange={setLevel} options={[{ value: "", label: "全部权限等级" }, ...levels.map((value) => ({ value, label: TOOL_PERMISSION_LABELS[value] ?? value }))]} aria-label="按权限等级过滤" className="dbg-toolbar-select" />
    </div>
    {rows.length === 0 ? <AdminEmpty description="没有符合条件的工具" /> : <List
      className="agent-debug-list"
      dataSource={rows}
      renderItem={(permission) => {
        const tool = schemas.get(permission.name);
        const row = tool ? debugRecord(tool) : {};
        const fn = debugRecord(row.function);
        return <List.Item key={permission.name}>
          <div className="agent-debug-list-item">
            <Space wrap>
              <Text strong>{toolDisplayName(permission.name)}</Text>
              <Text type="secondary" code>{permission.name}</Text>
              <Tag color={PERMISSION_COLORS[permission.permissionLevel] ?? "default"}>{TOOL_PERMISSION_LABELS[permission.permissionLevel] ?? permission.permissionLevel}</Tag>
              <Tag color={permission.exposed ? "green" : "default"}>{TOOL_PERMISSION_REASON_LABELS[permission.reason] ?? permission.reason}</Tag>
              {permission.exposed && !tool && <Tag>本轮未下发 Schema</Tag>}
            </Space>
            {(fn.description || row.description) ? <Text type="secondary">{debugDisplay(fn.description || row.description)}</Text> : null}
            {permission.actions.length > 0 && <Text type="secondary">OneBot：{permission.actions.join(" / ")}</Text>}
            {tool && <details className="agent-debug-details"><summary>查看 Schema</summary><DebugRawBlock value={tool} /></details>}
          </div>
        </List.Item>;
      }}
    />}
  </div>;
}

export function toolNames(result: AgentDebugResponse): string[] {
  return result.toolPermissions.filter((item) => item.exposed).map((item) => item.name).sort();
}
