import { Descriptions, Space, Tag, Typography } from "antd";
import type { AgentExecutionTrace } from "../types";
import {
  debugRecord,
  DELIVERY_STATE_LABELS,
  SEGMENT_NAME_LABELS,
  stringArray,
  TOOL_PERMISSION_REASON_LABELS,
  toolDisplayName,
  traceByteSize,
  TRACE_FIELD_LABELS,
  traceMetric,
  TRACE_MEDIA_SOURCE_LABELS,
  traceOutcomeLabel,
  triggerSourceLabel,
} from "./debug-utils";
import { ToolArgumentSummary } from "./ToolInspector";

const { Text } = Typography;

export function TraceDiagnosticValue({ name, value }: { name: string; value: unknown }): React.JSX.Element {
  if (value == null || value === "") return <Text type="secondary">—</Text>;
  if (typeof value === "boolean") return <Tag color={value ? "green" : "default"}>{value ? "是" : "否"}</Tag>;
  if (typeof value === "number") return <Text>{name === "size_bytes" ? traceByteSize(value) : value}</Text>;
  if (typeof value === "string") return <Text code={name.endsWith("_id") || name.includes("fingerprint")}>{value}</Text>;
  if (Array.isArray(value)) {
    const scalar = value.filter((item) => ["string", "number", "boolean"].includes(typeof item)).slice(0, 12);
    if (scalar.length === value.length) return <Space wrap size={[4, 4]}>{scalar.map((item, index) => <Tag key={`${String(item)}-${index}`}>{String(item)}</Tag>)}</Space>;
    return <Text type="secondary">{value.length} 项结构化数据</Text>;
  }
  const row = debugRecord(value);
  if (row.redacted === true) {
    const kind = String(row.kind ?? "sensitive");
    if (kind === "url") return <Space wrap size={[4, 4]}><Tag color="gold">完整 URL 已隐藏</Tag>{row.host ? <Text code>{String(row.host)}</Text> : null}{row.suffix ? <Tag>{String(row.suffix)}</Tag> : null}{row.has_query ? <Tag>含查询参数</Tag> : null}</Space>;
    if (kind === "path") return <Space wrap size={[4, 4]}><Tag color="gold">完整路径已隐藏</Tag>{row.platform ? <Tag>{String(row.platform)}</Tag> : null}{row.suffix ? <Tag>{String(row.suffix)}</Tag> : null}</Space>;
    if (kind === "payload") return <Space wrap size={[4, 4]}><Tag color="gold">Payload 值已隐藏</Tag>{stringArray(row.keys).map((key) => <Tag key={key}>{key}</Tag>)}</Space>;
    return <Tag color="gold">敏感值已隐藏 · {kind}</Tag>;
  }
  return <Space wrap size={[4, 4]}>{Object.entries(row).slice(0, 10).map(([key, item]) => <Tag key={key}>{key}={typeof item === "object" ? "…" : String(item)}</Tag>)}</Space>;
}

export function TraceDiagnosticFields({
  input,
  output,
}: {
  input: Record<string, unknown>;
  output: Record<string, unknown>;
}): React.JSX.Element | null {
  const hidden = new Set(["media", "items", "usage", "arguments", "trigger_signals", "onebot_actions", "selected_tool_names", "tool_names", "message_segment_types", "content_hashes", "roles"]);
  const rows = [
    ...Object.entries(input).map(([name, value]) => [`输入 · ${name}`, name, value] as const),
    ...Object.entries(output).map(([name, value]) => [`输出 · ${name}`, name, value] as const),
  ].filter(([, name, value]) => !hidden.has(name) && value != null && value !== "").slice(0, 18);
  if (rows.length === 0) return null;
  return <Descriptions
    className="agent-trace-facts"
    size="small"
    column={{ xs: 1, sm: 2, lg: 3 }}
    items={rows.map(([key, name, value]) => ({ key, label: TRACE_FIELD_LABELS[name] ?? name, children: <TraceDiagnosticValue name={name} value={value} /> }))}
  />;
}

export function TraceHumanSummary({ event }: { event: AgentExecutionTrace["events"][number] }): React.JSX.Element | null {
  const input = debugRecord(event.input);
  const output = debugRecord(event.output);

  if (event.phase === "intake" && (output.trigger_source || output.scene)) {
    const trigger = triggerSourceLabel(output.trigger_source);
    const scene = String(output.scene ?? "");
    const signals = debugRecord(output.trigger_signals);
    const matched = Object.entries(signals).filter(([, value]) => value === true).map(([key]) => key);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>这次执行的触发原因：<Tag color="blue">{trigger}</Tag></Text>
      {scene && <Text type="secondary">内部参与场景：{scene === "warmup" ? "冷场暖场" : scene === "active" ? "加入活跃话题" : scene === "followup" ? "自然续聊" : scene}</Text>}
      {matched.length > 1 && <Text type="secondary">同时检测到：{matched.join(" / ")}</Text>}
      {output.text_preview ? <Text type="secondary">消息预览：{String(output.text_preview)}</Text> : null}
      {typeof output.queue_wait_ms === "number" ? <Text type="secondary">进入执行前排队 {output.queue_wait_ms.toFixed(1)} ms</Text> : null}
    </Space>;
  }

  if (event.phase === "capability") {
    const exposed = stringArray(output.selected_tools ?? output.selected_tool_names ?? output.exposed_tools ?? output.tool_names);
    const messageSegments = stringArray(output.message_segment_types);
    const blocked = Array.isArray(output.blocked_tools) ? output.blocked_tools.map((item) => debugRecord(item)) : [];
    const toolCount = traceMetric(output.tool_count, exposed.length);
    const roundLimit = traceMetric(output.round_limit);
    return <Space orientation="vertical" size={6} style={{ width: "100%" }}>
      <Text>
        本轮向模型开放 <Text strong>{toolCount}</Text> 个工具
        {blocked.length > 0 ? <>，另有 <Text strong>{blocked.length}</Text> 个工具未开放</> : ""}
        {roundLimit > 0 ? <>；模型调用最多 <Text strong>{roundLimit}</Text> 轮。</> : "。"}
      </Text>
      {exposed.length > 0 && <Space wrap size={[6, 6]}>
        <Text type="secondary">可用：</Text>
        {exposed.map((name) => <Tag color="green" key={name}>{toolDisplayName(name)}</Tag>)}
      </Space>}
      {messageSegments.length > 0 && exposed.includes("send_message") && <Space wrap size={[6, 6]}>
        <Text type="secondary">本轮消息段：</Text>
        {messageSegments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}
      </Space>}
      {blocked.length > 0 && <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        <Text type="secondary">未开放：</Text>
        {blocked.map((item, index) => {
          const name = String(item.name ?? "");
          const reason = String(item.reason ?? "");
          return <Text key={`${name}-${index}`}>
            <Tag>{toolDisplayName(name)}</Tag>
            {TOOL_PERMISSION_REASON_LABELS[reason] ?? (reason || "未满足当前权限条件")}
          </Text>;
        })}
      </Space>}
    </Space>;
  }

  if (event.phase === "context") {
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        最终选入 <Text strong>{traceMetric(output.messages)}</Text> 条历史消息、
        <Text strong>{traceMetric(output.members)}</Text> 位成员、
        <Text strong>{traceMetric(output.memories)}</Text> 条记忆和
        <Text strong>{traceMetric(output.relations)}</Text> 条关系。
      </Text>
      {output.model ? <Text type="secondary">上下文按模型 <Text code>{String(output.model)}</Text> 的预算装箱；上限 {traceMetric(input.context_token_limit)} Token，预留输出 {traceMetric(input.completion_reserve)} Token。</Text> : null}
      {input.query_preview ? <Text type="secondary">用于相关性选择的文本：{String(input.query_preview)}</Text> : null}
    </Space>;
  }

  if (event.phase === "parse" || event.phase === "intake") {
    const replyDepth = traceMetric(output.reply_depth);
    const forwardNodes = traceMetric(output.forward_nodes ?? output.top_level_nodes);
    const mediaRefs = traceMetric(output.media_refs ?? output.media_refs_total);
    const segments = stringArray(output.segment_types);
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {event.label}：{replyDepth > 0 ? `解析了 ${replyDepth} 层引用；` : ""}
        {forwardNodes > 0 ? `展开 ${forwardNodes} 个转发节点；` : ""}
        {mediaRefs > 0 ? `发现 ${mediaRefs} 个媒体引用。` : "未发现额外媒体。"}
      </Text>
      {segments.length > 0 && <Space wrap size={[6, 6]}>{segments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}</Space>}
    </Space>;
  }

  if (event.phase === "media") {
    const media = Array.isArray(input.media) ? input.media.map((item) => debugRecord(item)) : [];
    const items = Array.isArray(output.items) ? output.items.map((item) => debugRecord(item)) : [];
    const statusLabel: Record<string, string> = {
      loaded: "已读取并转成视觉输入",
      url_passthrough: "本地读取失败，改由模型读取原始 URL",
      dropped_unavailable: "无法取得图片，已丢弃",
      skipped_non_image: "不是图片，已跳过",
    };
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        准备了 <Text strong>{traceMetric(output.vision_blocks)}</Text> 个视觉输入，
        命中 <Text strong>{traceMetric(output.cached_captions)}</Text> 条图片转述缓存。
      </Text>
      {output.multimodal_mode ? <Text type="secondary">当前多模态策略：<Text code>{String(output.multimodal_mode)}</Text>；媒体缓存：{output.cache_enabled ? "开启" : "关闭"}。</Text> : null}
      {media.length > 0 && <Space wrap size={[6, 6]}>{media.map((item, index) => {
        const source = String(item.source || "current");
        return <Tag key={`${String(item.type)}-${index}`}>{SEGMENT_NAME_LABELS[String(item.type)] ?? String(item.type || "媒体")} · {TRACE_MEDIA_SOURCE_LABELS[source] ?? source}</Tag>;
      })}</Space>}
      {items.length > 0 && <Space orientation="vertical" size={4} style={{ width: "100%" }}>
        {items.map((item, index) => {
          const status = String(item.status ?? "unknown");
          return <div key={`${status}-${index}`} className="agent-trace-media-row">
            <Space wrap size={[4, 4]}>
              <Tag color={status === "loaded" ? "green" : status === "dropped_unavailable" ? "red" : "gold"}>图片 {index + 1}</Tag>
              <Text>{statusLabel[status] ?? status}</Text>
              {item.mime ? <Tag>{String(item.mime)}</Tag> : null}
              {typeof item.size_bytes === "number" ? <Tag>{traceByteSize(item.size_bytes)}</Tag> : null}
              {item.url_host ? <Tag>{String(item.url_host)}</Tag> : null}
              {item.caption_cache ? <Tag>字幕缓存 {String(item.caption_cache)}</Tag> : null}
            </Space>
            {item.reason ? <Text type="danger">{String(item.reason)}</Text> : null}
          </div>;
        })}
      </Space>}
    </Space>;
  }

  if (event.phase === "prompt") {
    const count = traceMetric(output.message_count);
    const promptCache = String(output.prompt_cache ?? "");
    const contextCache = String(output.context_cache ?? "");
    const roles = debugRecord(output.roles);
    const tools = stringArray(output.tool_names);
    const hasPromptShape = typeof output.text_chars === "number" || typeof output.media_blocks === "number";
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        已组装 <Text strong>{count}</Text> 条 Prompt 消息
        {hasPromptShape ? <>，共约 <Text strong>{traceMetric(output.text_chars)}</Text> 个文本字符、<Text strong>{traceMetric(output.media_blocks)}</Text> 个图片块</> : null}
        {promptCache ? `；Prompt 前缀缓存${promptCache === "hit" ? "命中" : "未命中"}` : ""}
        {contextCache ? `，稳定上下文缓存${contextCache === "hit" ? "命中" : "未命中"}` : ""}。
      </Text>
      {Object.keys(roles).length > 0 && <Space wrap size={[4, 4]}><Text type="secondary">角色构成：</Text>{Object.entries(roles).map(([role, amount]) => <Tag key={role}>{role} × {String(amount)}</Tag>)}</Space>}
      {tools.length > 0 && <Space wrap size={[4, 4]}><Text type="secondary">Prompt 携带工具：</Text>{tools.map((tool) => <Tag key={tool}>{toolDisplayName(tool)}</Tag>)}</Space>}
      {output.current_turn_preview ? <Text type="secondary">模型看到的当前回合预览：{String(output.current_turn_preview)}</Text> : null}
    </Space>;
  }

  if (event.phase === "llm") {
    const tools = stringArray(output.tool_calls);
    const action = String(output.action ?? "");
    const chars = traceMetric(output.content_chars);
    const usage = debugRecord(output.usage);
    const requestUsage = debugRecord(usage.request);
    const turnUsage = debugRecord(usage.turn);
    const hasTurnUsage = Object.keys(turnUsage).length > 0;
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {action ? `模型决策：${action}` : `模型返回 ${chars} 个字符`}
        {tools.length > 0 ? `，并请求调用 ${tools.length} 个工具。` : "。"}
      </Text>
      {tools.length > 0 && <Space wrap size={[6, 6]}>{tools.map((name) => <Tag color="blue" key={name}>{toolDisplayName(name)}</Tag>)}</Space>}
      {output.model ? <Text type="secondary">模型：<Text code>{String(output.model)}</Text>{output.finish_reason ? `；结束原因：${String(output.finish_reason)}` : ""}</Text> : null}
      {output.content_preview ? <Text type="secondary">输出预览：{String(output.content_preview)}</Text> : null}
      {hasTurnUsage && <Text type="secondary">
        本次请求输入/输出：{traceMetric(requestUsage.prompt_tokens)} / {traceMetric(requestUsage.completion_tokens)} Token；
        本回合累计输入：{traceMetric(turnUsage.prompt_tokens)}，其中缓存命中 {traceMetric(turnUsage.cached_tokens)}、未命中 {traceMetric(turnUsage.cache_miss_tokens)} Token。
      </Text>}
      {typeof output.confidence === "number" && <Text type="secondary">决策置信度：{Math.round(output.confidence * 100)}%</Text>}
    </Space>;
  }

  if (event.phase === "tool") {
    const toolName = event.label.replace(/^工具\s+/, "").replace(/^工具意图\s+/, "");
    const executed = output.executed;
    const ok = output.ok;
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {executed === false
          ? <>模型计划调用 <Text strong>{toolDisplayName(toolName)}</Text>，但这是无副作用调试，因此没有实际执行。</>
          : <><Text strong>{toolDisplayName(toolName)}</Text>{ok === false ? " 执行失败" : " 执行完成"}{output.ends_turn ? "，并结束本轮回复" : ""}。</>}
      </Text>
      <ToolArgumentSummary name={toolName} value={input.arguments} />
    </Space>;
  }

  if (event.phase === "outbound") {
    const segments = stringArray(output.segment_types ?? output.fallback_types ?? input.segment_types);
    const deliveryState = String(output.delivery_state ?? "");
    const degradedFrom = String(output.degraded_from ?? "");
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>
        {deliveryState ? (DELIVERY_STATE_LABELS[deliveryState] ?? deliveryState) : event.label}
        {degradedFrom ? `；已从 ${degradedFrom} 方案降级` : ""}。
      </Text>
      {segments.length > 0 && <Space wrap size={[6, 6]}><Text type="secondary">实际消息：</Text>{segments.map((name) => <Tag key={name}>{SEGMENT_NAME_LABELS[name] ?? name}</Tag>)}</Space>}
      {(output.text_preview || input.text_preview) ? <Text type="secondary">发送文本预览：{String(output.text_preview ?? input.text_preview)}</Text> : null}
      {output.message_id ? <Text type="secondary">OneBot message_id：<Text code>{String(output.message_id)}</Text></Text> : null}
    </Space>;
  }

  if (event.phase === "state") {
    if (event.status === "skipped") return <Text>本次是调试执行，不会修改数据库、冷却时间或 Agent 状态。</Text>;
    if (event.status === "failed") return <Text type="danger">状态写入失败，已按当前流程回滚或保留发送结果。</Text>;
    if (event.status === "degraded") return <Text type="warning">消息投递流程已结束，但部分本地状态更新失败；发送结果不会因此改判为失败。</Text>;
    return <Text>本轮状态已经写入，包括去重指纹、冷却时间和会话进度。</Text>;
  }

  if (event.phase === "turn") {
    const outcome = String(output.outcome ?? "");
    return <Space orientation="vertical" size={4} style={{ width: "100%" }}>
      <Text>本轮执行结束{outcome ? `，结果：${traceOutcomeLabel(outcome)}` : ""}。</Text>
      {output.error_type ? <Text type="danger">{String(output.error_type)}{output.error_message ? `：${String(output.error_message)}` : ""}</Text> : null}
    </Space>;
  }

  return null;
}
