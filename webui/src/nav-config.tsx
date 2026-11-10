import {
  AuditOutlined,
  BarChartOutlined,
  CalendarOutlined,
  SearchOutlined,
  StarOutlined,
  BookOutlined,
  BulbOutlined,
  CommentOutlined,
  ControlOutlined,
  HeartOutlined,
  ProfileOutlined,
  RobotOutlined,
  TrophyOutlined,
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
  /** 侧栏分组标题；空字符串表示不分组、直接列在最上面（如“我的主页”） */
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
  { key: "/affinity", label: "好感度", icon: <HeartOutlined />, group: "群组与用户", description: "好感等级权益、礼物/商店/任务规则、衰减与关怀、全站统计", keywords: "affinity 好感 等级 权益 礼物 商店 兑换 任务 衰减 关怀 称呼 互动" },
  { key: "/private-agent", label: "私聊 Agent", icon: <MessageOutlined />, group: "群组与用户", description: "私聊提示词注册表（全局 / 某位用户）、每用户人设、最近请求与主动提议", keywords: "private agent 私聊 提示词 prompt 人设 persona 每用户 注册表 请求 抓取 主动 提议 offer 打扰" },
  { key: "/games", label: "对局中心", icon: <CrownOutlined />, group: "游戏与内容", description: "狼人杀与跑团实时对局", keywords: "game werewolf rpg 狼人杀 跑团" },
  { key: "/modules", label: "模组库", icon: <BookOutlined />, group: "游戏与内容", description: "跑团剧本模组", keywords: "module 模组 剧本" },
  { key: "/fanqie", label: "番茄小说", icon: <ReadOutlined />, group: "游戏与内容", description: "小说搜索与推送任务", keywords: "fanqie novel 小说" },
  { key: "/guest-access", label: "访客访问", icon: <KeyOutlined />, group: "系统设置", description: "访客访问码与可见群", keywords: "guest 访客" },
  { key: "/environment", label: "环境配置", icon: <SettingOutlined />, group: "系统设置", description: "模型路由与运行参数", keywords: "env config 配置 模型" },
];

export const GUEST_NAV: NavEntry[] = [
  { key: "/guest", label: "可查看群聊", icon: <EyeOutlined />, group: "访客视图", description: "被授权查看的群聊" },
];

// QQ 个人账号（群管理员 / 普通用户）只看自己的东西：主页不分组，其余按“助理 / 我和 Bot / 群与成长 / 账号”分组。
export const USER_NAV: NavEntry[] = [
  { key: "/me", label: "我的主页", icon: <HomeOutlined />, group: "", description: "今日概览：待办、提醒、心情、签到与快捷入口", keywords: "home 主页 今天 概览 dashboard" },
  { key: "/me/search", label: "搜一搜", icon: <SearchOutlined />, group: "", description: "一次搜遍我的待办、笔记、提醒、Bot 记得我的事和聊天记录", keywords: "search 搜索 查找 全局" },
  { key: "/me/assistant", label: "小本本", icon: <ProfileOutlined />, group: "私人助理", description: "待办、笔记、倒数日、心情，和私聊里让 Bot 记的是同一份", keywords: "todo 待办 笔记 note 倒数日 countdown 心情 mood 日记" },
  { key: "/me/calendar", label: "日历", icon: <CalendarOutlined />, group: "私人助理", description: "一个月里到期的待办、提醒、倒数日、心情、签到", keywords: "calendar 日历 月历 日程 安排" },
  { key: "/me/schedules", label: "我的定时任务", icon: <FieldTimeOutlined />, group: "私人助理", description: "私聊提醒和我在群里设的定时任务", keywords: "schedule 定时 提醒 闹钟 cron 群任务" },
  { key: "/me/tasks", label: "后台小助手", icon: <RobotOutlined />, group: "私人助理", description: "让 Bot 在后台帮我做的事：进度、结果、取消，以及我的虚拟电脑", keywords: "task 任务 小助手 后台 项目 电脑 sandbox" },
  { key: "/me/chat", label: "私聊设置", icon: <ControlOutlined />, group: "我和 Bot", description: "称呼、回复长短、免打扰、早安与签到提醒、主动提议", keywords: "chat 私聊 设置 偏好 免打扰 称呼 早安 生日 主动 提议 暂停" },
  { key: "/me/persona", label: "私聊人设", icon: <SmileOutlined />, group: "我和 Bot", description: "调整 Bot 和我私聊时的人设", keywords: "persona 人设 私聊 自称 称呼 性格" },
  { key: "/me/memory", label: "Bot 记得我", icon: <BulbOutlined />, group: "我和 Bot", description: "Bot 记住了我哪些事、群里怎么看我、让它忘掉、群聊私聊怎么互通", keywords: "memory 记忆 忘掉 隐私 共享 画像 回顾" },
  { key: "/me/chats", label: "聊天记录", icon: <CommentOutlined />, group: "我和 Bot", description: "和 Bot 的对话：翻看、搜索、改名、导出、删除", keywords: "chat history 聊天 记录 对话 搜索 导出 删除" },
  { key: "/me/groups", label: "我在的群", icon: <TeamOutlined />, group: "群与成长", description: "我在的所有群：身份、签到、本群功能；管理的群可以进去管", keywords: "group 群 管理 签到 功能" },
  { key: "/me/affinity", label: "好感与签到", icon: <HeartOutlined />, group: "群与成长", description: "和 Bot 的好感等级、来源、流水与各群连签", keywords: "affinity 好感 等级 签到 连签 积分 流水" },
  { key: "/me/games", label: "狼人杀战绩", icon: <TrophyOutlined />, group: "群与成长", description: "我的胜率、常玩角色、阵营表现与最近对局", keywords: "werewolf 狼人杀 战绩 胜率 角色 对局 game" },
  { key: "/me/insights", label: "数据洞察", icon: <BarChartOutlined />, group: "群与成长", description: "最近的聊天、待办、心情、签到走势，和我在各群的名次", keywords: "insights 洞察 统计 周报 趋势 排名 数据" },
  { key: "/me/achievements", label: "成就", icon: <StarOutlined />, group: "群与成长", description: "签到、关系、小本本、聊天、游戏里达成的成就和进度", keywords: "achievement 成就 徽章 勋章 进度" },
  { key: "/me/account", label: "账号与数据", icon: <LockOutlined />, group: "账号", description: "改密码、登录设备、显示偏好、导出和清空我的数据", keywords: "password 密码 安全 退出 设备 导出 数据 export 隐私 显示 字号 紧凑 清空" },
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
    // /me/groups/<群号> 属于“我在的群”；其余二级页各自高亮，未知路径回到主页
    const second = pathname.split("/")[2] ?? "";
    return USER_NAV.some((entry) => entry.key === `/me/${second}`) ? `/me/${second}` : "/me";
  }
  const root = `/${pathname.split("/")[1] || homePathForRole(resolved).slice(1)}`;
  return NAV_ALIASES[root] ?? root;
}

/** 手机上的底部标签栏（只给 QQ 个人账号）：最常用的四项 + “更多”打开完整侧栏。 */
export const USER_TABBAR_KEYS = ["/me", "/me/assistant", "/me/calendar", "/me/affinity"] as const;

/** 底部标签栏上的短名字 */
export const USER_TABBAR_LABELS: Record<string, string> = { "/me": "主页", "/me/assistant": "小本本", "/me/calendar": "日历", "/me/affinity": "签到" };
