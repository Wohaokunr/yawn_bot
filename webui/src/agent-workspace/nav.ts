// Agent 工作台的二级导航表：页面左侧栏、移动端横向条与深链 ?tab= 共用。
export type AgentSectionKey = "runtime" | "knowledge" | "debug" | "governance";

export interface AgentNavItem {
  key: string;
  label: string;
  hint: string;
}

export interface AgentNavSection {
  key: AgentSectionKey;
  label: string;
  items: AgentNavItem[];
}

export const AGENT_NAV: AgentNavSection[] = [
  {
    key: "runtime",
    label: "运行",
    items: [
      { key: "overview", label: "驾驶舱", hint: "状态、活跃度与执行脉搏" },
      { key: "config", label: "运行配置", hint: "触发、主动参与、记忆与工具" },
      { key: "persona", label: "人设", hint: "性格、说话方式与情绪" },
    ],
  },
  {
    key: "knowledge",
    label: "知识",
    items: [
      { key: "memories", label: "记忆", hint: "群级与公开记忆" },
      { key: "profiles", label: "成员画像", hint: "每个成员的沉淀" },
      { key: "relations", label: "关系", hint: "成员关系图谱" },
      { key: "messages", label: "消息", hint: "短期上下文消息" },
    ],
  },
  {
    key: "debug",
    label: "调试",
    items: [{ key: "debug", label: "调试台", hint: "执行 Trace 与模拟回合" }],
  },
  {
    key: "governance",
    label: "治理",
    items: [
      { key: "privacy", label: "隐私", hint: "成员退出与数据删除" },
      { key: "audit", label: "审计", hint: "特权工具调用记录" },
    ],
  },
];

export const AGENT_NAV_ORDER = AGENT_NAV.flatMap((section) => section.items.map((item) => item.key));

export const AGENT_TAB_KEYS = new Set(AGENT_NAV_ORDER);

export const AGENT_SECTION_BY_TAB: Record<string, AgentSectionKey> = Object.fromEntries(
  AGENT_NAV.flatMap((section) => section.items.map((item) => [item.key, section.key])),
);

export function agentTabLabel(key: string): string {
  for (const section of AGENT_NAV) {
    const item = section.items.find((entry) => entry.key === key);
    if (item) return item.label;
  }
  return key;
}
