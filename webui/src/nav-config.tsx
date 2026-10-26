import {
  AuditOutlined,
  BookOutlined,
  CodeOutlined,
  CrownOutlined,
  DashboardOutlined,
  EyeOutlined,
  GlobalOutlined,
  FieldTimeOutlined,
  KeyOutlined,
  ReadOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";

// 侧栏与命令面板共用同一份导航表，避免两处入口不同步。
export interface NavEntry {
  key: string;
  label: string;
  icon: ReactNode;
  group: string;
  description: string;
  keywords?: string;
}

export const ADMIN_NAV: NavEntry[] = [
  { key: "/overview", label: "运行概览", icon: <DashboardOutlined />, group: "运行监控", description: "Bot、AI 与任务的健康状况", keywords: "overview dashboard 首页 状态" },
  { key: "/sandbox", label: "代码沙箱", icon: <CodeOutlined />, group: "运行监控", description: "Agent 的内置电脑：群聊附件收件箱、运行记录、工作区文件与限额", keywords: "sandbox python 虚拟机 内置电脑 代码 运行 vm 附件 图片 文件 语音 inbox 收件箱" },
  { key: "/web-search", label: "网页搜索", icon: <GlobalOutlined />, group: "运行监控", description: "Agent 的 DeepSeek 联网搜索：开放范围、次数预算、费用与最近调用", keywords: "web search 搜索 联网 上网 deepseek 网页 新闻 费用 预算" },
  { key: "/schedules", label: "定时任务", icon: <FieldTimeOutlined />, group: "运行监控", description: "Agent 的提醒与定时工作：任务、运行记录、免打扰与限额", keywords: "schedule cron timer 定时 提醒 闹钟 循环 每天 每周 唤醒 任务" },
  { key: "/audits", label: "操作审计", icon: <AuditOutlined />, group: "运行监控", description: "管理台写操作记录", keywords: "audit 日志" },
  { key: "/groups", label: "群组", icon: <TeamOutlined />, group: "群组与用户", description: "按群管理 Agent 与功能权限", keywords: "group agent 权限 群" },
  { key: "/users", label: "全局用户", icon: <UserOutlined />, group: "群组与用户", description: "跨群的用户功能覆盖", keywords: "user 用户 好感" },
  { key: "/games", label: "对局中心", icon: <CrownOutlined />, group: "游戏与内容", description: "狼人杀与跑团实时对局", keywords: "game werewolf rpg 狼人杀 跑团" },
  { key: "/modules", label: "模组库", icon: <BookOutlined />, group: "游戏与内容", description: "跑团剧本模组", keywords: "module 模组 剧本" },
  { key: "/fanqie", label: "番茄小说", icon: <ReadOutlined />, group: "游戏与内容", description: "小说搜索与推送任务", keywords: "fanqie novel 小说" },
  { key: "/guest-access", label: "访客访问", icon: <KeyOutlined />, group: "系统设置", description: "访客访问码与可见群", keywords: "guest 访客" },
  { key: "/environment", label: "环境配置", icon: <SettingOutlined />, group: "系统设置", description: "模型路由与运行参数", keywords: "env config 配置 模型" },
];

export const GUEST_NAV: NavEntry[] = [
  { key: "/guest", label: "可查看群聊", icon: <EyeOutlined />, group: "访客视图", description: "被授权查看的群聊" },
];

// Agent 工作台属于“群组”这个入口，侧栏高亮跟随群组。
const NAV_ALIASES: Record<string, string> = { "/agent": "/groups" };

export function selectedNavKey(pathname: string, isGuest: boolean): string {
  const root = `/${pathname.split("/")[1] || (isGuest ? "guest" : "overview")}`;
  return NAV_ALIASES[root] ?? root;
}
