import type { AgentDebugMode, AgentMessageItem } from "../types";

export const AGENT_DEBUG_MODES: Array<{ value: AgentDebugMode; label: string }> = [
  { value: "dialogue", label: "普通对话" },
  { value: "active", label: "活跃插话" },
  { value: "warmup", label: "冷场暖场" },
  { value: "followup", label: "短会话续聊" },
];

export function agentDebugModeLabel(value: string): string {
  return AGENT_DEBUG_MODES.find((item) => item.value === value)?.label ?? value;
}

export function debugMessageLabel(row: AgentMessageItem): string {
  const actor = (row.senderName || row.userId).trim();
  const text = row.text.trim() || "[媒体消息]";
  return `${actor} · ${text.slice(0, 52)}`;
}

export function debugJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}

export function debugRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function debugDisplay(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return debugJson(value);
}

export function DebugRawBlock({ value }: { value: unknown }): React.JSX.Element {
  return <pre className="agent-debug-raw">{debugJson(value)}</pre>;
}

export const TOOL_PERMISSION_LABELS: Record<string, string> = {
  read: "只读",
  state_write: "状态写入",
  message_send: "消息发送",
  privileged: "特权",
  critical: "高风险",
};

export const TOOL_PERMISSION_REASON_LABELS: Record<string, string> = {
  exposed: "已暴露",
  permission_level: "本轮没有开放这个权限等级",
  onebot_action: "当前 OneBot 实现不支持这个操作",
  bot_not_admin: "机器人没有群管理权限",
  bot_not_owner: "机器人不是群主，无法执行这个操作",
  actor_not_admin: "当前调用者没有群管理权限",
  not_allowlisted: "当前群没有把这个特权工具加入白名单",
};

export const TOOL_NAME_LABELS: Record<string, string> = {
  get_group_info: "查看群信息",
  get_group_member: "查看成员详情",
  list_group_members: "查看群成员列表",
  get_message: "读取消息",
  get_recent_group_messages: "读取近期群消息",
  discover_tools: "发现可用工具",
  search_group_memory: "搜索群记忆",
  get_person_profile: "查看成员画像",
  list_user_relations: "查看成员关系",
  record_user_relation: "记录成员关系",
  search_reactions: "搜索表情包",
  send_message: "发送消息",
  send_forward: "发送合并转发",
  react_to_message: "消息表情回应",
  list_group_notices: "查看群公告",
  list_essence_messages: "查看精华消息",
  list_muted_members: "查看禁言列表",
  get_group_honor: "查看群荣誉",
  list_group_files: "查看群文件",
  get_group_file_link: "获取群文件链接",
  mute_member: "禁言成员",
  create_group_announcement: "发布群公告",
  set_essence_message: "设置精华消息",
  remove_essence_message: "移出精华消息",
  delete_group_notice: "删除群公告",
  set_group_card: "修改群名片",
  set_special_title: "设置专属头衔",
  set_group_name: "修改群名称",
  create_group_folder: "创建群文件夹",
  send_file: "发送群文件",
  kick_member: "踢出成员",
  set_whole_group_mute: "全员禁言",
  set_group_admin: "设置群管理员",
  delete_group_file: "删除群文件",
  move_group_file: "移动群文件",
  rename_group_file: "重命名群文件",
  delete_group_folder: "删除群文件夹",
};

export const SEGMENT_NAME_LABELS: Record<string, string> = {
  text: "文本",
  at: "@成员",
  reply: "引用回复",
  image: "图片",
  face: "QQ 表情",
  record: "语音",
  video: "视频",
  file: "文件",
  forward: "合并转发",
};

export const DELIVERY_STATE_LABELS: Record<string, string> = {
  confirmed_success: "确认发送成功",
  confirmed_failure: "确认发送失败",
  degraded_success: "降级后发送成功",
  unknown: "投递结果未知",
};

export const TRACE_MEDIA_SOURCE_LABELS: Record<string, string> = {
  current: "当前消息",
  reply: "引用消息",
  forward: "合并转发",
  history: "历史消息",
  tool: "工具搜索结果",
};

const TRACE_TRIGGER_SOURCE_LABELS: Record<string, string> = {
  mention: "@ 明确呼叫",
  reply: "回复 Agent",
  wake_word: "叫名 / 唤醒词",
  explicit_call: "明确呼叫",
  proactive_warmup: "主动参与 · 冷场暖场",
  proactive_interject: "主动参与 · 加入话题",
  proactive_participation: "主动参与",
  conversation_followup: "自然续聊",
};

export function triggerSourceLabel(value: unknown): string {
  const key = String(value ?? "");
  return TRACE_TRIGGER_SOURCE_LABELS[key] ?? (key || "—");
}

const TRACE_OUTCOME_LABELS: Record<string, string> = {
  completed: "正常完成",
  success: "成功",
  speak: "已发言",
  wait: "决定暂不发言",
  close: "结束会话",
  error: "执行失败",
  timeout: "模型调用超时",
  snapshot: "仅生成调试快照",
  delivery_unknown: "消息投递结果未知",
};

export function traceOutcomeLabel(value: unknown): string {
  const key = String(value ?? "");
  return TRACE_OUTCOME_LABELS[key] ?? (key || "—");
}

export function toolDisplayName(name: unknown): string {
  const key = String(name ?? "");
  return TOOL_NAME_LABELS[key] ?? (key || "未知工具");
}

export function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

export function traceMetric(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export const TRACE_FIELD_LABELS: Record<string, string> = {
  model: "模型",
  text_preview: "文本预览",
  content_preview: "模型输出预览",
  current_turn_preview: "当前回合预览",
  query_preview: "检索文本预览",
  error_type: "异常类型",
  error_message: "异常信息",
  finish_reason: "结束原因",
  message_id: "消息 ID",
  text_chars: "文本字符",
  query_chars: "检索字符",
  current_turn_chars: "当前回合字符",
  size_bytes: "大小",
  mime: "MIME",
  url_host: "URL 主机",
  url_allowed: "URL 可直传",
  status: "处理状态",
  reason: "原因",
  multimodal_mode: "多模态模式",
  cache_enabled: "媒体缓存",
  caption_cache: "图片转述缓存",
  context_token_limit: "上下文 Token 上限",
  completion_reserve: "预留输出 Token",
  prefix_fingerprint: "Prompt 指纹",
  prompt_cache: "Prompt 前缀缓存",
  context_cache: "稳定上下文缓存",
};

export function traceByteSize(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(2)} MiB`;
}

export function traceDurationSeconds(value: unknown): string {
  const seconds = traceMetric(value, -1);
  if (seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

export const TRACE_PHASE_LABELS: Record<string, string> = {
  parse: "解析",
  intake: "输入",
  policy: "策略",
  context: "上下文",
  capability: "能力",
  media: "媒体",
  prompt: "Prompt",
  llm: "模型",
  tool: "工具",
  speech: "发言",
  outbound: "发送",
  state: "状态",
  turn: "回合",
};

export const TRACE_STATUS_META: Record<string, { label: string; color: string }> = {
  running: { label: "执行中", color: "processing" },
  completed: { label: "完成", color: "green" },
  planned: { label: "计划", color: "blue" },
  success: { label: "成功", color: "green" },
  failed: { label: "失败", color: "red" },
  degraded: { label: "降级", color: "orange" },
  unknown: { label: "未知", color: "gold" },
  skipped: { label: "跳过", color: "default" },
};
