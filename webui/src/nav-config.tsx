import {
  AuditOutlined,
  BookOutlined,
  CodeOutlined,
  CrownOutlined,
  DashboardOutlined,
  EyeOutlined,
  GlobalOutlined,
  FieldTimeOutlined,
  HomeOutlined,
  KeyOutlined,
  MessageOutlined,
  ReadOutlined,
  LockOutlined,
  SettingOutlined,
  SmileOutlined,
  TeamOutlined,
  UserOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";
import type { SessionRole } from "./auth-session";

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
  { key: "/stickers", label: "表情包", icon: <SmileOutlined />, group: "运行监控", description: "Agent 的表情包库：上传、打标签、审核群友收藏，发送频率与菜单预览", keywords: "sticker emoji 表情 表情包 斗图 贴图 收藏 meme reaction" },
  { key: "/schedules", label: "定时任务", icon: <FieldTimeOutlined />, group: "运行监控", description: "Agent 的提醒与定时工作：任务、运行记录、免打扰与限额", keywords: "schedule cron timer 定时 提醒 闹钟 循环 每天 每周 唤醒 任务" },
  { key: "/audits", label: "操作审计", icon: <AuditOutlined />, group: "运行监控", description: "管理台写操作记录", keywords: "audit 日志" },
  { key: "/groups", label: "群组", icon: <TeamOutlined />, group: "群组与用户", description: "群概览、成员、群功能与单独设置，进入 Agent 工作台", keywords: "group agent 权限 群 成员 签到" },
  { key: "/users", label: "用户", icon: <UserOutlined />, group: "群组与用户", description: "用户档案：好感签到、所在群、私聊功能与私聊 Agent", keywords: "user 用户 好感 签到 私聊 全局用户" },
  { key: "/private-agent", label: "私聊 Agent", icon: <MessageOutlined />, group: "群组与用户", description: "私聊提示词注册表（全局 / 某位用户）、每用户人设与最近请求", keywords: "private agent 私聊 提示词 prompt 人设 persona 每用户 注册表 请求 抓取" },
  { key: "/games", label: "对局中心", icon: <CrownOutlined />, group: "游戏与内容", description: "狼人杀与跑团实时对局", keywords: "game werewolf rpg 狼人杀 跑团" },
  { key: "/modules", label: "模组库", icon: <BookOutlined />, group: "游戏与内容", description: "跑团剧本模组", keywords: "module 模组 剧本" },
  { key: "/fanqie", label: "番茄小说", icon: <ReadOutlined />, group: "游戏与内容", description: "小说搜索与推送任务", keywords: "fanqie novel 小说" },
  { key: "/guest-access", label: "访客访问", icon: <KeyOutlined />, group: "系统设置", description: "访客访问码与可见群", keywords: "guest 访客" },
  { key: "/environment", label: "环境配置", icon: <SettingOutlined />, group: "系统设置", description: "模型路由与运行参数", keywords: "env config 配置 模型" },
];

export const GUEST_NAV: NavEntry[] = [
  { key: "/guest", label: "可查看群聊", icon: <EyeOutlined />, group: "访客视图", description: "被授权查看的群聊" },
];

// QQ 个人账号（群管理员 / 普通用户）只看自己的东西：管理的群、私聊人设、账号安全。
export const USER_NAV: NavEntry[] = [
  { key: "/me", label: "我的主页", icon: <HomeOutlined />, group: "我的空间", description: "我管理的群与账号概况", keywords: "home 主页 群 管理" },
  { key: "/me/persona", label: "私聊人设", icon: <SmileOutlined />, group: "我的空间", description: "调整 Bot 和我私聊时的人设", keywords: "persona 人设 私聊 自称 称呼 性格" },
  { key: "/me/account", label: "账号安全", icon: <LockOutlined />, group: "我的空间", description: "改密码、退出所有设备", keywords: "password 密码 安全 退出 设备" },
];

export function navForRole(role: SessionRole): NavEntry[] {
  if (role === "guest") return GUEST_NAV;
  if (role === "user") return USER_NAV;
  return ADMIN_NAV;
}

export function homePathForRole(role: SessionRole): string {
  if (role === "guest") return "/guest";
  if (role === "user") return "/me";
  return "/overview";
}

// Agent 工作台属于“群组”这个入口，侧栏高亮跟随群组。
const NAV_ALIASES: Record<string, string> = { "/agent": "/groups" };

/** 第二个参数兼容旧的 isGuest 布尔值。 */
export function selectedNavKey(pathname: string, role: boolean | SessionRole): string {
  const resolved: SessionRole = role === true ? "guest" : role === false ? "admin" : role;
  if (resolved === "user") {
    // /me/groups/… 属于“我的主页”；/me/persona、/me/account 各自高亮
    const second = pathname.split("/")[2] ?? "";
    return second === "persona" || second === "account" ? `/me/${second}` : "/me";
  }
  const root = `/${pathname.split("/")[1] || homePathForRole(resolved).slice(1)}`;
  return NAV_ALIASES[root] ?? root;
}
